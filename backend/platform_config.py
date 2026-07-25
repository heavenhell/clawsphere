from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    transport: str = "streamable-http"
    host: str = "127.0.0.1"
    port: int = 8020


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
    mcp_data = payload.get("mcp") or {}
    mcp = McpRuntimeConfig(
        enabled=bool(mcp_data.get("enabled", False)),
        transport=str(mcp_data.get("transport") or "streamable-http"),
        host=str(mcp_data.get("host") or "127.0.0.1"),
        port=int(mcp_data.get("port") or 8020),
    )
    real_enabled = fusioncompute.configured or edme.configured
    return RuntimeConfig(
        fusioncompute=fusioncompute,
        edme=edme,
        mcp=mcp,
        expose_mock_api=bool(payload.get("expose_mock_api", not real_enabled)),
        source_path=source,
    )
