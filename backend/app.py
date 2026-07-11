from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.agent.copilot import resume_copilot, run_copilot
from backend.guardrails.approvals import approval_store
from backend.mcp.auth import AuthContext, get_auth_context, issue_demo_token
from backend.memory.store import list_memory_writes
from backend.memory.database import memory_db
from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import TOOL_REGISTRY, call_tool
from backend.mock.repository import repo
from backend.mock.api import router as mock_router


class ChatRequest(BaseModel):
    message: str
    roles: list[str] = Field(default_factory=lambda: ["readonly"])
    history: list[dict[str, str]] = Field(default_factory=list)
    summary: str = ""
    conversation_id: str = "demo-conversation"


class DemoTokenRequest(BaseModel):
    user_id: str = "demo-user"
    roles: list[str] = Field(default_factory=lambda: ["readonly"])
    tenant_id: str = "demo-tenant"


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: str = ""


app = FastAPI(title="DCS Copilot Demo")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5174", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(mock_router)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "dcs-copilot-demo"}


@app.get("/api/overview")
def overview():
    return repo.overview()


@app.get("/api/tools")
def tools():
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "risk": spec.risk,
            "auth_roles": spec.auth_roles,
            "input_schema": spec.input_model.model_json_schema(),
        }
        for spec in TOOL_REGISTRY.values()
    ]


@app.post("/api/auth/demo-token")
def demo_token(request: DemoTokenRequest):
    return {"access_token": issue_demo_token(request.user_id, request.roles, request.tenant_id), "token_type": "bearer"}


@app.post("/api/tools/call")
def tool_call(request: ToolRequest, auth: AuthContext = Depends(get_auth_context)):
    secured = request.model_copy(update={
        "caller_user_id": auth.user_id,
        "caller_roles": auth.roles,
        "tenant_id": auth.tenant_id,
    })
    return call_tool(secured)


@app.post("/api/chat")
def chat(request: ChatRequest, auth: AuthContext = Depends(get_auth_context)):
    return run_copilot(
        request.message,
        auth.roles,
        request.history,
        request.summary,
        request.conversation_id,
        auth.user_id,
        auth.tenant_id,
    )


@app.get("/api/audit")
def audit():
    return memory_db.list_tool_audit(50)


@app.get("/api/approvals")
def approvals(auth: AuthContext = Depends(get_auth_context)):
    if not set(auth.roles) & {"ops", "admin"}:
        return []
    return approval_store.list()


@app.post("/api/approvals/{approval_id}/decision")
def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    if not set(auth.roles) & {"ops", "admin"}:
        raise HTTPException(status_code=403, detail="当前角色无权审批变更")
    approval = approval_store.get(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="审批项不存在")
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail="审批项已处理")
    if approval["risk"] == "high" and "admin" not in auth.roles:
        raise HTTPException(status_code=403, detail="高风险变更必须由 admin 审批")
    return resume_copilot(
        approval["conversation_id"],
        request.approved,
        auth.user_id,
        request.reason,
    )


@app.get("/api/memory")
def memory():
    return list_memory_writes(50)
