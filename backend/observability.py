from __future__ import annotations

import os
from contextlib import nullcontext
from functools import wraps
from typing import Callable


class _NoopMetric:
    def labels(self, *args, **kwargs):
        return self

    def inc(self, amount: float = 1) -> None:
        return None

    def observe(self, amount: float) -> None:
        return None

    def time(self):
        return nullcontext()


try:
    from prometheus_client import Counter, Histogram
except ImportError:
    HTTP_REQUESTS = HTTP_LATENCY = INTENT_COUNT = TOOL_CALLS = TOOL_LATENCY = APPROVAL_DECISIONS = MCP_CLIENT_EVENTS = _NoopMetric()
else:
    HTTP_REQUESTS = Counter("clawsphere_http_requests_total", "HTTP requests", ["method", "path", "status"])
    HTTP_LATENCY = Histogram("clawsphere_http_request_duration_seconds", "HTTP request latency", ["path"])
    INTENT_COUNT = Counter("clawsphere_agent_intents_total", "Classified agent intents", ["intent"])
    TOOL_CALLS = Counter("clawsphere_tool_calls_total", "MCP tool calls", ["tool", "success"])
    TOOL_LATENCY = Histogram("clawsphere_tool_duration_seconds", "MCP tool latency", ["tool"])
    APPROVAL_DECISIONS = Counter("clawsphere_approval_decisions_total", "HITL decisions", ["decision"])
    MCP_CLIENT_EVENTS = Counter(
        "clawsphere_mcp_client_events_total",
        "MCP client connection, catalog, and call events",
        ["event"],
    )


def create_metrics_app():
    try:
        from prometheus_client import make_asgi_app
    except ImportError:
        return None
    return make_asgi_app()


def observe_agent(fn: Callable):
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return fn
    try:
        from langfuse import observe
        return observe(name="clawsphere-agent", as_type="agent")(fn)
    except ImportError:
        return fn
