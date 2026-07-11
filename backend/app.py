from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.agent.copilot import run_copilot
from backend.memory.store import MEMORY_WRITES
from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import APPROVAL_QUEUE, AUDIT_LOG, TOOL_REGISTRY, call_tool
from backend.mock.repository import repo


class ChatRequest(BaseModel):
    message: str
    roles: list[str] = ["readonly"]
    history: list[dict[str, str]] = []
    summary: str = ""


app = FastAPI(title="DCS Copilot Demo")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5174", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        }
        for spec in TOOL_REGISTRY.values()
    ]


@app.post("/api/tools/call")
def tool_call(request: ToolRequest):
    return call_tool(request)


@app.post("/api/chat")
def chat(request: ChatRequest):
    return run_copilot(request.message, request.roles, request.history, request.summary)


@app.get("/api/audit")
def audit():
    return AUDIT_LOG[-50:]


@app.get("/api/approvals")
def approvals():
    return APPROVAL_QUEUE


@app.get("/api/memory")
def memory():
    return MEMORY_WRITES[-50:]
