from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory


# This must remain before any backend import: database singletons read DCS_DATA_DIR at import time.
_test_data = TemporaryDirectory(prefix="clawsphere-tests-", ignore_cleanup_errors=True)
os.environ["DCS_DATA_DIR"] = _test_data.name

# Pin the platform config the suite runs against. Without this, tests inherit
# the developer's gitignored config/platforms.json: pointing it at a real eDME
# makes the mock endpoints disappear and every agent-loop test time out on the
# network, and agent_mode="mcp" routes tool calls to an MCP server that is not
# running under pytest. Both are environment noise, not regressions, so the
# suite supplies its own mock-mode config.
_test_platforms = Path(_test_data.name) / "platforms.json"
_test_platforms.write_text(
    json.dumps({
        "fusioncompute": {"ip": "", "username": "", "password": ""},
        "edme": {"ip": "", "username": "", "password": ""},
        "mcp": {"enabled": False, "agent_mode": "local"},
    }),
    encoding="utf-8",
)
os.environ["DCS_PLATFORM_CONFIG"] = str(_test_platforms)
# Keep unit tests deterministic and prevent load_dotenv() from restoring the real key.
os.environ["DEEPSEEK_API_KEY"] = ""
# API tests exercise many chats rapidly; limiter behavior has dedicated unit coverage.
os.environ["DCS_CHAT_RATE_PER_MINUTE"] = "10000"
os.environ["DCS_CHAT_BURST_PER_10S"] = "10000"


def pytest_sessionfinish(session, exitstatus):
    from backend.agent.copilot import close_graph_runtime

    close_graph_runtime()
    _test_data.cleanup()
