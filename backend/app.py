from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from time import perf_counter

from backend.agent.copilot import (
    PendingApprovalError,
    close_graph_runtime,
    get_graph,
    resume_copilot,
    run_copilot,
)
from backend.agent.llm import get_public_llm_status
from backend.guardrails.approvals import approval_store
from backend.guardrails.chat_limits import chat_limiter
from backend.mcp.auth import DEMO_MODE, AuthContext, get_auth_context, issue_demo_token
from backend.memory.store import list_memory_writes
from backend.memory.database import memory_db
from backend.mcp.schemas import GatewayToolRequest, ToolRequest
from backend.mcp.tools import TOOL_REGISTRY, call_tool
from backend.mock.api import router as mock_router
from backend.providers import repo, runtime_config
from backend.observability import APPROVAL_DECISIONS, HTTP_LATENCY, HTTP_REQUESTS, INTENT_COUNT, create_metrics_app
from backend.memory.retriever import ensure_knowledge_seeded


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None


class DemoTokenRequest(BaseModel):
    user_id: str = "demo-user"
    roles: list[str] = Field(default_factory=lambda: ["readonly"])
    tenant_id: str = "demo-tenant"


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: str = ""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_knowledge_seeded()
    get_graph()
    yield
    close_graph_runtime()


app = FastAPI(title="DCS Copilot Demo", lifespan=lifespan)

cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "DCS_CORS_ORIGINS",
        "http://127.0.0.1:5174,http://localhost:5174",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if runtime_config.expose_mock_api:
    app.include_router(mock_router)
metrics_app = create_metrics_app()
if metrics_app is not None:
    app.mount("/metrics", metrics_app)
else:
    @app.get("/metrics/", include_in_schema=False)
    def metrics_unavailable() -> Response:
        return Response("# Prometheus metrics are disabled\n", media_type="text/plain")


@app.middleware("http")
async def collect_http_metrics(request, call_next):
    started = perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    HTTP_REQUESTS.labels(request.method, path, response.status_code).inc()
    HTTP_LATENCY.labels(path).observe(perf_counter() - started)
    return response


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "dcs-copilot-demo"}


@app.get("/api/llm-status")
def llm_status(_auth: AuthContext = Depends(get_auth_context)):
    return get_public_llm_status(aggregate=True)


@app.get("/api/overview")
def overview():
    return repo.overview()


@app.get("/api/platform-status")
def platform_status():
    return repo.platform_status()


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
    if not DEMO_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    return {"access_token": issue_demo_token(request.user_id, request.roles, request.tenant_id), "token_type": "bearer"}


@app.post("/api/tools/call")
def tool_call(request: GatewayToolRequest, auth: AuthContext = Depends(get_auth_context)):
    return call_tool(ToolRequest(
        tool_name=request.tool_name,
        params=request.params,
        task_id=request.task_id,
        caller_user_id=auth.user_id,
        caller_roles=auth.roles,
        tenant_id=auth.tenant_id,
    ))


@app.post("/api/chat")
def chat(request: ChatRequest, auth: AuthContext = Depends(get_auth_context)):
    lease = chat_limiter.try_acquire(auth.user_id, auth.tenant_id)
    if lease is None:
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁或当前并发已满，请稍后重试",
            headers={"Retry-After": "10"},
        )
    try:
        try:
            result = run_copilot(
                message=request.message,
                roles=auth.roles,
                conversation_id=request.conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="会话不属于当前用户或租户") from exc
        except PendingApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        lease.release()
    INTENT_COUNT.labels(result.get("intent") or "unknown").inc()
    return result


@app.get("/api/audit")
def audit(auth: AuthContext = Depends(get_auth_context)):
    return memory_db.list_tool_audit(50, auth.tenant_id)


@app.get("/api/approvals")
def approvals(auth: AuthContext = Depends(get_auth_context)):
    if not set(auth.roles) & {"ops", "admin"}:
        return []
    return approval_store.list(tenant_id=auth.tenant_id)


@app.post("/api/approvals/{approval_id}/decision")
def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    if not set(auth.roles) & {"ops", "admin"}:
        raise HTTPException(status_code=403, detail="当前角色无权审批变更")
    approval = approval_store.get(approval_id, auth.tenant_id)
    if not approval:
        raise HTTPException(status_code=404, detail="审批项不存在")
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail="审批项已处理")
    if approval["user_id"] == auth.user_id:
        raise HTTPException(status_code=403, detail="发起人不能审批自己的变更")
    if approval["risk"] == "high" and "admin" not in auth.roles:
        raise HTTPException(status_code=403, detail="高风险变更必须由 admin 审批")
    if not approval["resume_required"]:
        try:
            decided = approval_store.decide(approval_id, request.approved, auth.user_id, request.reason)
        except (KeyError, PermissionError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        APPROVAL_DECISIONS.labels("approved" if request.approved else "rejected").inc()
        return {
            "conversation_id": approval["conversation_id"],
            "answer": f"审批 {approval_id} 已{'批准' if request.approved else '拒绝'}。",
            "approval": decided,
            "tool_results": [],
        }
    try:
        result = resume_copilot(
            approval["task_id"],
            request.approved,
            auth.user_id,
            request.reason,
        )
    except (KeyError, PermissionError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    APPROVAL_DECISIONS.labels("approved" if request.approved else "rejected").inc()
    return result


@app.get("/api/memory")
def memory(auth: AuthContext = Depends(get_auth_context)):
    return list_memory_writes(auth.tenant_id, 50)
