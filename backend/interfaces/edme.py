from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class EDMEInterface(ABC):
    """Stable contract for eDME operations-plane northbound APIs."""

    @abstractmethod
    def edme_current_alarms(
        self,
        severity: int | None = None,
        iterator: str | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def edme_resource_instances(
        self,
        class_name: str,
        page_no: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def edme_object_types(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def edme_indicators(self, object_type_id: int | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def edme_history(
        self,
        object_ids: list[str] | None = None,
        indicator_ids: list[int] | None = None,
        time_range: str = "LAST_1_HOUR",
    ) -> list[dict[str, Any]]: ...
