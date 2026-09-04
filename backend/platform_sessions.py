from __future__ import annotations

import threading
import time
import hashlib
import logging
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from backend.adapters.edme import acquire_edme_session
from backend.mcp.auth import AuthContext, issue_platform_delegation_token
from backend.platform_config import PlatformCredentials
from backend.providers import runtime_config
from backend.observability import MCP_CLIENT_EVENTS


SessionLogin = Callable[[PlatformCredentials, str, str], tuple[str, int]]
LOGGER = logging.getLogger(__name__)


class PlatformSessionRateLimitError(RuntimeError):
    pass


class PlatformSessionRefreshError(RuntimeError):
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
        self._condition = threading.Condition(self._lock)
        self._sessions: dict[tuple[str, str, str], ClientPlatformSession] = {}
        self._last_login: dict[tuple[str, str, str], float] = {}
        self._inflight: dict[tuple[str, str, str], int] = {}
        self._refreshing: dict[tuple[str, str, str], int] = {}
        # Cooldown state deliberately retains only a timestamp. Keeping the
        # original exception would retain its traceback (and potentially
        # vendor response data) for the process lifetime.
        self._refresh_errors: dict[tuple[str, str, str], float] = {}
        self._versions: dict[tuple[str, str, str], int] = {}
        self._version_counter = 0

    @staticmethod
    def _key(auth: AuthContext, platform_id: str = "edme") -> tuple[str, str, str]:
        return auth.user_id, auth.tenant_id, platform_id

    @staticmethod
    def _identity_fingerprint(auth: AuthContext) -> str:
        value = f"{auth.user_id}|{auth.tenant_id}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()[:16]

    def _record_refresh_event(self, event: str, auth: AuthContext, exc: Exception | None = None) -> None:
        MCP_CLIENT_EVENTS.labels(event).inc()
        LOGGER.info(
            "platform_session event=%s platform=edme identity=%s error_type=%s",
            event,
            self._identity_fingerprint(auth),
            type(exc).__name__ if exc else "none",
        )

    def _next_version_locked(self, key: tuple[str, str, str]) -> int:
        self._version_counter += 1
        self._versions[key] = self._version_counter
        return self._version_counter

    def _cleanup_locked(self, now: float) -> None:
        expiry_cutoff = int(now) + 5
        for key, session in list(self._sessions.items()):
            if session.expires_at <= expiry_cutoff:
                self._sessions.pop(key, None)
                self._prune_version_locked(key)
        for key, attempted_at in list(self._last_login.items()):
            if now - attempted_at >= self._min_login_interval_seconds:
                self._last_login.pop(key, None)
        for key, failed_at in list(self._refresh_errors.items()):
            if now - failed_at >= self._min_login_interval_seconds:
                self._refresh_errors.pop(key, None)

    def _prune_version_locked(self, key: tuple[str, str, str]) -> None:
        if (
            key not in self._sessions
            and key not in self._inflight
            and key not in self._refreshing
            and key not in self._refresh_errors
        ):
            self._versions.pop(key, None)

    def _wait_for_newer_operation_locked(
        self,
        key: tuple[str, str, str],
        operation_version: int,
    ) -> None:
        while (
            self._inflight.get(key) not in {None, operation_version}
            or self._refreshing.get(key) not in {None, operation_version}
        ):
            self._condition.wait()

    def _new_session(self, auth: AuthContext, access_session: str, lifetime: int) -> ClientPlatformSession:
        lifetime_seconds = min(int(lifetime), 86400)
        if lifetime_seconds <= 0:
            raise RuntimeError("eDME returned an invalid session lifetime")
        return ClientPlatformSession(
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            platform_id="edme",
            access_session=access_session,
            session_id=f"edme-{uuid4()}",
            expires_at=int(self._clock()) + lifetime_seconds,
        )

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
        with self._condition:
            self._cleanup_locked(now)
            previous = self._last_login.get(key)
            if (
                key in self._inflight
                or previous is not None
                and now - previous < self._min_login_interval_seconds
            ):
                raise PlatformSessionRateLimitError("eDME 登录请求过于频繁，请稍后重试")
            if (
                key not in self._sessions
                and key not in self._inflight
                and key not in self._refreshing
                and len(self._sessions) + len(self._inflight) + len(self._refreshing) >= self._max_sessions
            ):
                raise RuntimeError("Agent 平台会话容量已满")
            self._last_login[key] = now
            operation_version = self._next_version_locked(key)
            self._inflight[key] = operation_version
        try:
            access_session, lifetime = self._login(self._edme_config, username, password)
            session = self._new_session(auth, access_session, lifetime)
        except Exception:
            with self._condition:
                if self._inflight.get(key) == operation_version:
                    self._inflight.pop(key, None)
                self._prune_version_locked(key)
                self._condition.notify_all()
            raise
        with self._condition:
            if self._inflight.get(key) == operation_version:
                self._inflight.pop(key, None)
            if self._versions.get(key) == operation_version:
                self._sessions[key] = session
                self._refresh_errors.pop(key, None)
                selected = session
            else:
                self._wait_for_newer_operation_locked(key, operation_version)
                selected = self._sessions.get(key)
                self._prune_version_locked(key)
            self._condition.notify_all()
        if selected is None:
            raise PlatformSessionRefreshError("eDME 登录结果已被连接重置丢弃")
        return selected

    def get(self, auth: AuthContext) -> ClientPlatformSession | None:
        key = self._key(auth)
        with self._lock:
            now = self._clock()
            self._cleanup_locked(now)
            session = self._sessions.get(key)
            if session and session.expires_at > int(now) + 5:
                return session
            self._sessions.pop(key, None)
            self._prune_version_locked(key)
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
        if self._edme_config.bootstrap_login is None:
            return None
        return self.refresh(auth)

    def refresh(
        self,
        auth: AuthContext,
        stale_session_id: str | None = None,
    ) -> ClientPlatformSession:
        """Refresh one bootstrap identity, coalescing concurrent attempts."""
        key = self._key(auth)
        now = self._clock()
        login = self._edme_config.bootstrap_login
        failure_without_login = False
        with self._condition:
            self._cleanup_locked(now)
            current = self._sessions.get(key)
            if stale_session_id and current and current.session_id != stale_session_id:
                return current
            cached_error = self._refresh_errors.get(key)
            if cached_error is not None and now - cached_error < self._min_login_interval_seconds:
                raise PlatformSessionRefreshError("eDME 会话刷新暂时失败")
            observed_version = self._versions.get(key)
            waited = False
            while key in self._refreshing or key in self._inflight:
                if not waited:
                    self._record_refresh_event("platform_auth_refresh_wait", auth)
                    waited = True
                self._condition.wait()
                current = self._sessions.get(key)
                if current and (not stale_session_id or current.session_id != stale_session_id):
                    return current
                cached_error = self._refresh_errors.get(key)
                if cached_error is not None:
                    raise PlatformSessionRefreshError("eDME 会话刷新失败")
                if self._versions.get(key) != observed_version:
                    self._prune_version_locked(key)
                    raise PlatformSessionRefreshError("eDME 连接已被重置")
            if login is None:
                self._sessions.pop(key, None)
                self._refresh_errors.pop(key, None)
                self._next_version_locked(key)
                self._prune_version_locked(key)
                self._condition.notify_all()
                failure_without_login = True
            else:
                self._cleanup_locked(self._clock())
                if (
                    key not in self._sessions
                    and len(self._sessions) + len(self._inflight) + len(self._refreshing)
                    >= self._max_sessions
                ):
                    raise PlatformSessionRefreshError("Agent 平台会话容量已满")
                operation_version = self._next_version_locked(key)
                self._refreshing[key] = operation_version
        self._record_refresh_event("platform_auth_refresh_attempt", auth)
        if failure_without_login:
            error = PlatformSessionRefreshError("eDME 需要当前用户重新登录")
            self._record_refresh_event("platform_auth_refresh_failed", auth, error)
            raise error
        try:
            assert login is not None
            access_session, lifetime = self._login(self._edme_config, *login)
            session = self._new_session(auth, access_session, lifetime)
        except Exception as exc:
            with self._condition:
                if self._refreshing.get(key) == operation_version:
                    self._refreshing.pop(key, None)
                if self._versions.get(key) == operation_version:
                    self._sessions.pop(key, None)
                    self._refresh_errors[key] = self._clock()
                self._prune_version_locked(key)
                self._condition.notify_all()
            self._record_refresh_event("platform_auth_refresh_failed", auth, exc)
            raise PlatformSessionRefreshError("eDME 会话刷新失败") from exc
        with self._condition:
            if self._refreshing.get(key) == operation_version:
                self._refreshing.pop(key, None)
            if self._versions.get(key) == operation_version:
                self._sessions[key] = session
                self._refresh_errors.pop(key, None)
                selected = session
            else:
                self._wait_for_newer_operation_locked(key, operation_version)
                selected = self._sessions.get(key)
                self._prune_version_locked(key)
            self._condition.notify_all()
        if selected is None:
            error = PlatformSessionRefreshError("eDME 连接已被重置")
            self._record_refresh_event("platform_auth_refresh_failed", auth, error)
            raise error
        self._record_refresh_event("platform_auth_refresh_succeeded", auth)
        return selected

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
            self._edme_config.endpoint_fingerprint(26335),
        )
        return session.session_id, token

    def revoke(self, auth: AuthContext) -> bool:
        with self._condition:
            key = self._key(auth)
            revoked = (
                self._sessions.pop(key, None) is not None
                or key in self._inflight
                or key in self._refreshing
            )
            self._refresh_errors.pop(key, None)
            self._last_login.pop(key, None)
            self._next_version_locked(key)
            self._prune_version_locked(key)
            self._condition.notify_all()
            return revoked

    def clear(self) -> None:
        with self._condition:
            active = set(self._inflight) | set(self._refreshing)
            for key in active:
                self._next_version_locked(key)
            self._sessions.clear()
            self._last_login.clear()
            self._inflight.clear()
            self._refreshing.clear()
            self._refresh_errors.clear()
            self._versions = {key: self._versions[key] for key in active}
            self._condition.notify_all()


platform_session_broker = PlatformSessionBroker(runtime_config.edme)
