from __future__ import annotations

import os
from tempfile import TemporaryDirectory


# This must remain before any backend import: database singletons read DCS_DATA_DIR at import time.
_test_data = TemporaryDirectory(prefix="clawsphere-tests-", ignore_cleanup_errors=True)
os.environ["DCS_DATA_DIR"] = _test_data.name


def pytest_sessionfinish(session, exitstatus):
    from backend.agent.copilot import close_graph_runtime

    close_graph_runtime()
    _test_data.cleanup()
