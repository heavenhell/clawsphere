from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
from backend.app import app
from backend.mcp.auth import (
    MCP_PLATFORM_CREDENTIAL_HEADER,
    AuthContext,
    decode_platform_delegation_token,
    issue_mcp_caller_token,
    issue_platform_delegation_token,
)
from backend.mcp.gateway import IdentityScopedMcpGateway, McpUnavailableError
from backend.mcp import mcp_server
from backend.platform_config import PlatformCredentials, load_runtime_config
from backend.platform_sessions import PlatformSessionBroker, PlatformSessionRateLimitError


client = TestClient(app)


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
    with pytest.raises(RuntimeError, match="账号或密码无效"):
        broker.ensure_session(auth)
    with pytest.raises(RuntimeError, match="账号或密码无效"):
        broker.ensure_session(auth)


def test_platform_delegation_is_bound_to_caller_identity():
    owner = AuthContext("user-1", ["ops"], "tenant-1")
    token = issue_platform_delegation_token(
        owner, "edme", "access-session", "edme-session-123", 4102444800
    )

    decoded = decode_platform_delegation_token(token, owner)
    assert decoded.auth == owner
    assert decoded.access_session == "access-session"
    with pytest.raises(PermissionError, match="身份不匹配"):
        decode_platform_delegation_token(
            token, AuthContext("user-1", ["ops"], "tenant-2")
        )


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
        auth, "edme", "access-from-header", "edme-session-header", 4102444800
    )
    context = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(headers={MCP_PLATFORM_CREDENTIAL_HEADER: token})
    ))
    monkeypatch.setattr(
        mcp_server,
        "runtime_config",
        SimpleNamespace(edme=SimpleNamespace(client_delegated=True)),
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


def test_mcp_server_builds_request_repository_from_validated_session(monkeypatch):
    auth = AuthContext("user-call", ["readonly"], "tenant-call")
    task_id = "task-platform-delegation"
    platform_token = issue_platform_delegation_token(
        auth, "edme", "access-for-repository", "edme-session-call", 4102444800
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
        SimpleNamespace(edme=SimpleNamespace(client_delegated=True)),
    )
    monkeypatch.setattr(
        mcp_server,
        "delegated_edme_repository",
        build_repository,
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
