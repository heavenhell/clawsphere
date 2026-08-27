from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from backend.adapters.edme import EDMERestAdapter
from backend.adapters.fusioncompute import FusionComputeRestAdapter
from backend.platform_config import PlatformCredentials, load_runtime_config


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
        transport=httpx.MockTransport(lambda _request: httpx.Response(403, json={})),
    )
    adapter = EDMERestAdapter(
        PlatformCredentials(ip="edme.local", session="expired-session"), client=client
    )
    with pytest.raises(RuntimeError, match="session is missing or expired"):
        adapter.edme_resource_instances("SYS_StorageDevice")


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
