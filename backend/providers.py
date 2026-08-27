from __future__ import annotations

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
        return self.compute.sites()

    def clusters(self):
        return self.compute.clusters()

    def hosts(self):
        return self.compute.hosts()

    def vms(self):
        return self.compute.vms()

    def alarms(self):
        return self.compute.alarms()

    def metrics(self):
        return self.compute.metrics()

    def vm_metrics(self, vm_id: str):
        return self.compute.vm_metrics(vm_id)

    def cluster_daily_growth_gb(self, cluster_id: str) -> float:
        return self.compute.cluster_daily_growth_gb(cluster_id)

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

    def overview(self) -> dict[str, Any]:
        clusters = self.clusters()
        hosts = self.hosts()
        vms = self.vms()
        stores = self.datastores()
        alarms = self.alarms()
        return {
            "cluster_count": len(clusters),
            "host_count": len(hosts),
            "vm_count": len(vms),
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
            "edme": "real" if self.config.edme.configured else "mock",
            "storage": "edme" if self.config.edme.configured else (
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


runtime_config = load_runtime_config()
repo = ConfiguredRepository(runtime_config)
