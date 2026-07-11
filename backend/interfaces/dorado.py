from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DoradoInterface(ABC):
    """Storage-facing contract used by capacity and latency tools."""

    @abstractmethod
    def datastores(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def storage_pool_usage(self, pool_id: str | None = None) -> list[dict[str, Any]]: ...
