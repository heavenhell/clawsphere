from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.app import app
from backend.mcp.auth import (
    MCP_DELEGATION_SECRET,
    MCP_PLATFORM_TOKEN_AUDIENCE,
    MCP_PLATFORM_TOKEN_ISSUER,
    MCP_PLATFORM_CREDENTIAL_HEADER,
    AuthContext,
    PlatformDelegationExpiredError,
    decode_platform_delegation_token,
    issue_mcp_caller_token,
    issue_platform_delegation_token,
)
import backend.mcp.gateway as gateway_module
from backend.mcp.gateway import (
    GatewayTool,
    IdentityScopedMcpGateway,
    McpUnavailableError,
    ToolCatalogSnapshot,
)
from backend.mcp.schemas import ToolRequest, ToolResponse
from backend.mcp.tools import TOOL_REGISTRY
from backend.mcp import mcp_server
from backend.platform_config import PlatformCredentials, load_runtime_config
from backend.platform_sessions import (
    PlatformSessionBroker,
    PlatformSessionRateLimitError,
    PlatformSessionRefreshError,
)


client = TestClient(app)
ENDPOINT_FINGERPRINT = PlatformCredentials(ip="edme.example.test").endpoint_fingerprint(26335)


def test_client_delegated_config_has_endpoint_but_no_persisted_credentials(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {"auth_mode": "client", "ip": "edme.example.test"},
    }), encoding="utf-8")

    config = load_runtime_config(path)

    assert config.edme.client_delegated
    assert not config.edme.configured
    assert config.real_platforms == ["edme"]
    assert config.mcp.agent_mode == "mcp"
    assert not config.expose_mock_api


def test_client_delegation_is_rejected_for_unsupported_platform(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "fusioncompute": {"auth_mode": "client", "ip": "fc.example.test"},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="only for eDME"):
        load_runtime_config(path)


@pytest.mark.parametrize("unsafe_endpoint", [
    "http://edme.example.test",
    "https://user:password@edme.example.test",
    "https://edme.example.test/custom/path",
    "https://edme.example.test?target=other",
])
def test_client_delegated_config_rejects_unsafe_edme_endpoint(tmp_path, unsafe_endpoint):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {"auth_mode": "client", "ip": unsafe_endpoint},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="client-delegated eDME endpoint"):
        load_runtime_config(path)


def test_client_delegated_config_allows_loopback_http_for_local_mock(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {"auth_mode": "client", "ip": "http://127.0.0.1:8010"},
    }), encoding="utf-8")
    assert load_runtime_config(path).edme.client_delegated


@pytest.mark.parametrize("field,value", [
    ("username", "tenant-user"),
    ("password", "secret"),
    ("session", "access-session"),
])
def test_client_delegated_config_rejects_persisted_credentials(tmp_path, field, value):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {"auth_mode": "client", "ip": "edme.example.test", field: value},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="must not store"):
        load_runtime_config(path)


def test_broker_keeps_only_session_and_isolates_user_and_tenant():
    captured: dict[str, str] = {}

    def login(_config, username, password):
        captured.update(username=username, password=password)
        return "access-session-1", 600

    broker = PlatformSessionBroker(
        PlatformCredentials(auth_mode="client", ip="edme.example.test"),
        login=login,
        clock=lambda: 1000,
    )
    owner = AuthContext("user-1", ["readonly"], "tenant-1")
    session = broker.register_edme(owner, "business-user", "business-password")

    assert captured == {"username": "business-user", "password": "business-password"}
    assert session.access_session == "access-session-1"
    assert not hasattr(session, "username")
    assert not hasattr(session, "password")
    assert broker.get(owner) == session
    assert broker.get(AuthContext("user-2", ["readonly"], "tenant-1")) is None
    assert broker.get(AuthContext("user-1", ["readonly"], "tenant-2")) is None


def test_broker_rate_limits_repeated_login_for_same_identity():
    broker = PlatformSessionBroker(
        PlatformCredentials(auth_mode="client", ip="edme.example.test"),
        login=lambda *_args: ("access-session", 600),
        clock=lambda: 1000,
    )
    auth = AuthContext("user-rate", ["readonly"], "tenant-rate")
    broker.register_edme(auth, "user", "password")
    with pytest.raises(PlatformSessionRateLimitError, match="过于频繁"):
        broker.register_edme(auth, "user", "password")


def test_bootstrap_seeds_the_delegation_chain_from_configuration():
    """Single tenant, no per-user login yet: the credential comes from config.

    What must not change is everything downstream — the session still lives
    only in the broker, still becomes a signed delegation token, and the MCP
    Server still stores no platform credential. Only where the login came from
    differs, so switching to per-user login later touches nothing else.
    """
    logins: list[tuple[str, str]] = []

    def login(_config, username, password):
        logins.append((username, password))
        return "bootstrap-session", 600

    broker = PlatformSessionBroker(
        PlatformCredentials(
            auth_mode="client",
            single_tenant_bootstrap=True,
            ip="edme.example.test",
            username="northbound-user",
            password="northbound-password",
        ),
        login=login,
    )
    auth = AuthContext("demo-user", ["readonly"], "demo-tenant")

    assert broker.get(auth) is None
    session = broker.ensure_session(auth)
    assert session is not None
    assert session.access_session == "bootstrap-session"
    assert logins == [("northbound-user", "northbound-password")]

    # A cached session is reused rather than re-exchanged on every request.
    assert broker.ensure_session(auth) == session
    assert len(logins) == 1

    session_id, token = broker.delegation(auth)
    assert session_id == session.session_id
    assert decode_platform_delegation_token(token, auth).access_session == "bootstrap-session"


def test_bootstrap_does_not_leak_across_identities():
    """Each identity gets its own session even when they share one credential.

    The keying is what makes per-user login a drop-in replacement later, so it
    has to hold now rather than being introduced together with the login.
    """
    counter = {"value": 0}

    def login(_config, _username, _password):
        counter["value"] += 1
        return f"bootstrap-{counter['value']}", 600

    broker = PlatformSessionBroker(
        PlatformCredentials(
            auth_mode="client", single_tenant_bootstrap=True,
            ip="edme.example.test", username="u", password="p",
        ),
        login=login,
        clock=lambda: 1000,
    )
    first = broker.ensure_session(AuthContext("user-1", ["readonly"], "tenant-1"))
    second = broker.ensure_session(AuthContext("user-2", ["readonly"], "tenant-1"))
    assert first.access_session == "bootstrap-1"
    assert second.access_session == "bootstrap-2"


def test_a_per_user_login_takes_precedence_over_the_bootstrap():
    """The migration path: once a real login lands, it wins for that identity."""
    broker = PlatformSessionBroker(
        PlatformCredentials(
            auth_mode="client", single_tenant_bootstrap=True,
            ip="edme.example.test", username="shared", password="shared-password",
        ),
        login=lambda _config, username, _password: (f"session-for-{username}", 600),
        clock=lambda: 1000,
    )
    auth = AuthContext("user-1", ["readonly"], "tenant-1")
    broker.register_edme(auth, "alice", "alice-password")
    assert broker.ensure_session(auth).access_session == "session-for-alice"


def test_ensure_session_stays_none_without_a_bootstrap_credential():
    """Plain client delegation is unchanged: no session until the user logs in."""
    broker = PlatformSessionBroker(
        PlatformCredentials(auth_mode="client", ip="edme.example.test"),
        login=lambda *_args: ("unexpected", 600),
        clock=lambda: 1000,
    )
    assert broker.ensure_session(AuthContext("user-1", ["readonly"], "tenant-1")) is None


def test_a_failing_bootstrap_reports_the_login_error_not_the_rate_limit():
    """A wrong password in the config file must say so on every request.

    `register_edme` records the attempt before it runs, so the second request
    would otherwise hit the 2-second backpressure and report "登录请求过于频繁"
    — advice to wait, for a credential that will never work until it is edited.
    """
    def login(*_args):
        raise RuntimeError("eDME 业务账号或密码无效")

    broker = PlatformSessionBroker(
        PlatformCredentials(
            auth_mode="client", single_tenant_bootstrap=True,
            ip="edme.example.test", username="u", password="wrong",
        ),
        login=login,
        clock=lambda: 1000,
    )
    auth = AuthContext("demo-user", ["readonly"], "demo-tenant")
    with pytest.raises(RuntimeError, match="会话刷新失败"):
        broker.ensure_session(auth)
    with pytest.raises(RuntimeError, match="会话刷新暂时失败"):
        broker.ensure_session(auth)


def test_platform_delegation_is_bound_to_caller_identity():
    owner = AuthContext("user-1", ["ops"], "tenant-1")
    token = issue_platform_delegation_token(
        owner, "edme", "access-session", "edme-session-123", 4102444800,
        ENDPOINT_FINGERPRINT,
    )

    decoded = decode_platform_delegation_token(token, owner)
    assert decoded.auth == owner
    assert decoded.access_session == "access-session"
    with pytest.raises(PermissionError, match="身份不匹配"):
        decode_platform_delegation_token(
            token, AuthContext("user-1", ["ops"], "tenant-2")
        )


def test_broker_caps_session_lifetime_and_delegation_expiry_together():
    now = int(time.time())
    broker = PlatformSessionBroker(
        PlatformCredentials(auth_mode="client", ip="edme.example.test"),
        login=lambda *_args: ("long-lived-access", 7 * 86400),
        clock=lambda: now,
    )
    auth = AuthContext("user-expiry", ["readonly"], "tenant-expiry")

    session = broker.register_edme(auth, "user", "password")
    _, token = broker.delegation(auth)
    delegation = decode_platform_delegation_token(token, auth)

    assert session.expires_at == now + 86400
    assert delegation.expires_at == session.expires_at


def test_expired_platform_delegation_has_a_distinct_error():
    auth = AuthContext("user-expired", ["readonly"], "tenant-expired")
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    token = jwt.encode(
        {
            "sub": auth.user_id,
            "tenant_id": auth.tenant_id,
            "platform_id": "edme",
            "access_session": "expired-access",
            "session_id": "edme-session-expired",
            "endpoint_fingerprint": ENDPOINT_FINGERPRINT,
            "iss": MCP_PLATFORM_TOKEN_ISSUER,
            "aud": MCP_PLATFORM_TOKEN_AUDIENCE,
            "iat": expired_at - timedelta(minutes=1),
            "exp": expired_at,
        },
        MCP_DELEGATION_SECRET,
        algorithm="HS256",
    )

    with pytest.raises(PlatformDelegationExpiredError):
        decode_platform_delegation_token(token, auth, ENDPOINT_FINGERPRINT)


def test_platform_delegation_is_bound_to_normalized_endpoint():
    auth = AuthContext("user-endpoint", ["readonly"], "tenant-endpoint")
    token = issue_platform_delegation_token(
        auth, "edme", "access-session", "edme-session-endpoint", 4102444800,
        ENDPOINT_FINGERPRINT,
    )

    decoded = decode_platform_delegation_token(token, auth, ENDPOINT_FINGERPRINT)
    assert decoded.endpoint_fingerprint == ENDPOINT_FINGERPRINT
    with pytest.raises(PermissionError, match="目标"):
        decode_platform_delegation_token(token, auth, "f" * 64)


def test_endpoint_fingerprint_normalizes_case_and_default_port():
    first = PlatformCredentials(ip="https://EDME.Example.Test").endpoint_fingerprint(26335)
    second = PlatformCredentials(ip="https://edme.example.test:443").endpoint_fingerprint(26335)
    assert first == second


def test_identity_scoped_gateway_requires_session_and_uses_separate_headers():
    counter = {"value": 0}

    def login(_config, _username, _password):
        counter["value"] += 1
        return f"access-{counter['value']}", 600

    broker = PlatformSessionBroker(
        PlatformCredentials(auth_mode="client", ip="edme.example.test"), login=login
    )
    gateway = IdentityScopedMcpGateway("http://127.0.0.1:8020/mcp", broker)
    auth1 = AuthContext("user-1", ["readonly"], "tenant-1")
    auth2 = AuthContext("user-2", ["readonly"], "tenant-1")

    with pytest.raises(McpUnavailableError, match="尚未连接 eDME"):
        gateway._gateway_for(auth1)
    broker.register_edme(auth1, "u1", "p1")
    broker.register_edme(auth2, "u2", "p2")
    child1 = gateway._gateway_for(auth1)
    child2 = gateway._gateway_for(auth2)

    assert child1 is not child2
    token1 = child1.default_headers[MCP_PLATFORM_CREDENTIAL_HEADER]
    token2 = child2.default_headers[MCP_PLATFORM_CREDENTIAL_HEADER]
    assert decode_platform_delegation_token(token1, auth1).access_session == "access-1"
    assert decode_platform_delegation_token(token2, auth2).access_session == "access-2"


def test_identity_scoped_gateway_rejects_remote_plain_http():
    broker = PlatformSessionBroker(PlatformCredentials(auth_mode="client", ip="edme.test"))
    with pytest.raises(ValueError, match="HTTPS"):
        IdentityScopedMcpGateway("http://mcp.example.test/mcp", broker)


def test_mcp_server_reads_and_validates_platform_header(monkeypatch):
    auth = AuthContext("user-header", ["readonly"], "tenant-header")
    token = issue_platform_delegation_token(
        auth, "edme", "access-from-header", "edme-session-header", 4102444800,
        ENDPOINT_FINGERPRINT,
    )
    context = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(headers={MCP_PLATFORM_CREDENTIAL_HEADER: token})
    ))
    monkeypatch.setattr(
        mcp_server,
        "runtime_config",
        SimpleNamespace(edme=SimpleNamespace(
            client_delegated=True,
            endpoint_fingerprint=lambda _port: ENDPOINT_FINGERPRINT,
        )),
    )

    delegation = mcp_server._platform_delegation_from_context(context, auth)

    assert delegation.access_session == "access-from-header"


def test_mcp_server_fails_closed_when_client_delegation_header_is_missing(monkeypatch):
    context = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(headers={})
    ))
    monkeypatch.setattr(
        mcp_server,
        "runtime_config",
        SimpleNamespace(edme=SimpleNamespace(client_delegated=True)),
    )
    with pytest.raises(PermissionError, match="缺少 eDME"):
        mcp_server._platform_delegation_from_context(
            context, AuthContext("user", ["readonly"], "tenant")
        )


def test_mcp_server_maps_expired_delegation_to_refreshable_tool_response(monkeypatch):
    auth = AuthContext("user-mcp-expired", ["readonly"], "tenant-mcp-expired")
    task_id = "task-mcp-expired"
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    platform_token = jwt.encode(
        {
            "sub": auth.user_id,
            "tenant_id": auth.tenant_id,
            "platform_id": "edme",
            "access_session": "expired-access",
            "session_id": "edme-session-expired",
            "endpoint_fingerprint": ENDPOINT_FINGERPRINT,
            "iss": MCP_PLATFORM_TOKEN_ISSUER,
            "aud": MCP_PLATFORM_TOKEN_AUDIENCE,
            "iat": expired_at - timedelta(minutes=1),
            "exp": expired_at,
        },
        MCP_DELEGATION_SECRET,
        algorithm="HS256",
    )
    caller_token = issue_mcp_caller_token(auth, task_id)
    context = SimpleNamespace(request_context=SimpleNamespace(
        meta=SimpleNamespace(model_dump=lambda **_kwargs: {
            mcp_server.CALLER_TOKEN_META_KEY: caller_token
        }),
        request=SimpleNamespace(headers={
            MCP_PLATFORM_CREDENTIAL_HEADER: platform_token,
        }),
    ))
    monkeypatch.setattr(
        mcp_server,
        "runtime_config",
        SimpleNamespace(edme=SimpleNamespace(
            client_delegated=True,
            endpoint_fingerprint=lambda _port: ENDPOINT_FINGERPRINT,
        )),
    )

    response = mcp_server._call("query_edme_resources", {}, ctx=context)

    assert response["success"] is False
    assert response["error_code"] == "PLATFORM_AUTH_EXPIRED"


def test_mcp_server_builds_request_repository_from_validated_session(monkeypatch):
    auth = AuthContext("user-call", ["readonly"], "tenant-call")
    task_id = "task-platform-delegation"
    platform_token = issue_platform_delegation_token(
        auth, "edme", "access-for-repository", "edme-session-call", 4102444800,
        ENDPOINT_FINGERPRINT,
    )
    caller_token = issue_mcp_caller_token(auth, task_id)
    context = SimpleNamespace(request_context=SimpleNamespace(
        meta=SimpleNamespace(model_dump=lambda **_kwargs: {
            mcp_server.CALLER_TOKEN_META_KEY: caller_token
        }),
        request=SimpleNamespace(headers={MCP_PLATFORM_CREDENTIAL_HEADER: platform_token}),
    ))
    captured = {}

    class FakeRepository:
        closed = False

        def close(self):
            self.closed = True

    repository = FakeRepository()

    def build_repository(session):
        captured["session"] = session
        return repository

    def fake_call_tool(request):
        captured["request"] = request
        return SimpleNamespace(model_dump=lambda **_kwargs: {"success": True})

    monkeypatch.setattr(
        mcp_server,
        "runtime_config",
        SimpleNamespace(edme=SimpleNamespace(
            client_delegated=True,
            endpoint_fingerprint=lambda _port: ENDPOINT_FINGERPRINT,
        )),
    )
    monkeypatch.setattr(
        mcp_server,
        "delegated_edme_repository",
        lambda session, config: build_repository(session),
    )
    monkeypatch.setattr(
        mcp_server,
        "call_tool",
        fake_call_tool,
    )

    result = mcp_server._call("list_alarms", {}, ctx=context)

    assert result == {"success": True}
    assert captured["session"] == "access-for-repository"
    assert captured["request"].tenant_id == auth.tenant_id
    assert repository.closed


def test_platform_session_api_never_returns_password_or_access_session(monkeypatch):
    captured = {}

    class FakeSession:
        def public_status(self):
            return {
                "platform_id": "edme",
                "connected": True,
                "session_id": "edme-public-id",
                "expires_at": 4102444800,
            }

    class FakeBroker:
        def register_edme(self, auth, username, password):
            captured.update(auth=auth, username=username, password=password)
            return FakeSession()

    monkeypatch.setattr(app_module, "platform_session_broker", FakeBroker())
    response = client.post(
        "/api/platform-sessions/edme",
        json={"username": "business-user", "password": "business-password"},
    )

    assert response.status_code == 200
    assert captured["username"] == "business-user"
    assert captured["password"] == "business-password"
    assert captured["auth"].tenant_id == "demo-tenant"
    assert response.json()["connected"] is True
    assert "password" not in response.text
    assert "access_session" not in response.text


def test_platform_session_api_rejects_extra_endpoint_field(monkeypatch):
    class NoCallBroker:
        def register_edme(self, *_args):
            raise AssertionError("invalid request must not reach broker")

    monkeypatch.setattr(app_module, "platform_session_broker", NoCallBroker())
    response = client.post(
        "/api/platform-sessions/edme",
        json={
            "username": "business-user",
            "password": "business-password",
            "ip": "attacker.example.test",
        },
    )

    assert response.status_code == 422


def test_direct_api_helper_sets_delegated_repository_before_operation(monkeypatch):
    auth = AuthContext("api-user", ["readonly"], "api-tenant")
    session = SimpleNamespace(access_session="api-access-session")

    class FakeBroker:
        def ensure_session(self, requested_auth):
            assert requested_auth == auth
            return session

    class FakeRepository:
        closed = False

        def close(self):
            self.closed = True

    repository = FakeRepository()
    monkeypatch.setattr(
        app_module,
        "runtime_config",
        SimpleNamespace(edme=SimpleNamespace(client_delegated=True)),
    )
    monkeypatch.setattr(app_module, "platform_session_broker", FakeBroker())
    monkeypatch.setattr(
        app_module,
        "delegated_edme_repository",
        lambda access_session: repository if access_session == session.access_session else None,
    )

    selected = app_module._with_client_platform(
        auth, lambda: app_module.repo.current()
    )

    assert selected is repository
    assert repository.closed


def test_direct_overview_refreshes_once_and_retries_with_the_new_session(monkeypatch):
    auth = AuthContext("overview-user", ["readonly"], "overview-tenant")
    old = SimpleNamespace(access_session="old-access", session_id="old-session")
    new = SimpleNamespace(access_session="new-access", session_id="new-session")

    class FakeBroker:
        def ensure_session(self, requested_auth):
            assert requested_auth == auth
            return old

        def refresh(self, requested_auth, stale_session_id=None):
            assert requested_auth == auth
            assert stale_session_id == old.session_id
            return new

    class FakeRepository:
        def __init__(self, access_session):
            self.access_session = access_session
            self.closed = False

        def overview(self):
            if self.access_session == old.access_session:
                raise app_module.PlatformAuthExpiredError("expired")
            return {"vm_count": 7}

        def close(self):
            self.closed = True

    repositories = []

    def build_repository(access_session):
        repository = FakeRepository(access_session)
        repositories.append(repository)
        return repository

    monkeypatch.setattr(
        app_module,
        "runtime_config",
        SimpleNamespace(edme=SimpleNamespace(client_delegated=True)),
    )
    monkeypatch.setattr(app_module, "platform_session_broker", FakeBroker())
    monkeypatch.setattr(app_module, "delegated_edme_repository", build_repository)

    result = app_module._with_client_platform(
        auth,
        lambda: app_module.repo.overview(),
        retry_on_auth_expiry=True,
        operation_name="overview",
    )

    assert result == {"vm_count": 7}
    assert [item.access_session for item in repositories] == ["old-access", "new-access"]
    assert all(item.closed for item in repositories)


def test_direct_tool_api_uses_the_unified_gateway(monkeypatch):
    captured = {}

    class FakeGateway:
        def catalog(self, auth):
            captured["catalog_auth"] = auth
            return SimpleNamespace(version="test-catalog")

        def call(self, request, expected_catalog_version):
            captured["request"] = request
            captured["version"] = expected_catalog_version
            return _tool_response(request.tool_name, True)

    monkeypatch.setattr(app_module, "get_tool_gateway", lambda: FakeGateway())

    response = client.post(
        "/api/tools/call",
        json={"tool_name": "list_vms", "params": {}, "task_id": "task-direct-api"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert captured["version"] == "test-catalog"
    assert captured["request"].task_id == "task-direct-api"
    assert captured["catalog_auth"].tenant_id == "demo-tenant"


def test_concurrent_refresh_reuses_the_same_new_session():
    calls = {"count": 0}
    calls_lock = threading.Lock()
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    def login(_config, _username, _password):
        with calls_lock:
            calls["count"] += 1
            attempt = calls["count"]
        if attempt == 2:
            refresh_started.set()
            assert release_refresh.wait(timeout=2)
        return f"access-{attempt}", 600

    broker = PlatformSessionBroker(
        PlatformCredentials(
            auth_mode="client",
            single_tenant_bootstrap=True,
            ip="edme.example.test",
            username="bootstrap-user",
            password="bootstrap-password",
        ),
        login=login,
    )
    auth = AuthContext("single-flight-user", ["readonly"], "single-flight-tenant")
    original = broker.ensure_session(auth)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(broker.refresh, auth, original.session_id)
        assert refresh_started.wait(timeout=2)
        second = executor.submit(broker.refresh, auth, original.session_id)
        release_refresh.set()
        refreshed = first.result(timeout=2)
        coalesced = second.result(timeout=2)

    assert calls["count"] == 2
    assert refreshed.session_id == coalesced.session_id
    assert refreshed.access_session == "access-2"


def test_concurrent_refresh_failure_is_coalesced_and_cooled_down():
    calls = {"count": 0}
    calls_lock = threading.Lock()
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    def login(_config, _username, _password):
        with calls_lock:
            calls["count"] += 1
            attempt = calls["count"]
        if attempt == 1:
            return "access-1", 600
        refresh_started.set()
        assert release_refresh.wait(timeout=2)
        raise RuntimeError("vendor login unavailable")

    broker = PlatformSessionBroker(
        PlatformCredentials(
            auth_mode="client",
            single_tenant_bootstrap=True,
            ip="edme.example.test",
            username="bootstrap-user",
            password="bootstrap-password",
        ),
        login=login,
    )
    auth = AuthContext("failed-refresh-user", ["readonly"], "failed-refresh-tenant")
    original = broker.ensure_session(auth)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(broker.refresh, auth, original.session_id)
        assert refresh_started.wait(timeout=2)
        second = executor.submit(broker.refresh, auth, original.session_id)
        release_refresh.set()
        with pytest.raises(PlatformSessionRefreshError):
            first.result(timeout=2)
        with pytest.raises(PlatformSessionRefreshError):
            second.result(timeout=2)

    with pytest.raises(PlatformSessionRefreshError, match="暂时失败"):
        broker.refresh(auth, original.session_id)
    assert calls["count"] == 2


def test_new_per_user_login_wins_over_an_inflight_bootstrap_refresh():
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    calls = {"count": 0}

    def login(_config, username, _password):
        calls["count"] += 1
        if calls["count"] == 2:
            refresh_started.set()
            assert release_refresh.wait(timeout=2)
            return "stale-bootstrap", 600
        return ("bootstrap-original" if username == "bootstrap-user" else "personal-alice"), 600

    broker = PlatformSessionBroker(
        PlatformCredentials(
            auth_mode="client",
            single_tenant_bootstrap=True,
            ip="edme.example.test",
            username="bootstrap-user",
            password="bootstrap-password",
        ),
        login=login,
        min_login_interval_seconds=0,
    )
    auth = AuthContext("race-user", ["readonly"], "race-tenant")
    original = broker.ensure_session(auth)

    with ThreadPoolExecutor(max_workers=2) as executor:
        refreshing = executor.submit(broker.refresh, auth, original.session_id)
        assert refresh_started.wait(timeout=2)
        personal = broker.register_edme(auth, "alice", "alice-password")
        release_refresh.set()
        refresh_result = refreshing.result(timeout=2)

    assert personal.access_session == "personal-alice"
    assert refresh_result.session_id == personal.session_id
    assert broker.get(auth).session_id == personal.session_id


def test_no_bootstrap_refresh_reuses_a_newer_personal_session():
    now = {"value": 1000.0}
    broker = PlatformSessionBroker(
        PlatformCredentials(auth_mode="client", ip="edme.example.test"),
        login=lambda _config, username, _password: (f"personal-{username}", 600),
        clock=lambda: now["value"],
    )
    auth = AuthContext("personal-user", ["readonly"], "personal-tenant")
    stale = broker.register_edme(auth, "old", "password")
    now["value"] += 3
    current = broker.register_edme(auth, "new", "password")

    selected = broker.refresh(auth, stale.session_id)

    assert selected.session_id == current.session_id
    assert broker.get(auth).session_id == current.session_id


def test_reset_during_refresh_prevents_session_republication():
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    calls = {"count": 0}

    def login(_config, _username, _password):
        calls["count"] += 1
        if calls["count"] == 2:
            refresh_started.set()
            assert release_refresh.wait(timeout=2)
        return f"access-{calls['count']}", 600

    broker = PlatformSessionBroker(
        PlatformCredentials(
            auth_mode="client",
            single_tenant_bootstrap=True,
            ip="edme.example.test",
            username="bootstrap-user",
            password="bootstrap-password",
        ),
        login=login,
    )
    auth = AuthContext("reset-user", ["readonly"], "reset-tenant")
    original = broker.ensure_session(auth)

    with ThreadPoolExecutor(max_workers=1) as executor:
        refreshing = executor.submit(broker.refresh, auth, original.session_id)
        assert refresh_started.wait(timeout=2)
        assert broker.revoke(auth)
        release_refresh.set()
        with pytest.raises(PlatformSessionRefreshError, match="重置"):
            refreshing.result(timeout=2)

    assert broker.get(auth) is None


def test_expired_sessions_do_not_consume_bootstrap_capacity():
    now = {"value": 1000.0}
    broker = PlatformSessionBroker(
        PlatformCredentials(auth_mode="client", ip="edme.example.test"),
        login=lambda _config, username, _password: (f"access-{username}", 6),
        clock=lambda: now["value"],
        max_sessions=1,
        min_login_interval_seconds=0,
    )
    broker.register_edme(
        AuthContext("capacity-old", ["readonly"], "tenant"),
        "old",
        "password",
    )
    now["value"] += 2

    replacement = broker.register_edme(
        AuthContext("capacity-new", ["readonly"], "tenant"),
        "new",
        "password",
    )

    assert replacement.access_session == "access-new"


class _ScriptedChildGateway:
    def __init__(self, tool: GatewayTool, responses: list[ToolResponse]):
        self.snapshot = ToolCatalogSnapshot("catalog-version", (tool,), "mcp")
        self.responses = responses
        self.calls = 0

    def catalog(self, _auth):
        return self.snapshot

    def call(self, _request, _expected_catalog_version):
        self.calls += 1
        return self.responses.pop(0)


class _RefreshingBroker:
    def __init__(self):
        self.refreshes = []

    def refresh(self, auth, stale_session_id=None):
        self.refreshes.append((auth, stale_session_id))
        return SimpleNamespace(session_id="new-session")


def _gateway_tool(name: str, *, risk: str, retry: bool) -> GatewayTool:
    return GatewayTool(
        name=name,
        description="test tool",
        input_schema={"type": "object"},
        risk=risk,
        auth_roles=("readonly", "ops", "admin"),
        category="test",
        tags=(),
        retry_on_auth_expiry=retry,
    )


def _tool_response(name: str, success: bool, error_code: str | None = None) -> ToolResponse:
    return ToolResponse(
        tool_name=name,
        success=success,
        data={"ok": True} if success else None,
        error_code=error_code,
        error_msg="expired" if error_code else None,
        execution_time_ms=1,
        audit_id=f"audit-{name}",
    )


def test_retry_safe_tool_refreshes_and_retries_only_once(monkeypatch):
    broker = _RefreshingBroker()
    gateway = IdentityScopedMcpGateway("http://127.0.0.1:8020/mcp", broker)
    tool = _gateway_tool("query_edme_resources", risk="none", retry=True)
    old = _ScriptedChildGateway(tool, [
        _tool_response(tool.name, False, "PLATFORM_AUTH_EXPIRED"),
    ])
    new = _ScriptedChildGateway(tool, [_tool_response(tool.name, True)])
    entries = iter([(old, "old-session"), (new, "new-session")])
    monkeypatch.setattr(gateway, "_gateway_entry", lambda _auth: next(entries))
    monkeypatch.setattr(gateway, "_close_session", lambda *_args: None)
    request = ToolRequest(
        tool_name=tool.name,
        caller_user_id="user",
        caller_roles=["readonly"],
        tenant_id="tenant",
        task_id="task-retry-safe",
    )

    response = gateway.call(request, "catalog-version")

    assert response.success
    assert old.calls == 1
    assert new.calls == 1
    assert broker.refreshes[0][1] == "old-session"


def test_write_tool_refreshes_but_is_not_replayed_and_invalidates_approval(monkeypatch):
    broker = _RefreshingBroker()
    gateway = IdentityScopedMcpGateway("http://127.0.0.1:8020/mcp", broker)
    # The client invariant must hold even if a remote server advertises an
    # internally inconsistent write+retry combination.
    tool = _gateway_tool("restart_vm", risk="high", retry=True)
    child = _ScriptedChildGateway(tool, [
        _tool_response(tool.name, False, "PLATFORM_AUTH_EXPIRED"),
    ])
    monkeypatch.setattr(gateway, "_gateway_entry", lambda _auth: (child, "old-session"))
    monkeypatch.setattr(gateway, "_close_session", lambda *_args: None)
    invalidated = []
    monkeypatch.setattr(
        gateway_module.approval_store,
        "invalidate",
        lambda task_id, reason: invalidated.append((task_id, reason)) or True,
    )
    request = ToolRequest(
        tool_name=tool.name,
        caller_user_id="user",
        caller_roles=["ops"],
        tenant_id="tenant",
        task_id="task-write-expired",
    )

    response = gateway.call(request, "catalog-version")

    assert response.error_code == "PLATFORM_AUTH_REFRESHED_RETRY_REQUIRED"
    assert child.calls == 1
    assert invalidated and invalidated[0][0] == request.task_id


def test_remote_catalog_rejects_write_tool_marked_for_auth_retry():
    remote_tool = gateway_module.mcp_types.Tool(
        name="unsafe-write",
        description="invalid metadata combination",
        inputSchema={"type": "object"},
        _meta={
            gateway_module.TOOL_META_KEY: {
                "risk": "high",
                "auth_roles": ["ops"],
                "category": "test",
                "tags": [],
                "retry_on_auth_expiry": True,
            },
        },
    )

    with pytest.raises(gateway_module.ToolGatewayError, match="写工具"):
        gateway_module.McpToolGateway._tool_from_mcp(remote_tool)


def test_refresh_failure_returns_stable_error_without_replaying(monkeypatch):
    class FailedBroker:
        def refresh(self, _auth, stale_session_id=None):
            assert stale_session_id == "old-session"
            raise PlatformSessionRefreshError("refresh failed")

    gateway = IdentityScopedMcpGateway(
        "http://127.0.0.1:8020/mcp", FailedBroker()
    )
    tool = _gateway_tool("query_edme_resources", risk="none", retry=True)
    child = _ScriptedChildGateway(tool, [
        _tool_response(tool.name, False, "PLATFORM_AUTH_EXPIRED"),
    ])
    monkeypatch.setattr(gateway, "_gateway_entry", lambda _auth: (child, "old-session"))
    monkeypatch.setattr(gateway, "_close_session", lambda *_args: None)
    request = ToolRequest(
        tool_name=tool.name,
        caller_user_id="user",
        caller_roles=["readonly"],
        tenant_id="tenant",
        task_id="task-refresh-failed",
    )

    response = gateway.call(request, "catalog-version")

    assert response.error_code == "PLATFORM_AUTH_REFRESH_FAILED"
    assert child.calls == 1


def test_retry_metadata_is_opt_in_and_only_enabled_for_reads():
    assert TOOL_REGISTRY["query_edme_resources"].retry_on_auth_expiry is True
    assert TOOL_REGISTRY["restart_vm"].retry_on_auth_expiry is False
    assert TOOL_REGISTRY["create_approval_request"].retry_on_auth_expiry is False
