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
    def overview(self) -> dict[str, Any]: ...
