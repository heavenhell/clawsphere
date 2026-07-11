from __future__ import annotations

import os
from functools import wraps
from typing import Callable

from prometheus_client import Counter, Histogram


HTTP_REQUESTS = Counter("clawsphere_http_requests_total", "HTTP requests", ["method", "path", "status"])
HTTP_LATENCY = Histogram("clawsphere_http_request_duration_seconds", "HTTP request latency", ["path"])
INTENT_COUNT = Counter("clawsphere_agent_intents_total", "Classified agent intents", ["intent"])
TOOL_CALLS = Counter("clawsphere_tool_calls_total", "MCP tool calls", ["tool", "success"])
TOOL_LATENCY = Histogram("clawsphere_tool_duration_seconds", "MCP tool latency", ["tool"])
APPROVAL_DECISIONS = Counter("clawsphere_approval_decisions_total", "HITL decisions", ["decision"])


def observe_agent(fn: Callable):
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return fn
    try:
        from langfuse import observe
        return observe(name="clawsphere-agent", as_type="agent")(fn)
    except ImportError:
        return fn
