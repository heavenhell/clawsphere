from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import threading
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Callable, Protocol

import httpx
import mcp.types as mcp_types
from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backend.mcp.auth import AuthContext, MCP_CALLER_TOKEN_META_KEY, issue_mcp_caller_token
from backend.mcp.schemas import ToolRequest, ToolResponse
from backend.mcp.tools import TOOL_CATALOG_VERSION, TOOL_METADATA_KEY, TOOL_REGISTRY, call_tool
from backend.observability import MCP_CLIENT_EVENTS
from backend.providers import runtime_config


LOGGER = logging.getLogger(__name__)
TOOL_META_KEY = TOOL_METADATA_KEY
CALLER_TOKEN_META_KEY = MCP_CALLER_TOKEN_META_KEY


class ToolGatewayError(RuntimeError):
    pass


class McpUnavailableError(ToolGatewayError):
    pass


class ToolCatalogChangedError(ToolGatewayError):
    pass


@dataclass(frozen=True)
class GatewayTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: str
    auth_roles: tuple[str, ...]
    category: str
    tags: tuple[str, ...]

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["auth_roles"] = list(self.auth_roles)
        payload["tags"] = list(self.tags)
        return payload


@dataclass(frozen=True)
class ToolCatalogSnapshot:
    version: str
    tools: tuple[GatewayTool, ...]
    source: str

    def model_dump(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tools": [tool.model_dump() for tool in self.tools],
            "source": self.source,
        }


class ToolGateway(Protocol):
    mode: str

    def catalog(self, auth: AuthContext) -> ToolCatalogSnapshot: ...

    def call(self, request: ToolRequest, expected_catalog_version: str) -> ToolResponse: ...

    def close(self) -> None: ...


AlertSink = Callable[[str, dict[str, Any]], None]


def _default_alert_sink(event: str, detail: dict[str, Any]) -> None:
    # Reserved integration point for a future Alertmanager/agent-governance
    # implementation. Never include tokens, request arguments, or credentials.
    LOGGER.error("MCP client event=%s detail=%s", event, json.dumps(detail, ensure_ascii=False))


_alert_sink: AlertSink = _default_alert_sink


def set_mcp_alert_sink(sink: AlertSink) -> None:
    global _alert_sink
    _alert_sink = sink


def _emit(event: str, **detail: Any) -> None:
    MCP_CLIENT_EVENTS.labels(event).inc()
    try:
        _alert_sink(event, detail)
    except Exception:
        # Alert delivery must never mask or replace the original MCP failure.
        LOGGER.exception("MCP alert sink failed for event=%s", event)


def _identity_key(auth: AuthContext) -> tuple[str, tuple[str, ...], str]:
    return auth.user_id, tuple(sorted(auth.roles)), auth.tenant_id


def _fingerprint(tools: list[GatewayTool], source: str) -> str:
    canonical = json.dumps(
        [tool.model_dump() for tool in sorted(tools, key=lambda item: item.name)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{source}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]}"


def _local_tools() -> list[GatewayTool]:
    return [
        GatewayTool(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_model.model_json_schema(),
            risk=spec.risk,
            auth_roles=tuple(spec.auth_roles),
            category=spec.category,
            tags=tuple(spec.tags),
        )
        for spec in TOOL_REGISTRY.values()
    ]


class LocalToolGateway:
    mode = "local"

    def catalog(self, auth: AuthContext) -> ToolCatalogSnapshot:
        del auth
        tools = _local_tools()
        return ToolCatalogSnapshot(
            version=f"local:{TOOL_CATALOG_VERSION}:{_fingerprint(tools, 'local').split(':', 1)[1]}",
            tools=tuple(tools),
            source="local",
        )

    def call(self, request: ToolRequest, expected_catalog_version: str) -> ToolResponse:
        current = self.catalog(AuthContext(request.caller_user_id, request.caller_roles, request.tenant_id))
        if current.version != expected_catalog_version:
            raise ToolCatalogChangedError("工具目录已变化，请重新规划后再执行")
        return call_tool(request)

    def close(self) -> None:
        return None


class McpToolGateway:
    """Persistent Streamable HTTP MCP client with eager list-changed refresh.

    The public interface is synchronous because LangGraph's current nodes are
    synchronous. AnyIO's blocking portal owns one long-lived async session;
    notifications are received on that session and schedule list_tools refresh
    as a separate task so the receive loop is never blocked waiting on itself.
    """

    mode = "mcp"

    def __init__(
        self,
        url: str,
        connect_timeout_seconds: float = 5.0,
        call_timeout_seconds: float = 30.0,
    ) -> None:
        self.url = url
        self.connect_timeout_seconds = connect_timeout_seconds
        self.call_timeout_seconds = call_timeout_seconds
        self._lifecycle_lock = threading.RLock()
        self._portal_cm: Any | None = None
        self._portal: BlockingPortal | None = None
        self._worker_ready = threading.Event()
        self._worker_future: Any | None = None
        self._command_queue: asyncio.Queue[tuple[str, tuple[Any, ...], asyncio.Future[Any]]] | None = None
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._connection_error: Exception | None = None
        self._catalogs: dict[tuple[str, tuple[str, ...], str], ToolCatalogSnapshot] = {}
        self._identities: dict[tuple[str, tuple[str, ...], str], AuthContext] = {}
        self._stale: set[tuple[str, tuple[str, ...], str]] = set()
        self._refresh_task: asyncio.Task[None] | None = None

    def _ensure_portal(self) -> BlockingPortal:
        with self._lifecycle_lock:
            if self._portal is None:
                self._portal_cm = start_blocking_portal(backend="asyncio", name="clawsphere-mcp-client")
                self._portal = self._portal_cm.__enter__()
                self._worker_ready.clear()
                self._worker_future = self._portal.start_task_soon(self._worker)
                if not self._worker_ready.wait(timeout=self.connect_timeout_seconds):
                    raise McpUnavailableError("MCP Client Worker 启动超时")
            return self._portal

    async def _worker(self) -> None:
        self._command_queue = asyncio.Queue()
        self._worker_ready.set()
        while True:
            operation, arguments, result_future = await self._command_queue.get()
            try:
                if operation == "catalog":
                    result = await self._catalog(*arguments)
                elif operation == "call":
                    result = await self._call(*arguments)
                elif operation == "close":
                    await self._disconnect()
                    result_future.set_result(None)
                    return
                else:  # pragma: no cover - internal invariant
                    raise RuntimeError(f"unknown MCP worker operation: {operation}")
            except BaseException as exc:
                if not result_future.done():
                    result_future.set_exception(exc)
            else:
                if not result_future.done():
                    result_future.set_result(result)

    async def _request(self, operation: str, *arguments: Any) -> Any:
        if self._command_queue is None:
            raise McpUnavailableError("MCP Client Worker 尚未就绪")
        result_future = asyncio.get_running_loop().create_future()
        await self._command_queue.put((operation, arguments, result_future))
        return await result_future

    async def _connect(self) -> None:
        if self._session is not None and self._connection_error is None:
            return
        await self._disconnect()
        stack = AsyncExitStack()
        try:
            timeout = httpx.Timeout(
                self.call_timeout_seconds,
                connect=self.connect_timeout_seconds,
            )
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=timeout))
            streams = await stack.enter_async_context(
                streamable_http_client(self.url, http_client=client)
            )
            session = await stack.enter_async_context(ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=self.call_timeout_seconds),
                message_handler=self._handle_message,
            ))
            await session.initialize()
        except Exception as exc:
            await stack.aclose()
            self._connection_error = exc
            _emit("connection_failed", endpoint=self.url, error_type=type(exc).__name__)
            raise McpUnavailableError(f"MCP Server 不可用：{self.url}") from exc
        self._stack = stack
        self._session = session
        self._connection_error = None
        self._stale.update(self._catalogs)
        MCP_CLIENT_EVENTS.labels("connected").inc()

    async def _disconnect(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._refresh_task = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                LOGGER.exception("Failed to close MCP client session")

    async def _handle_message(self, message: Any) -> None:
        if isinstance(message, Exception):
            self._connection_error = message
            self._stale.update(self._catalogs)
            _emit("connection_lost", endpoint=self.url, error_type=type(message).__name__)
            return
        if (
            isinstance(message, mcp_types.ServerNotification)
            and isinstance(message.root, mcp_types.ToolListChangedNotification)
        ):
            self._stale.update(self._catalogs)
            MCP_CLIENT_EVENTS.labels("catalog_notification").inc()
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._refresh_known_catalogs())

    async def _refresh_known_catalogs(self) -> None:
        try:
            for auth in list(self._identities.values()):
                await self._refresh_catalog(auth)
        except Exception as exc:
            self._stale.update(self._catalogs)
            _emit("catalog_refresh_failed", endpoint=self.url, error_type=type(exc).__name__)

    @staticmethod
    def _tool_from_mcp(tool: mcp_types.Tool) -> GatewayTool:
        metadata = (tool.meta or {}).get(TOOL_META_KEY)
        if not isinstance(metadata, dict):
            raise ToolGatewayError(f"MCP 工具 {tool.name} 缺少 ClawSphere 安全元数据")
        roles = metadata.get("auth_roles")
        risk = metadata.get("risk")
        if not isinstance(roles, list) or not roles or not set(roles) <= {"readonly", "ops", "admin"}:
            raise ToolGatewayError(f"MCP 工具 {tool.name} 的角色元数据无效")
        if risk not in {"none", "low", "medium", "high"}:
            raise ToolGatewayError(f"MCP 工具 {tool.name} 的风险元数据无效")
        return GatewayTool(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema,
            risk=risk,
            auth_roles=tuple(roles),
            category=str(metadata.get("category") or "general"),
            tags=tuple(str(tag) for tag in (metadata.get("tags") or [])),
        )

    async def _refresh_catalog(self, auth: AuthContext) -> ToolCatalogSnapshot:
        await self._connect()
        assert self._session is not None
        tools: list[GatewayTool] = []
        cursor: str | None = None
        catalog_task_id = "catalog-" + hashlib.sha256(
            f"{auth.user_id}|{auth.tenant_id}".encode("utf-8")
        ).hexdigest()[:16]
        token = issue_mcp_caller_token(auth, catalog_task_id)
        while True:
            params = mcp_types.PaginatedRequestParams(
                cursor=cursor,
                _meta=mcp_types.RequestParams.Meta(**{CALLER_TOKEN_META_KEY: token}),
            )
            result = await self._session.list_tools(params=params)
            tools.extend(self._tool_from_mcp(tool) for tool in result.tools)
            cursor = result.nextCursor
            if not cursor:
                break
        snapshot = ToolCatalogSnapshot(
            version=_fingerprint(tools, "mcp"),
            tools=tuple(tools),
            source="mcp",
        )
        key = _identity_key(auth)
        self._catalogs[key] = snapshot
        self._identities[key] = auth
        self._stale.discard(key)
        MCP_CLIENT_EVENTS.labels("catalog_refreshed").inc()
        return snapshot

    async def _catalog(self, auth: AuthContext) -> ToolCatalogSnapshot:
        await self._connect()
        key = _identity_key(auth)
        if self._refresh_task is not None and not self._refresh_task.done():
            await self._refresh_task
        if key not in self._catalogs or key in self._stale:
            return await self._refresh_catalog(auth)
        return self._catalogs[key]

    async def _call(self, request: ToolRequest, expected_catalog_version: str) -> ToolResponse:
        auth = AuthContext(request.caller_user_id, request.caller_roles, request.tenant_id)
        catalog = await self._catalog(auth)
        if catalog.version != expected_catalog_version:
            raise ToolCatalogChangedError("工具目录已变化，请重新规划后再执行")
        if request.tool_name not in {tool.name for tool in catalog.tools}:
            raise ToolCatalogChangedError(f"工具 {request.tool_name} 已不在当前 MCP 目录中")
        assert self._session is not None
        token = issue_mcp_caller_token(auth, request.task_id)
        try:
            result = await self._session.call_tool(
                request.tool_name,
                request.params,
                meta={CALLER_TOKEN_META_KEY: token},
            )
        except Exception as exc:
            _emit("tool_call_failed", endpoint=self.url, tool=request.tool_name, error_type=type(exc).__name__)
            raise McpUnavailableError(f"MCP 工具调用失败：{request.tool_name}") from exc
        payload: Any = result.structuredContent
        if payload is None:
            for block in result.content:
                if isinstance(block, mcp_types.TextContent):
                    try:
                        payload = json.loads(block.text)
                    except json.JSONDecodeError:
                        continue
                    break
        if result.isError:
            raise ToolGatewayError(f"MCP Server 拒绝工具调用：{request.tool_name}")
        try:
            return ToolResponse.model_validate(payload)
        except Exception as exc:
            raise ToolGatewayError(f"MCP 工具 {request.tool_name} 返回了无效响应") from exc

    def catalog(self, auth: AuthContext) -> ToolCatalogSnapshot:
        portal = self._ensure_portal()
        try:
            return portal.call(self._request, "catalog", auth)
        except ToolGatewayError:
            raise
        except Exception as exc:
            raise McpUnavailableError(f"MCP Server 不可用：{self.url}") from exc

    def call(self, request: ToolRequest, expected_catalog_version: str) -> ToolResponse:
        portal = self._ensure_portal()
        try:
            return portal.call(self._request, "call", request, expected_catalog_version)
        except ToolGatewayError:
            raise
        except Exception as exc:
            raise McpUnavailableError(f"MCP Server 不可用：{self.url}") from exc

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._portal is not None:
                try:
                    self._portal.call(self._request, "close")
                    if self._worker_future is not None:
                        self._worker_future.result(timeout=self.call_timeout_seconds)
                finally:
                    assert self._portal_cm is not None
                    self._portal_cm.__exit__(None, None, None)
                    self._portal = None
                    self._portal_cm = None
                    self._worker_future = None
                    self._command_queue = None


_gateway_lock = threading.Lock()
_gateway: ToolGateway | None = None


def get_tool_gateway() -> ToolGateway:
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                if runtime_config.mcp.agent_mode == "mcp":
                    _gateway = McpToolGateway(
                        runtime_config.mcp.endpoint,
                        runtime_config.mcp.connect_timeout_seconds,
                        runtime_config.mcp.call_timeout_seconds,
                    )
                else:
                    _gateway = LocalToolGateway()
    return _gateway


def set_tool_gateway_for_tests(gateway: ToolGateway | None) -> None:
    global _gateway
    with _gateway_lock:
        if _gateway is not None and _gateway is not gateway:
            _gateway.close()
        _gateway = gateway


def close_tool_gateway() -> None:
    set_tool_gateway_for_tests(None)


atexit.register(close_tool_gateway)
