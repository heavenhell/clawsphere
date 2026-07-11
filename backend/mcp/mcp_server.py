from __future__ import annotations

import json
import sys

from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import TOOL_REGISTRY, call_tool


def list_tools() -> list[dict]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "risk": spec.risk,
            "auth_roles": spec.auth_roles,
        }
        for spec in TOOL_REGISTRY.values()
    ]


def handle(payload: dict) -> dict:
    method = payload.get("method")
    if method == "tools/list":
        return {"tools": list_tools()}
    if method == "tools/call":
        params = payload.get("params", {})
        response = call_tool(ToolRequest(
            tool_name=params.get("name"),
            params=params.get("arguments", {}),
            caller_roles=params.get("roles", ["readonly"]),
        ))
        return response.model_dump()
    return {"error": "unsupported_method", "method": method}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            print(json.dumps(handle(payload), ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
