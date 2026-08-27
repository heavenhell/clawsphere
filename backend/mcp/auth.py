from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Header, HTTPException


JWT_ALGORITHM = "HS256"
MCP_CALLER_TOKEN_AUDIENCE = "clawsphere-mcp-server"
MCP_CALLER_TOKEN_ISSUER = "clawsphere-agent"
MCP_CALLER_TOKEN_META_KEY = "com.clawsphere/caller-token"
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
JWT_SECRET = os.getenv("DCS_JWT_SECRET")
if not JWT_SECRET:
    if DEMO_MODE:
        JWT_SECRET = "clawsphere-demo-only-secret-do-not-use-production"
    else:
        raise RuntimeError("DCS_JWT_SECRET is required when DEMO_MODE=false")
if not DEMO_MODE and len(JWT_SECRET) < 32:
    raise RuntimeError("DCS_JWT_SECRET must contain at least 32 characters")
ALLOW_ANONYMOUS = DEMO_MODE and os.getenv("DCS_ALLOW_ANONYMOUS", "true").lower() == "true"


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    roles: list[str]
    tenant_id: str


def issue_demo_token(user_id: str = "demo-user", roles: list[str] | None = None, tenant_id: str = "demo-tenant") -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user_id,
            "roles": roles or ["readonly"],
            "tenant_id": tenant_id,
            "iat": now,
            "exp": now + timedelta(hours=8),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def decode_token(token: str) -> AuthContext:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="无效或已过期的访问令牌") from exc
    roles = payload.get("roles") or ["readonly"]
    if not isinstance(roles, list) or not set(roles) <= {"readonly", "ops", "admin"}:
        raise HTTPException(status_code=403, detail="令牌角色无效")
    return AuthContext(
        user_id=str(payload.get("sub") or "unknown"),
        roles=roles,
        tenant_id=str(payload.get("tenant_id") or "default"),
    )


def issue_mcp_caller_token(auth: AuthContext, task_id: str, ttl_seconds: int = 120) -> str:
    """Issue a short-lived, Agent-only caller envelope for MCP request metadata.

    This token carries ClawSphere caller identity and the server-side approval
    task binding. Device credentials are deliberately excluded and remain owned
    by the MCP Server's configured providers.
    """
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": auth.user_id,
            "roles": auth.roles,
            "tenant_id": auth.tenant_id,
            "task_id": task_id,
            "iss": MCP_CALLER_TOKEN_ISSUER,
            "aud": MCP_CALLER_TOKEN_AUDIENCE,
            "iat": now,
            "exp": now + timedelta(seconds=ttl_seconds),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def decode_mcp_caller_token(token: str) -> tuple[AuthContext, str]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=MCP_CALLER_TOKEN_AUDIENCE,
            issuer=MCP_CALLER_TOKEN_ISSUER,
        )
    except jwt.PyJWTError as exc:
        raise PermissionError("无效或已过期的 MCP 调用者令牌") from exc
    roles = payload.get("roles")
    if not isinstance(roles, list) or not roles or not set(roles) <= {"readonly", "ops", "admin"}:
        raise PermissionError("MCP 调用者令牌角色无效")
    task_id = str(payload.get("task_id") or "")
    if not 8 <= len(task_id) <= 128:
        raise PermissionError("MCP 调用者令牌缺少有效 task_id")
    return (
        AuthContext(
            user_id=str(payload.get("sub") or "unknown"),
            roles=roles,
            tenant_id=str(payload.get("tenant_id") or "default"),
        ),
        task_id,
    )


def get_auth_context(authorization: str | None = Header(default=None)) -> AuthContext:
    if authorization and authorization.startswith("Bearer "):
        return decode_token(authorization[7:])
    if ALLOW_ANONYMOUS:
        return AuthContext("demo-user", ["readonly"], "demo-tenant")
    raise HTTPException(status_code=401, detail="需要 Bearer 访问令牌")


def get_mcp_auth_context() -> AuthContext:
    token = os.getenv("MCP_AUTH_TOKEN")
    if token:
        return decode_token(token)
    if DEMO_MODE:
        roles = [role.strip() for role in os.getenv("MCP_CALLER_ROLES", "readonly").split(",") if role.strip()]
        return AuthContext(
            os.getenv("MCP_CALLER_USER_ID", "mcp-demo-user"),
            roles,
            os.getenv("MCP_CALLER_TENANT_ID", "demo-tenant"),
        )
    raise RuntimeError("MCP_AUTH_TOKEN is required when DEMO_MODE=false")
