from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

from backend.interfaces.dorado import DoradoInterface
from backend.interfaces.fusioncompute import FusionComputeInterface


class MockRepository(FusionComputeInterface, DoradoInterface):
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

    def storage_pool_usage(self, pool_id: str | None = None):
        pools = self.datastores()
        if pool_id:
            pools = [pool for pool in pools if pool["id"] == pool_id]
        return pools

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
