from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

from backend.interfaces.dorado import DoradoInterface
from backend.interfaces.edme import EDMEInterface
from backend.interfaces.fusioncompute import FusionComputeInterface


class MockRepository(FusionComputeInterface, DoradoInterface, EDMEInterface):
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent
        self.data_dir = self.base_dir / "data"

    def _load(self, name: str):
        path = self.data_dir / name
        return json.loads(path.read_text(encoding="utf-8"))

    def sites(self):
        return self._load("sites.json")

    def clusters(self):
        return self._load("clusters.json")

    def hosts(self):
        return self._load("hosts.json")

    def vms(self):
        return self._load("vms.json")

    def datastores(self):
        return self._load("datastores.json")

    def alarms(self):
        return self._load("alarms.json")

    def tasks(self):
        return self._load("tasks.json")

    def metrics(self):
        return self._load("metrics-sample.json")

    def vm_metrics(self, vm_id: str):
        scenario = self._load("vm-metrics.json")
        values = {**scenario["defaults"], **scenario["overrides"].get(vm_id, {})}
        units = {
            "cpu.usage": "%", "cpu.ready": "%", "mem.usage": "%",
            "mem.balloon": "MB", "disk.latency": "ms", "net.drop": "%",
        }
        thresholds = {
            "cpu.usage": 80, "cpu.ready": 5, "mem.usage": 85,
            "mem.balloon": 0, "disk.latency": 20, "net.drop": 1,
        }
        return [
            {
                "metric": name,
                "value": value,
                "unit": units[name],
                "status": "warning" if value > thresholds[name] else "normal",
            }
            for name, value in values.items()
        ]

    def cluster_daily_growth_gb(self, cluster_id: str) -> float:
        growth = self._load("capacity-growth.json")
        if cluster_id not in growth:
            raise KeyError(cluster_id)
        return float(growth[cluster_id])

    def storage_pool_usage(self, pool_id: str | None = None):
        pools = self.datastores()
        if pool_id:
            pools = [pool for pool in pools if pool["id"] == pool_id]
        return pools

    def edme_current_alarms(self, severity: int | None = None, iterator: str | None = None):
        alarms = self._load("edme-alarms.json")
        if severity is not None:
            alarms = [alarm for alarm in alarms if alarm.get("severity") == severity]
        # The demo dataset fits in one page, so no continuation iterator is emitted.
        return {"hits": alarms, "iterator": None, "resCode": 1}

    def edme_resource_instances(self, class_name: str, page_no: int = 1, page_size: int = 20):
        if page_no < 1 or not 1 <= page_size <= 1000:
            raise ValueError("page_no must be >= 1 and page_size must be between 1 and 1000")
        resources = self._load("edme-resources.json").get(class_name, [])
        start = (page_no - 1) * page_size
        page = resources[start:start + page_size]
        total = len(resources)
        return {
            "objList": page,
            "totalNum": total,
            "pageSize": page_size,
            "totalPageNo": max(1, (total + page_size - 1) // page_size),
            "currentPage": page_no,
        }

    def edme_object_types(self):
        return self._load("edme-metrics.json")["objectTypes"]

    def edme_indicators(self, object_type_id: int | None = None):
        indicators = self._load("edme-metrics.json")["indicators"]
        if object_type_id is not None:
            indicators = [item for item in indicators if item["objectTypeId"] == object_type_id]
        return indicators

    def edme_history(
        self,
        object_ids: list[str] | None = None,
        indicator_ids: list[int] | None = None,
        time_range: str = "LAST_1_HOUR",
    ):
        points = self._load("edme-metrics.json")["history"]
        if object_ids:
            points = [point for point in points if point["objectId"] in object_ids]
        if indicator_ids:
            points = [point for point in points if point["indicatorId"] in indicator_ids]
        return [{**point, "range": time_range} for point in points]

    def overview(self) -> dict:
        clusters = self.clusters()
        hosts = self.hosts()
        vms = self.vms()
        datastores = self.datastores()
        alarms = self.alarms()
        return {
            "cluster_count": len(clusters),
            "host_count": len(hosts),
            "vm_count": len(vms),
            "datastore_count": len(datastores),
            "active_alarm_count": len([a for a in alarms if a.get("status") == "active"]),
            "avg_cpu_usage": round(mean(c.get("cpu_usage", 0) for c in clusters), 2),
            "avg_memory_usage": round(mean(c.get("memory_usage", 0) for c in clusters), 2),
            "capacity_risks": [
                ds for ds in datastores
                if ds.get("capacity_gb") and ds.get("free_gb", 0) / ds["capacity_gb"] < 0.12
            ],
        }


repo = MockRepository()
