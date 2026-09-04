from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from backend.adapters.edme import EDMERestAdapter, PlatformAuthExpiredError
from backend.adapters.fusioncompute import FusionComputeRestAdapter
from backend.platform_config import PlatformCredentials, load_runtime_config
from backend.providers import ConfiguredRepository


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_single_config_enables_only_completed_platforms(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "fusioncompute": {"ip": "10.0.0.10", "username": "fc-user", "password": "secret"},
        "edme": {"ip": "", "username": "", "password": ""},
    }), encoding="utf-8")
    config = load_runtime_config(path)
    assert config.real_platforms == ["fusioncompute"]
    assert not config.expose_mock_api
    assert config.mcp.agent_mode == "mcp"
    assert config.fusioncompute.base_url(7443) == "https://10.0.0.10:7443"


def test_partial_platform_credentials_are_rejected(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {"ip": "10.0.0.20", "username": "", "password": ""},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="requires either session"):
        load_runtime_config(path)


def test_session_only_configuration_enables_platform(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {"ip": "10.0.0.20", "session": "existing-session"},
    }), encoding="utf-8")
    config = load_runtime_config(path)
    assert config.real_platforms == ["edme"]
    assert config.edme.session == "existing-session"
    assert not config.edme.can_login


def test_fusioncompute_adapter_logs_in_and_discovers_site():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/service/session":
            assert request.headers["X-Auth-Key"] == hashlib.sha256(b"secret").hexdigest()
            return httpx.Response(200, headers={"X-Auth-Token": "fc-token"}, json={"validity": 600000})
        assert request.headers["X-Auth-Token"] == "fc-token"
        if request.url.path == "/service/sites":
            return httpx.Response(200, json={"items": [{"urn": "urn:sites:SITE01", "name": "FC-Site"}]})
        if request.url.path == "/service/sites/SITE01/clusters":
            return httpx.Response(200, json={"items": [{
                "urn": "urn:sites:SITE01:clusters:10", "name": "Cluster-A", "cpuUsage": 65,
            }]})
        return httpx.Response(404)

    client = httpx.Client(base_url="https://fc.local:7443", transport=httpx.MockTransport(handler))
    adapter = FusionComputeRestAdapter(
        PlatformCredentials(ip="fc.local", username="fc-user", password="secret"), client=client
    )
    clusters = adapter.clusters()
    assert clusters[0]["id"] == "10"
    assert clusters[0]["cpu_usage"] == 0.65
    assert len([item for item in requests if item.url.path == "/service/session"]) == 1


def test_edme_adapter_logs_in_and_queries_resources():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/plat/smapp/v1/sessions":
            return httpx.Response(200, json={"accessSession": "edme-token", "expires": 1800})
        assert request.headers["X-Auth-Token"] == "edme-token"
        if request.url.path.endswith("/SYS_StorageDevice"):
            return httpx.Response(200, json={
                "objList": [{"id": "device-1", "name": "Storage-A"}],
                "totalNum": 1,
                "pageSize": 20,
                "totalPageNo": 1,
                "currentPage": 1,
            })
        return httpx.Response(404)

    client = httpx.Client(base_url="https://edme.local:26335", transport=httpx.MockTransport(handler))
    adapter = EDMERestAdapter(
        PlatformCredentials(ip="edme.local", username="edme-user", password="secret"), client=client
    )
    result = adapter.edme_resource_instances("SYS_StorageDevice")
    assert result["totalNum"] == 1
    assert result["objList"][0]["name"] == "Storage-A"


def test_fusioncompute_adapter_uses_configured_session_without_login():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path != "/service/session"
        assert request.headers["X-Auth-Token"] == "existing-fc-session"
        return httpx.Response(200, json={"items": [{"urn": "urn:sites:SITE01", "name": "FC-Site"}]})

    client = httpx.Client(base_url="https://fc.local:7443", transport=httpx.MockTransport(handler))
    adapter = FusionComputeRestAdapter(
        PlatformCredentials(ip="fc.local", session="existing-fc-session"), client=client
    )
    assert adapter.sites()[0]["id"] == "SITE01"


def test_edme_adapter_uses_configured_session_without_login():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path != "/rest/plat/smapp/v1/sessions"
        assert request.headers["X-Auth-Token"] == "existing-edme-session"
        return httpx.Response(200, json={
            "objList": [{"id": "device-1", "name": "Storage-A"}],
            "totalNum": 1,
        })

    client = httpx.Client(base_url="https://edme.local:26335", transport=httpx.MockTransport(handler))
    adapter = EDMERestAdapter(
        PlatformCredentials(ip="edme.local", session="existing-edme-session"), client=client
    )
    assert adapter.edme_resource_instances("SYS_StorageDevice")["totalNum"] == 1


def test_session_only_configuration_fails_clearly_when_session_expires():
    client = httpx.Client(
        base_url="https://edme.local:26335",
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={})),
    )
    adapter = EDMERestAdapter(
        PlatformCredentials(ip="edme.local", session="expired-session"), client=client
    )
    with pytest.raises(PlatformAuthExpiredError, match="access session has expired"):
        adapter.edme_resource_instances("SYS_StorageDevice")


def test_component_specific_config_paths_take_precedence(tmp_path, monkeypatch):
    agent_path = tmp_path / "agent.json"
    server_path = tmp_path / "server.json"
    agent_path.write_text("{}", encoding="utf-8")
    server_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DCS_PLATFORM_CONFIG", str(tmp_path / "legacy.json"))
    monkeypatch.setenv("DCS_AGENT_PLATFORM_CONFIG", str(agent_path))
    monkeypatch.setenv("DCS_MCP_SERVER_PLATFORM_CONFIG", str(server_path))

    assert load_runtime_config(component="agent").source_path == agent_path.resolve()
    assert load_runtime_config(component="mcp-server").source_path == server_path.resolve()


def _write_split_client_configs(tmp_path):
    agent_path = tmp_path / "agent.json"
    server_path = tmp_path / "server.json"
    agent_path.write_text(json.dumps({
        "edme": {
            "auth_mode": "client",
            "single_tenant_bootstrap": True,
            "ip": "edme.example.test",
            "username": "agent-user",
            "password": "agent-password",
        },
    }), encoding="utf-8")
    server_path.write_text(json.dumps({
        "edme": {
            "auth_mode": "client",
            "ip": "edme.example.test",
        },
    }), encoding="utf-8")
    return agent_path, server_path


def test_mcp_server_process_loads_only_its_credential_free_config(tmp_path):
    agent_path, server_path = _write_split_client_configs(tmp_path)
    environment = os.environ.copy()
    environment["DCS_AGENT_PLATFORM_CONFIG"] = str(agent_path)
    environment["DCS_MCP_SERVER_PLATFORM_CONFIG"] = str(server_path)
    command = (
        "import sys, backend; "
        "print('providers_preloaded=' + str('backend.providers' in sys.modules)); "
        "import backend.mcp.mcp_server as server; "
        "import backend.providers as providers; "
        "print(providers.runtime_config.source_path); "
        "print(server.runtime_config.source_path)"
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines[-3:] == [
        "providers_preloaded=False",
        str(server_path.resolve()),
        str(server_path.resolve()),
    ]


def test_mcp_server_rejects_a_process_that_preloaded_agent_config(tmp_path):
    agent_path, server_path = _write_split_client_configs(tmp_path)
    environment = os.environ.copy()
    environment["DCS_AGENT_PLATFORM_CONFIG"] = str(agent_path)
    environment["DCS_MCP_SERVER_PLATFORM_CONFIG"] = str(server_path)
    command = (
        "import backend.providers; "
        "import backend.mcp.mcp_server"
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode != 0
    assert "providers loaded a different platform config" in result.stderr


@pytest.mark.parametrize("credentials", [
    {"single_tenant_bootstrap": True},
    {"username": "client-user", "password": "client-password"},
    {"session": "client-session"},
])
def test_mcp_server_rejects_client_credentials(tmp_path, credentials):
    path = tmp_path / "server.json"
    path.write_text(json.dumps({
        "edme": {"auth_mode": "client", "ip": "edme.example.test", **credentials},
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="MCP Server client authentication config"):
        load_runtime_config(path, component="mcp-server")


def test_agent_can_start_with_unavailable_bootstrap_login(tmp_path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({
        "edme": {
            "auth_mode": "client",
            "single_tenant_bootstrap": True,
            "ip": "edme.example.test",
        },
    }), encoding="utf-8")

    config = load_runtime_config(path, component="agent")

    assert config.edme.client_delegated
    assert config.edme.bootstrap_login is None
    repository = ConfiguredRepository(config)
    assert repository.platform_status()["edme"] == "unavailable"
    assert repository.platform_status()["storage"] == "unavailable"


def test_expired_session_error_codes_are_normalized(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {
            "ip": "edme.example.test",
            "session": "session",
            "expired_session_error_codes": [" SESSION_EXPIRED ", 10042],
        },
    }), encoding="utf-8")

    config = load_runtime_config(path)

    assert config.edme.expired_session_error_codes == ("SESSION_EXPIRED", "10042")


@pytest.mark.parametrize("codes", ["", "SESSION_EXPIRED", [""], [True], [{}]])
def test_invalid_expired_session_error_codes_are_rejected(tmp_path, codes):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {
            "ip": "edme.example.test",
            "session": "session",
            "expired_session_error_codes": codes,
        },
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="expired_session_error_codes"):
        load_runtime_config(path)


def test_remote_mcp_endpoint_is_decoupled_from_local_server_start(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "mcp": {
            "enabled": False,
            "agent_mode": "mcp",
            "url": "https://mcp.example.test/operations/mcp",
            "connect_timeout_seconds": 3,
            "call_timeout_seconds": 20,
        }
    }), encoding="utf-8")
    config = load_runtime_config(path)
    assert not config.mcp.enabled
    assert config.mcp.agent_mode == "mcp"
    assert config.mcp.endpoint == "https://mcp.example.test/operations/mcp"


def test_local_agent_mode_is_rejected_for_real_platforms(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "fusioncompute": {"ip": "10.0.0.10", "username": "fc-user", "password": "secret"},
        "mcp": {"agent_mode": "local"},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="only allowed.*Mock"):
        load_runtime_config(path)


def test_mcp_url_rejects_embedded_credentials(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "mcp": {"agent_mode": "mcp", "url": "https://user:secret@mcp.example.test/mcp"},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="must not contain credentials"):
        load_runtime_config(path)


# --- The shipped example config -----------------------------------------------

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "platforms.example.json"


def _example_payload() -> dict:
    return json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))


def test_the_shipped_example_config_starts_unmodified(tmp_path):
    """Copying the template without editing it must produce a working mock setup."""
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps(_example_payload()), encoding="utf-8")
    config = load_runtime_config(path)
    assert config.real_platforms == []
    assert config.expose_mock_api


def test_filling_in_the_example_config_the_obvious_way_starts_successfully(tmp_path):
    """The template must be fillable by doing the obvious thing to it.

    It briefly shipped `"auth_mode": "client"` next to empty `username`,
    `password` and `session` fields. Filling those blanks in — the only thing a
    blank template invites — raised `client authentication mode must not store
    platform credentials` at startup, and the message read as an accusation
    rather than as "these must stay empty in this mode". There was in fact no
    way at all to reach the delegation chain from the config file, which is
    what a single-tenant deployment needs. `single_tenant_bootstrap` is that
    way, and this test fails if filling the template stops working again.
    """
    payload = _example_payload()
    payload["fusioncompute"].update({
        "ip": "fusioncompute.example.internal",
        "username": "northbound-user",
        "password": "replace-me",
    })
    payload["edme"].update({
        "ip": "edme.example.internal",
        "single_tenant_bootstrap": True,
        "username": "northbound-user",
        "password": "replace-me",
    })
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_runtime_config(path)
    assert config.real_platforms == ["fusioncompute", "edme"]
    assert not config.expose_mock_api
    # The credential bootstraps a delegated session; it never becomes a server
    # identity, so every tool call still travels the delegation chain.
    assert not config.edme.configured
    assert config.edme.client_delegated
    assert config.edme.bootstrap_login == ("northbound-user", "replace-me")


def test_bootstrap_requires_a_complete_login(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {
            "auth_mode": "client", "single_tenant_bootstrap": True,
            "ip": "edme.example.internal", "username": "northbound-user",
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="requires both username and password"):
        load_runtime_config(path)


def test_bootstrap_rejects_a_stored_session(tmp_path):
    """The broker logs in to obtain a session; a pre-baked one cannot be renewed
    and would silently expire into a state no restart of the Agent recovers."""
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {
            "auth_mode": "client", "single_tenant_bootstrap": True,
            "ip": "edme.example.internal", "session": "pre-baked",
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="rather than a session"):
        load_runtime_config(path)


def test_bootstrap_is_rejected_outside_client_mode(tmp_path):
    """Server mode already stores credentials; accepting the flag there would
    imply a delegation chain that mode does not use."""
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {
            "single_tenant_bootstrap": True, "ip": "edme.example.internal",
            "username": "northbound-user", "password": "replace-me",
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="requires auth_mode='client'"):
        load_runtime_config(path)


def test_client_mode_without_the_bootstrap_flag_still_refuses_credentials(tmp_path):
    """Opting in has to be explicit: `auth_mode=client` alone still promises
    per-user identities, and silently sharing one account would break that."""
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({
        "edme": {
            "auth_mode": "client", "ip": "edme.example.internal",
            "username": "northbound-user", "password": "replace-me",
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="must not store platform credentials"):
        load_runtime_config(path)


def test_mcp_server_example_is_credential_free_and_loadable():
    config = load_runtime_config(
        REPOSITORY_ROOT / "config" / "platforms.mcp-server.example.json",
        component="mcp-server",
    )

    assert not config.fusioncompute.configured
    assert config.edme.client_delegated
    assert not config.edme.has_sensitive_credentials
    assert config.edme.ip == "192.0.2.30"


@pytest.mark.skipif(sys.platform != "win32", reason="start-demo.ps1 is Windows-only")
def test_start_demo_empty_client_template_stays_mock_and_honors_legacy_fallback(tmp_path):
    legacy_path = tmp_path / "legacy-platforms.json"
    legacy_path.write_text(
        (REPOSITORY_ROOT / "config" / "platforms.example.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("DCS_AGENT_PLATFORM_CONFIG", None)
    env.pop("DCS_MCP_SERVER_PLATFORM_CONFIG", None)
    env["DCS_PLATFORM_CONFIG"] = str(legacy_path)

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(REPOSITORY_ROOT / "start-demo.ps1"),
            "-ValidateOnly",
        ],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert Path(result["agent_platform_config"]) == legacy_path
    assert Path(result["mcp_server_platform_config"]) == legacy_path
    assert result["client_delegated_edme"] is False
    assert result["mcp_server_config_available"] is True


# --- Test-suite determinism --------------------------------------------------

def test_the_suite_runs_against_mock_platforms_not_the_developers_config():
    """conftest pins DCS_PLATFORM_CONFIG; assert the pin actually took effect.

    Without it the suite inherits the gitignored config/platforms.json. Pointing
    that at a real platform removes the mock endpoints and makes every agent
    test time out on the network, and agent_mode="mcp" sends tool calls to an
    MCP server that is not running — 15 tests failed that way, none of them a
    real regression. A silent dependency on one developer's local file is worse
    than a broken test, so it gets its own assertion.
    """
    from backend.providers import repo, runtime_config

    assert runtime_config.real_platforms == []
    assert runtime_config.mcp.agent_mode == "local"
    assert repo.platform_status()["fusioncompute"] == "mock"
    assert repo.platform_status()["edme"] == "mock"


def test_metrics_are_served_from_mock_when_no_real_platform_is_configured():
    """The counterpart to the eDME guard: with nothing real configured, the
    mock repository is the honest answer and must not be refused."""
    from backend.providers import repo

    assert repo.metrics()
    assert repo.vm_metrics("vm-1001") is not None
