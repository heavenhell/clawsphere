from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "platforms.json"


@dataclass(frozen=True)
class PlatformCredentials:
    ip: str = ""
    username: str = ""
    password: str = ""
    session: str = ""
    port: int | None = None
    ca_cert: str | None = None
    site_id: str | None = None
    api_version: str | None = None

    @property
    def configured(self) -> bool:
        ip = self.ip.strip()
        username = self.username.strip()
        has_session = bool(self.session.strip())
        has_username = bool(username)
        has_password = bool(self.password)
        if has_username != has_password:
            raise ValueError("platform username and password must be provided together")
        has_credentials = has_username and has_password
        if (has_session or has_credentials) and not ip:
            raise ValueError("platform ip is required when authentication is configured")
        if ip and not (has_session or has_credentials):
            raise ValueError("platform requires either session or username and password")
        return bool(ip and (has_session or has_credentials))

    @property
    def can_login(self) -> bool:
        return bool(self.username.strip() and self.password)

    def base_url(self, default_port: int) -> str:
        address = self.ip.strip().rstrip("/")
        if address.startswith(("http://", "https://")):
            return address
        return f"https://{address}:{self.port or default_port}"

    @property
    def verify(self) -> bool | str:
        return self.ca_cert or True


@dataclass(frozen=True)
class McpRuntimeConfig:
    enabled: bool = False
    agent_mode: str = "local"
    transport: str = "streamable-http"
    host: str = "127.0.0.1"
    port: int = 8020
    url: str = ""
    connect_timeout_seconds: float = 5.0
    call_timeout_seconds: float = 30.0

    @property
    def endpoint(self) -> str:
        return self.url.strip() or f"http://{self.host}:{self.port}/mcp"


@dataclass(frozen=True)
class RuntimeConfig:
    fusioncompute: PlatformCredentials
    edme: PlatformCredentials
    mcp: McpRuntimeConfig
    expose_mock_api: bool
    source_path: Path

    @property
    def real_platforms(self) -> list[str]:
        result = []
        if self.fusioncompute.configured:
            result.append("fusioncompute")
        if self.edme.configured:
            result.append("edme")
        return result


def _credentials(payload: dict[str, Any] | None) -> PlatformCredentials:
    data = payload or {}
    return PlatformCredentials(
        ip=str(data.get("ip") or ""),
        username=str(data.get("username") or ""),
        password=str(data.get("password") or ""),
        session=str(data.get("session") or ""),
        port=int(data["port"]) if data.get("port") is not None else None,
        ca_cert=str(data["ca_cert"]) if data.get("ca_cert") else None,
        site_id=str(data["site_id"]) if data.get("site_id") else None,
        api_version=str(data["api_version"]) if data.get("api_version") else None,
    )


def load_runtime_config(path: str | Path | None = None) -> RuntimeConfig:
    source = Path(path or os.getenv("DCS_PLATFORM_CONFIG") or DEFAULT_CONFIG_PATH).expanduser().resolve()
    payload: dict[str, Any] = {}
    if source.exists():
        payload = json.loads(source.read_text(encoding="utf-8"))
    fusioncompute = _credentials(payload.get("fusioncompute"))
    edme = _credentials(payload.get("edme"))
    real_enabled = fusioncompute.configured or edme.configured
    mcp_data = payload.get("mcp") or {}
    mcp_enabled = bool(mcp_data.get("enabled", False))
    agent_mode = str(
        mcp_data.get("agent_mode") or ("mcp" if (mcp_enabled or real_enabled) else "local")
    ).lower()
    if agent_mode not in {"local", "mcp"}:
        raise ValueError("mcp.agent_mode must be either 'local' or 'mcp'")
    mcp = McpRuntimeConfig(
        enabled=mcp_enabled,
        agent_mode=agent_mode,
        transport=str(mcp_data.get("transport") or "streamable-http"),
        host=str(mcp_data.get("host") or "127.0.0.1"),
        port=int(mcp_data.get("port") or 8020),
        url=str(mcp_data.get("url") or ""),
        connect_timeout_seconds=float(mcp_data.get("connect_timeout_seconds") or 5.0),
        call_timeout_seconds=float(mcp_data.get("call_timeout_seconds") or 30.0),
    )
    if mcp.connect_timeout_seconds <= 0 or mcp.call_timeout_seconds <= 0:
        raise ValueError("MCP connect and call timeouts must be positive")
    if mcp.url:
        parsed_url = urlparse(mcp.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("mcp.url must be an absolute HTTP(S) URL")
        if parsed_url.username or parsed_url.password:
            raise ValueError("mcp.url must not contain credentials")
        if parsed_url.query or parsed_url.fragment:
            raise ValueError("mcp.url must not contain a query string or fragment")
    if real_enabled and mcp.agent_mode == "local":
        raise ValueError("mcp.agent_mode='local' is only allowed when all platform providers use Mock")
    if mcp.agent_mode == "mcp" and mcp.transport != "streamable-http":
        raise ValueError("Agent MCP mode currently requires mcp.transport='streamable-http'")
    return RuntimeConfig(
        fusioncompute=fusioncompute,
        edme=edme,
        mcp=mcp,
        expose_mock_api=bool(payload.get("expose_mock_api", not real_enabled)),
        source_path=source,
    )
