from __future__ import annotations

import json
import os
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
AGENT_CONFIG_ENV = 'DCS_AGENT_PLATFORM_CONFIG'
MCP_SERVER_CONFIG_ENV = 'DCS_MCP_SERVER_PLATFORM_CONFIG'
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
    verify_ssl: bool | None = None
    auth_mode: str = "server"
    single_tenant_bootstrap: bool = False
    expired_session_error_codes: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        auth_mode = self.auth_mode.strip().lower()
        if auth_mode not in {"server", "client"}:
            raise ValueError("platform auth_mode must be either 'server' or 'client'")
        ip = self.ip.strip()
        username = self.username.strip()
        has_session = bool(self.session.strip())
        has_username = bool(username)
        has_password = bool(self.password)
        if auth_mode == "client":
            if self.single_tenant_bootstrap:
                # Delegation stays switched on: these are the single tenant's
                # own login, exchanged for a session at runtime and forwarded
                # as a signed delegation token. The MCP Server still holds no
                # platform credential of its own. `configured` therefore stays
                # False — the credential is a bootstrap, not a server identity.
                if has_session:
                    raise ValueError(
                        "single-tenant bootstrap logs in at runtime; "
                        "store username and password rather than a session"
                    )
                if has_username != has_password:
                    raise ValueError("single_tenant_bootstrap requires both username and password")
                if (has_username or has_password) and not ip:
                    raise ValueError("platform ip is required when authentication is configured")
            elif has_session or has_username or has_password:
                raise ValueError("client authentication mode must not store platform credentials")
            return False
        if has_username != has_password:
            raise ValueError("platform username and password must be provided together")
        has_credentials = has_username and has_password
        if (has_session or has_credentials) and not ip:
            raise ValueError("platform ip is required when authentication is configured")
        if ip and not (has_session or has_credentials):
            raise ValueError("platform requires either session or username and password")
        return bool(ip and (has_session or has_credentials))

    @property
    def client_delegated(self) -> bool:
        configured = self.configured
        return self.auth_mode.strip().lower() == "client" and bool(self.ip.strip()) and not configured

    @property
    def available(self) -> bool:
        return self.configured or self.client_delegated

    @property
    def can_login(self) -> bool:
        return bool(self.username.strip() and self.password)

    @property
    def has_sensitive_credentials(self) -> bool:
        return bool(self.username.strip() or self.password or self.session.strip())

    @property
    def bootstrap_login(self) -> tuple[str, str] | None:
        """The single tenant's configured platform login, when one is set.

        None means the broker must wait for a per-user login instead: plain
        client delegation, server credentials, or a platform left on Mock.
        """
        if not (self.single_tenant_bootstrap and self.client_delegated and self.can_login):
            return None
        return self.username.strip(), self.password

    def base_url(self, default_port: int) -> str:
        address = self.ip.strip().rstrip("/")
        if address.startswith(("http://", "https://")):
            return address
        return f"https://{address}:{self.port or default_port}"

    def endpoint_fingerprint(self, default_port: int) -> str:
        '''Bind delegated credentials to one normalized platform origin.'''
        parsed = urlparse(self.base_url(default_port))
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or '').lower()
        port = parsed.port or (443 if scheme == 'https' else 80)
        if not scheme or not hostname:
            raise ValueError('platform endpoint must be an absolute origin')
        host = f'[{hostname}]' if ':' in hostname else hostname
        origin = f'{scheme}://{host}:{port}'
        return sha256(origin.encode('utf-8')).hexdigest()

    @property
    def verify(self) -> bool | str:
        if self.verify_ssl is False:
            return False
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
        if self.edme.available:
            result.append("edme")
        return result


def _credentials(payload: dict[str, Any] | None, platform_id: str) -> PlatformCredentials:
    data = payload or {}
    expired_codes = data.get("expired_session_error_codes", [])
    if expired_codes is None:
        expired_codes = []
    if not isinstance(expired_codes, list) or any(
        isinstance(code, bool)
        or not isinstance(code, (str, int))
        or not str(code).strip()
        or len(str(code)) > 128
        for code in expired_codes
    ):
        raise ValueError("expired_session_error_codes must be a list of non-empty codes")
    credentials = PlatformCredentials(
        auth_mode=str(data.get("auth_mode") or "server"),
        single_tenant_bootstrap=bool(data.get("single_tenant_bootstrap", False)),
        ip=str(data.get("ip") or ""),
        username=str(data.get("username") or ""),
        password=str(data.get("password") or ""),
        session=str(data.get("session") or ""),
        port=int(data["port"]) if data.get("port") is not None else None,
        ca_cert=str(data["ca_cert"]) if data.get("ca_cert") else None,
        site_id=str(data["site_id"]) if data.get("site_id") else None,
        api_version=str(data["api_version"]) if data.get("api_version") else None,
        verify_ssl=bool(data["verify_ssl"]) if data.get("verify_ssl") is not None else None,
        expired_session_error_codes=tuple(str(code).strip() for code in expired_codes),
    )
    if credentials.auth_mode.strip().lower() == "client" and platform_id != "edme":
        raise ValueError("client authentication mode is currently supported only for eDME")
    if credentials.single_tenant_bootstrap and credentials.auth_mode.strip().lower() != "client":
        raise ValueError("single_tenant_bootstrap requires auth_mode='client'")
    return credentials


def load_runtime_config(
    path: str | Path | None = None,
    *,
    component: str = "agent",
) -> RuntimeConfig:
    normalized_component = component.strip().lower()
    if normalized_component not in {"agent", "mcp-server"}:
        raise ValueError("component must be either 'agent' or 'mcp-server'")
    component_env = MCP_SERVER_CONFIG_ENV if normalized_component == "mcp-server" else AGENT_CONFIG_ENV
    source = Path(
        path or os.getenv(component_env) or os.getenv("DCS_PLATFORM_CONFIG") or DEFAULT_CONFIG_PATH
    ).expanduser().resolve()
    payload: dict[str, Any] = {}
    if source.exists():
        payload = json.loads(source.read_text(encoding="utf-8"))
    fusioncompute = _credentials(payload.get("fusioncompute"), "fusioncompute")
    edme = _credentials(payload.get("edme"), "edme")
    if (
        normalized_component == "mcp-server"
        and edme.auth_mode.strip().lower() == "client"
        and (edme.single_tenant_bootstrap or edme.has_sensitive_credentials)
    ):
        raise ValueError(
            "MCP Server client authentication config must not contain platform credentials "
            "or enable single_tenant_bootstrap"
        )
    if edme.client_delegated:
        edme_url = urlparse(edme.base_url(26335))
        if edme_url.username or edme_url.password:
            raise ValueError("client-delegated eDME endpoint must not contain credentials")
        if edme_url.query or edme_url.fragment or edme_url.path not in {"", "/"}:
            raise ValueError("client-delegated eDME endpoint must be an origin without path, query, or fragment")
        if edme_url.scheme != "https" and edme_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("client-delegated eDME endpoint requires HTTPS unless it is loopback")
    real_enabled = fusioncompute.available or edme.available
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
