from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from statistics import mean
from typing import Any

from backend.adapters.edme import EDMERestAdapter
from backend.adapters.fusioncompute import FusionComputeRestAdapter
from backend.interfaces.dorado import DoradoInterface
from backend.interfaces.edme import EDMEInterface
from backend.interfaces.fusioncompute import FusionComputeInterface
from backend.mock.repository import MockRepository
from backend.platform_config import RuntimeConfig, load_runtime_config


class ConfiguredRepository(FusionComputeInterface, DoradoInterface, EDMEInterface):
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.mock = MockRepository()
        self.compute: Any = (
            FusionComputeRestAdapter(config.fusioncompute)
            if config.fusioncompute.configured
            else self.mock
        )
        self.edme: Any = EDMERestAdapter(config.edme) if config.edme.configured else self.mock
        self.storage: Any = self.edme if config.edme.configured else self.compute

    def sites(self):
        if self.config.edme.configured:
            return self.edme.virtual_sites()
        return self.compute.sites()

    def clusters(self):
        if self.config.edme.configured:
            return self.edme.virtual_clusters()
        return self.compute.clusters()

    def hosts(self):
        if self.config.edme.configured:
            return self.edme.virtual_hosts()
        return self.compute.hosts()

    def vms(self):
        if self.config.edme.configured:
            return self.edme.virtual_vms()
        return self.compute.vms()

    def alarms(self):
        if self.config.edme.configured:
            return self.edme.virtual_alarms()
        return self.compute.alarms()

    def _performance_source(self) -> Any:
        """Return the performance-metric backend, or refuse to guess.

        Metrics come from FusionCompute. When a real platform is configured but
        FusionCompute is not, `self.compute` is the mock repository — serving
        its numbers would present demo data as live platform state, which is
        exactly the fabrication the grounding rules exist to prevent. Failing
        loudly turns that into a tool error the agent can report instead.
        """
        if self.config.fusioncompute.configured or not self.config.real_platforms:
            return self.compute
        raise ValueError(
            "性能指标由 FusionCompute 提供，当前未配置 FusionCompute："
            f"已接入平台为 {', '.join(self.config.real_platforms)}，无法返回真实指标"
        )

    def metrics(self):
        return self._performance_source().metrics()

    def vm_metrics(self, vm_id: str):
        return self._performance_source().vm_metrics(vm_id)

    def cluster_daily_growth_gb(self, cluster_id: str) -> float:
        return self._performance_source().cluster_daily_growth_gb(cluster_id)

    def datastores(self):
        return self.storage.datastores()

    def storage_pool_usage(self, pool_id: str | None = None):
        return self.storage.storage_pool_usage(pool_id)

    def edme_current_alarms(self, severity: int | None = None, iterator: str | None = None):
        return self.edme.edme_current_alarms(severity, iterator)

    def edme_resource_instances(self, class_name: str, page_no: int = 1, page_size: int = 20):
        return self.edme.edme_resource_instances(class_name, page_no, page_size)

    def edme_object_types(self):
        return self.edme.edme_object_types()

    def edme_indicators(self, object_type_id: int | None = None):
        return self.edme.edme_indicators(object_type_id)

    def edme_history(
        self,
        object_ids: list[str] | None = None,
        indicator_ids: list[int] | None = None,
        time_range: str = "LAST_1_HOUR",
    ):
        return self.edme.edme_history(object_ids, indicator_ids, time_range)

    @staticmethod
    def _reported_totals(clusters: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "vms": sum(int(item.get("vm_count") or 0) for item in clusters),
            "hosts": sum(int(item.get("host_count") or 0) for item in clusters),
        }

    def _vms_reusing(self, clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """List VMs without re-fetching a cluster list the caller already holds.

        The eDME fan-out needs the cluster list to know what to query; handing
        over the one `overview()` just fetched turns 2 + N requests into 1 + N.
        """
        if self.config.edme.configured:
            return self.edme.virtual_vms(clusters=clusters)
        return self.vms()

    def overview(self) -> dict[str, Any]:
        clusters = self.clusters()
        hosts = self.hosts()
        vms = self._vms_reusing(clusters)
        stores = self.datastores()
        alarms = self.alarms()
        # eDME's vms/query has no pagination, so its listing can come back short
        # even after the per-cluster fan-out; each cluster's own `vm_num` is the
        # platform's total and stays correct. Applied only on the eDME path:
        # mock data deliberately carries inconsistent counts, and there len() is
        # the honest answer.
        counted = self._reported_totals(clusters) if self.config.edme.configured else {}
        return {
            "cluster_count": len(clusters),
            "host_count": max(len(hosts), counted.get("hosts", 0)),
            "vm_count": max(len(vms), counted.get("vms", 0)),
            "datastore_count": len(stores),
            "active_alarm_count": len([item for item in alarms if item.get("status") == "active"]),
            "avg_cpu_usage": round(mean([item.get("cpu_usage", 0) for item in clusters] or [0]), 4),
            "avg_memory_usage": round(mean([item.get("memory_usage", 0) for item in clusters] or [0]), 4),
            "capacity_risks": [
                item for item in stores
                if item.get("capacity_gb") and item.get("free_gb", 0) / item["capacity_gb"] < 0.12
            ],
        }

    def platform_status(self) -> dict[str, Any]:
        return {
            "fusioncompute": "real" if self.config.fusioncompute.configured else "mock",
            "edme": (
                "client-delegated" if self.config.edme.client_delegated
                else "real" if self.config.edme.configured else "mock"
            ),
            "storage": "edme" if self.config.edme.available else (
                "fusioncompute" if self.config.fusioncompute.configured else "mock"
            ),
            "mock_api_exposed": self.config.expose_mock_api,
            "mcp": {
                "enabled": self.config.mcp.enabled,
                "agent_mode": self.config.mcp.agent_mode,
                "transport": self.config.mcp.transport,
                "host": self.config.mcp.host,
                "port": self.config.mcp.port,
                "endpoint": self.config.mcp.endpoint,
            },
            "config_path": str(self.config.source_path),
        }

    def close(self) -> None:
        closed: set[int] = set()
        for adapter in (self.compute, self.edme, self.storage):
            close = getattr(adapter, "close", None)
            if callable(close) and id(adapter) not in closed:
                closed.add(id(adapter))
                close()


_repository_context: ContextVar[ConfiguredRepository | None] = ContextVar(
    "clawsphere_repository", default=None
)


class RepositoryProxy:
    def __init__(self, default: ConfiguredRepository):
        self._default = default

    def current(self) -> ConfiguredRepository:
        return _repository_context.get() or self._default

    def __getattr__(self, name: str) -> Any:
        return getattr(self.current(), name)


@contextmanager
def use_repository(repository: ConfiguredRepository):
    token = _repository_context.set(repository)
    try:
        yield repository
    finally:
        _repository_context.reset(token)


def delegated_edme_repository(access_session: str) -> ConfiguredRepository:
    if not runtime_config.edme.client_delegated:
        raise RuntimeError("eDME 未配置为客户端委托鉴权模式")
    edme = replace(
        runtime_config.edme,
        auth_mode="server",
        username="",
        password="",
        session=access_session,
    )
    return ConfiguredRepository(replace(runtime_config, edme=edme))


runtime_config = load_runtime_config()
repo = RepositoryProxy(ConfiguredRepository(runtime_config))
