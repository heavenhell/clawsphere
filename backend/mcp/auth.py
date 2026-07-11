from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Header, HTTPException


JWT_ALGORITHM = "HS256"
JWT_SECRET = os.getenv("DCS_JWT_SECRET", "clawsphere-local-demo-secret-change-me-now")
ALLOW_ANONYMOUS = os.getenv("DCS_ALLOW_ANONYMOUS", "true").lower() == "true"


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


def get_auth_context(authorization: str | None = Header(default=None)) -> AuthContext:
    if authorization and authorization.startswith("Bearer "):
        return decode_token(authorization[7:])
    if ALLOW_ANONYMOUS:
        return AuthContext("demo-user", ["readonly"], "demo-tenant")
    raise HTTPException(status_code=401, detail="需要 Bearer 访问令牌")
