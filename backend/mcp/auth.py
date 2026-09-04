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
MCP_PLATFORM_CREDENTIAL_HEADER = "X-ClawSphere-Platform-Credential"
MCP_PLATFORM_TOKEN_AUDIENCE = "clawsphere-mcp-platform-delegation"
MCP_PLATFORM_TOKEN_ISSUER = "clawsphere-agent-platform-broker"
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
MCP_DELEGATION_SECRET = os.getenv("DCS_MCP_DELEGATION_SECRET")
if not MCP_DELEGATION_SECRET:
    if DEMO_MODE:
        MCP_DELEGATION_SECRET = "clawsphere-demo-platform-delegation-secret"
    else:
        raise RuntimeError("DCS_MCP_DELEGATION_SECRET is required when DEMO_MODE=false")
if not DEMO_MODE and len(MCP_DELEGATION_SECRET) < 32:
    raise RuntimeError("DCS_MCP_DELEGATION_SECRET must contain at least 32 characters")


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    roles: list[str]
    tenant_id: str


@dataclass(frozen=True)
class PlatformDelegation:
    auth: AuthContext
    platform_id: str
    access_session: str
    session_id: str
    expires_at: int
    endpoint_fingerprint: str


class PlatformDelegationExpiredError(PermissionError):
    """A correctly transported delegation JWT has crossed its expiry."""


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


def issue_platform_delegation_token(
    auth: AuthContext,
    platform_id: str,
    access_session: str,
    session_id: str,
    expires_at: int,
    endpoint_fingerprint: str,
) -> str:
    """Wrap a platform session for one authenticated user and tenant.

    This JWT is signed, not encrypted. Its confidentiality therefore depends
    on TLS (or a loopback-only MCP endpoint), which the client gateway enforces.
    """
    now = datetime.now(timezone.utc)
    if not access_session or len(access_session) > 8192:
        raise ValueError("invalid platform access session")
    if expires_at <= int(now.timestamp()):
        raise ValueError("platform access session has expired")
    if len(endpoint_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in endpoint_fingerprint
    ):
        raise ValueError("invalid platform endpoint fingerprint")
    return jwt.encode(
        {
            "sub": auth.user_id,
            "tenant_id": auth.tenant_id,
            "platform_id": platform_id,
            "access_session": access_session,
            "session_id": session_id,
            "endpoint_fingerprint": endpoint_fingerprint,
            "iss": MCP_PLATFORM_TOKEN_ISSUER,
            "aud": MCP_PLATFORM_TOKEN_AUDIENCE,
            "iat": now,
            "exp": expires_at,
        },
        MCP_DELEGATION_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def decode_platform_delegation_token(
    token: str,
    expected_auth: AuthContext | None = None,
    expected_endpoint_fingerprint: str | None = None,
) -> PlatformDelegation:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            MCP_DELEGATION_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=MCP_PLATFORM_TOKEN_AUDIENCE,
            issuer=MCP_PLATFORM_TOKEN_ISSUER,
            options={"require": [
                "sub", "tenant_id", "platform_id", "access_session", "session_id",
                "endpoint_fingerprint", "exp",
            ]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise PlatformDelegationExpiredError("平台委托令牌已过期") from exc
    except jwt.PyJWTError as exc:
        raise PermissionError("无效或已过期的平台委托令牌") from exc
    auth = AuthContext(
        user_id=str(payload.get("sub") or ""),
        roles=list(expected_auth.roles) if expected_auth else ["readonly"],
        tenant_id=str(payload.get("tenant_id") or ""),
    )
    if expected_auth and (
        auth.user_id != expected_auth.user_id or auth.tenant_id != expected_auth.tenant_id
    ):
        raise PermissionError("平台委托令牌与 MCP 调用者身份不匹配")
    platform_id = str(payload.get("platform_id") or "")
    access_session = str(payload.get("access_session") or "")
    session_id = str(payload.get("session_id") or "")
    endpoint_fingerprint = str(payload.get("endpoint_fingerprint") or "")
    if platform_id != "edme" or not access_session or len(access_session) > 8192:
        raise PermissionError("平台委托令牌内容无效")
    if not 8 <= len(session_id) <= 128:
        raise PermissionError("平台委托令牌缺少有效 session_id")
    if len(endpoint_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in endpoint_fingerprint
    ):
        raise PermissionError("平台委托令牌缺少有效 endpoint 指纹")
    if expected_endpoint_fingerprint and endpoint_fingerprint != expected_endpoint_fingerprint:
        raise PermissionError("平台委托令牌目标与 MCP Server eDME 配置不匹配")
    return PlatformDelegation(
        auth=auth,
        platform_id=platform_id,
        access_session=access_session,
        session_id=session_id,
        expires_at=int(payload["exp"]),
        endpoint_fingerprint=endpoint_fingerprint,
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
