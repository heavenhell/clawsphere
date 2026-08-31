from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from backend.adapters.edme import acquire_edme_session
from backend.mcp.auth import AuthContext, issue_platform_delegation_token
from backend.platform_config import PlatformCredentials
from backend.providers import runtime_config


SessionLogin = Callable[[PlatformCredentials, str, str], tuple[str, int]]


class PlatformSessionRateLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClientPlatformSession:
    user_id: str
    tenant_id: str
    platform_id: str
    access_session: str
    session_id: str
    expires_at: int

    def public_status(self) -> dict[str, str | int | bool]:
        return {
            "platform_id": self.platform_id,
            "connected": True,
            "session_id": self.session_id,
            "expires_at": self.expires_at,
        }


class PlatformSessionBroker:
    """Agent-side, memory-only platform session broker.

    Raw business credentials exist only in ``register_edme`` arguments while
    the login exchange is running. They are never placed in broker state.
    """

    def __init__(
        self,
        edme_config: PlatformCredentials,
        login: SessionLogin = acquire_edme_session,
        clock: Callable[[], float] = time.time,
        max_sessions: int = 1024,
        min_login_interval_seconds: float = 2.0,
    ) -> None:
        self._edme_config = edme_config
        self._login = login
        self._clock = clock
        self._max_sessions = max_sessions
        self._min_login_interval_seconds = min_login_interval_seconds
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str, str], ClientPlatformSession] = {}
        self._last_login: dict[tuple[str, str, str], float] = {}
        self._inflight: set[tuple[str, str, str]] = set()
        self._last_bootstrap_error: Exception | None = None

    @staticmethod
    def _key(auth: AuthContext, platform_id: str = "edme") -> tuple[str, str, str]:
        return auth.user_id, auth.tenant_id, platform_id

    def register_edme(
        self,
        auth: AuthContext,
        username: str,
        password: str,
    ) -> ClientPlatformSession:
        if not self._edme_config.client_delegated:
            raise RuntimeError("eDME 未配置为客户端委托鉴权模式")
        key = self._key(auth)
        now = self._clock()
        with self._lock:
            expired = [item for item, session in self._sessions.items() if session.expires_at <= int(now) + 5]
            for item in expired:
                self._sessions.pop(item, None)
                self._last_login.pop(item, None)
            previous = self._last_login.get(key)
            if previous is not None and now - previous < self._min_login_interval_seconds:
                raise PlatformSessionRateLimitError("eDME 登录请求过于频繁，请稍后重试")
            if (
                key not in self._sessions
                and key not in self._inflight
                and len(self._sessions) + len(self._inflight) >= self._max_sessions
            ):
                raise RuntimeError("Agent 平台会话容量已满")
            self._last_login[key] = now
            self._inflight.add(key)
        try:
            access_session, lifetime = self._login(self._edme_config, username, password)
            now = int(self._clock())
            session = ClientPlatformSession(
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
                platform_id="edme",
                access_session=access_session,
                session_id=f"edme-{uuid4()}",
                expires_at=now + max(60, min(int(lifetime), 86400)),
            )
        except Exception:
            with self._lock:
                self._inflight.discard(key)
            raise
        with self._lock:
            self._inflight.discard(key)
            self._sessions[key] = session
        return session

    def get(self, auth: AuthContext) -> ClientPlatformSession | None:
        key = self._key(auth)
        with self._lock:
            session = self._sessions.get(key)
            if session and session.expires_at > int(self._clock()) + 5:
                return session
            self._sessions.pop(key, None)
        return None

    def ensure_session(self, auth: AuthContext) -> ClientPlatformSession | None:
        """Return this identity's session, logging in from configuration if needed.

        Single-tenant deployments have no per-user login yet, so the one
        tenant's credential is filled into `config/platforms.json` by hand.
        Seeding the broker from it keeps the delegation chain exactly as it
        will be with real logins — Agent exchanges the credential, signs a
        delegation token, and the MCP Server still stores nothing. Only the
        source of the credential changes when per-user login arrives, and a
        session registered through `register_edme` already takes precedence.
        """
        session = self.get(auth)
        if session is not None:
            return session
        login = self._edme_config.bootstrap_login
        if login is None:
            return None
        try:
            session = self.register_edme(auth, *login)
        except PlatformSessionRateLimitError:
            # The bootstrap credential never changes between attempts, so the
            # backpressure is hiding the reason the last login failed rather
            # than protecting against a caller that could succeed by waiting.
            if self._last_bootstrap_error is not None:
                raise self._last_bootstrap_error
            raise
        except Exception as exc:
            self._last_bootstrap_error = exc
            raise
        self._last_bootstrap_error = None
        return session

    def delegation(self, auth: AuthContext) -> tuple[str, str] | None:
        session = self.ensure_session(auth)
        if session is None:
            return None
        token = issue_platform_delegation_token(
            auth,
            session.platform_id,
            session.access_session,
            session.session_id,
            session.expires_at,
        )
        return session.session_id, token

    def revoke(self, auth: AuthContext) -> bool:
        with self._lock:
            return self._sessions.pop(self._key(auth), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._last_login.clear()
            self._inflight.clear()
            self._last_bootstrap_error = None


platform_session_broker = PlatformSessionBroker(runtime_config.edme)
