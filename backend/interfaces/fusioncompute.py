from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class FusionComputeInterface(ABC):
    """Stable domain contract between tools and FusionCompute northbound APIs."""

    @abstractmethod
    def sites(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def clusters(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def hosts(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def vms(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def alarms(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def metrics(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def vm_metrics(self, vm_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def cluster_daily_growth_gb(self, cluster_id: str) -> float: ...

    @abstractmethod
    def overview(self) -> dict[str, Any]: ...
