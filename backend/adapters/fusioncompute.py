from __future__ import annotations

import hashlib
import threading
from statistics import mean
from time import monotonic
from typing import Any

import httpx

from backend.adapters.common import coalesce, extract_items, ratio, resource_id, total
from backend.interfaces.fusioncompute import FusionComputeInterface
from backend.platform_config import PlatformCredentials


class FusionComputeRestAdapter(FusionComputeInterface):
    """FusionCompute 8.10 VRM REST adapter with automatic session and site discovery."""

    def __init__(self, config: PlatformCredentials, client: httpx.Client | None = None):
        self.config = config
        self.api_version = config.api_version or "v6.3"
        self.client = client or httpx.Client(
            base_url=config.base_url(7443),
            verify=config.verify,
            timeout=httpx.Timeout(30, connect=10),
        )
        self._token: str | None = config.session.strip() or None
        self._token_expires_at = float("inf") if self._token else 0.0
        self._site_id = config.site_id
        self._auth_lock = threading.Lock()

    @property
    def _accept(self) -> str:
        return f"application/json;version={self.api_version};charset=UTF-8"

    def _login(self, force: bool = False) -> None:
        with self._auth_lock:
            if not force and self._token and monotonic() < self._token_expires_at:
                return
            if not self.config.can_login:
                raise RuntimeError(
                    "FusionCompute session is missing or expired and no username/password is configured"
                )
            password_hash = hashlib.sha256(self.config.password.encode("utf-8")).hexdigest()
            response = self.client.post("/service/session", headers={
                "Accept": self._accept,
                "Content-Type": "application/json; charset=UTF-8",
                "Accept-Language": "zh_CN",
                "X-Auth-User": self.config.username,
                "X-Auth-Key": password_hash,
                "X-Auth-UserType": "2",
                "X-ENCRYPT-ALGORITHM": "0",
            })
            response.raise_for_status()
            token = response.headers.get("X-Auth-Token")
            if not token:
                raise RuntimeError("FusionCompute login response did not include X-Auth-Token")
            body = response.json() if response.content else {}
            validity_ms = int(body.get("validity") or 600000)
            self._token = token
            self._token_expires_at = monotonic() + max(30, validity_ms / 1000 - 30)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._login()
        headers = {"Accept": self._accept, "X-Auth-Token": self._token or ""}
        headers.update(kwargs.pop("headers", {}))
        response = self.client.request(method, path, headers=headers, **kwargs)
        if response.status_code in {401, 403}:
            self._login(force=True)
            headers["X-Auth-Token"] = self._token or ""
            response = self.client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def sites(self) -> list[dict[str, Any]]:
        items = extract_items(self._request("GET", "/service/sites"), "sites")
        return [{
            "id": resource_id(item),
            "name": str(coalesce(item, "name", "siteName", default=resource_id(item))),
            "status": str(coalesce(item, "status", "runningStatus", default="unknown")).lower(),
            "urn": item.get("urn"),
            "raw": item,
        } for item in items]

    def _resolved_site_id(self) -> str:
        if self._site_id:
            return self._site_id
        sites = self.sites()
        if not sites:
            raise RuntimeError("FusionCompute returned no sites")
        self._site_id = sites[0]["id"]
        return self._site_id

    def clusters(self) -> list[dict[str, Any]]:
        site_id = self._resolved_site_id()
        items = extract_items(self._request("GET", f"/service/sites/{site_id}/clusters"), "clusters")
        return [{
            "id": resource_id(item),
            "name": str(coalesce(item, "name", "clusterName", default=resource_id(item))),
            "status": str(coalesce(item, "status", "runningStatus", default="unknown")).lower(),
            "host_count": int(coalesce(item, "host_count", "hostNum", "hostsNum", default=0) or 0),
            "vm_count": int(coalesce(item, "vm_count", "vmNum", "vmsNum", default=0) or 0),
            "cpu_usage": ratio(coalesce(item, "cpu_usage", "cpuUsage", default=0)),
            "memory_usage": ratio(coalesce(item, "memory_usage", "memoryUsage", default=0)),
            "urn": item.get("urn"),
            "raw": item,
        } for item in items]

    def hosts(self) -> list[dict[str, Any]]:
        site_id = self._resolved_site_id()
        items = extract_items(self._request(
            "GET", f"/service/sites/{site_id}/hosts", params={"limit": 1000, "offset": 0}
        ), "hosts")
        return [{
            "id": resource_id(item),
            "name": str(coalesce(item, "name", "hostName", default=resource_id(item))),
            "cluster_id": resource_id(item, "clusterUrn", "clusterURN", "clusterId"),
            "status": str(coalesce(item, "status", "runningStatus", default="unknown")).lower(),
            "management_ip": str(coalesce(item, "management_ip", "ip", "managementIp", default="")),
            "cpu_usage": ratio(coalesce(item, "cpu_usage", "cpuUsage", default=0)),
            "memory_usage": ratio(coalesce(item, "memory_usage", "memoryUsage", default=0)),
            "urn": item.get("urn"),
            "raw": item,
        } for item in items]

    def vms(self) -> list[dict[str, Any]]:
        site_id = self._resolved_site_id()
        items = extract_items(self._request(
            "GET", f"/service/sites/{site_id}/vms", params={"limit": 1000, "offset": 0}
        ), "vms")
        return [{
            "id": str(coalesce(item, "vmId", "id", default=resource_id(item))),
            "name": str(coalesce(item, "name", "vmName", default=resource_id(item))),
            "status": str(coalesce(item, "status", "runningStatus", default="unknown")).lower(),
            "host_id": resource_id(item, "hostUrn", "hostURN", "hostId"),
            "cpu": int(coalesce(item, "cpu", "cpuCount", "numCpu", default=0) or 0),
            "memory_mb": int(coalesce(item, "memory_mb", "memory", "memorySize", default=0) or 0),
            "ip": str(coalesce(item, "ip", "ipAddress", default="")),
            "urn": item.get("urn"),
            "raw": item,
        } for item in items]

    def datastores(self) -> list[dict[str, Any]]:
        site_id = self._resolved_site_id()
        items = extract_items(self._request(
            "GET", f"/service/sites/{site_id}/datastores", params={"limit": 1000, "offset": 0}
        ), "datastores")
        return [{
            "id": resource_id(item),
            "name": str(coalesce(item, "name", "dataStoreName", default=resource_id(item))),
            "cluster_id": resource_id(item, "clusterUrn", "clusterId"),
            "status": str(coalesce(item, "status", "runningStatus", default="unknown")).lower(),
            "capacity_gb": float(coalesce(item, "capacity_gb", "capacityGB", "totalCapacity", default=0) or 0),
            "free_gb": float(coalesce(item, "free_gb", "freeCapacityGB", "freeCapacity", default=0) or 0),
            "latency_ms": float(coalesce(item, "latency_ms", "latency", default=0) or 0),
            "raw": item,
        } for item in items]

    def alarms(self) -> list[dict[str, Any]]:
        site_id = self._resolved_site_id()
        items = extract_items(self._request(
            "POST", f"/service/sites/{site_id}/alarms/activeAlarms", json={}
        ), "alarms", "activeAlarms")
        return [{
            "id": str(coalesce(item, "serialNo", "alarmSn", "alarmId", "id", default="")),
            "severity": str(coalesce(item, "severity", "alarmLevel", default="unknown")).lower(),
            "object_type": str(coalesce(item, "objectType", "object_type", default="resource")).lower(),
            "object_id": resource_id(item, "objectUrn", "objectId"),
            "name": str(coalesce(item, "alarmName", "name", default="FusionCompute alarm")),
            "status": "active",
            "first_seen": coalesce(item, "occurTime", "firstOccurTime"),
            "raw": item,
        } for item in items]

    def metrics(self) -> list[dict[str, Any]]:
        return []

    def vm_metrics(self, vm_id: str) -> list[dict[str, Any]]:
        vm = next((item for item in self.vms() if item["id"] == vm_id or item["name"] == vm_id), None)
        if not vm:
            raise ValueError(f"virtual machine not found: {vm_id}")
        urn = vm.get("urn") or f"urn:sites:{self._resolved_site_id()}:vms:{vm['id']}"
        metric_names = ["cpu_usage", "cpu_ready", "mem_usage", "mem_balloon", "disk_latency", "net_drop"]
        payload = self._request(
            "POST",
            f"/service/sites/{self._resolved_site_id()}/monitors/objectmetric-realtimedata",
            json=[{"urn": urn, "metricId": metric_names}],
        )
        items = extract_items(payload, "metrics")
        result = []
        for item in items:
            values = item.get("value") if isinstance(item.get("value"), list) else [item]
            for value in values:
                if not isinstance(value, dict):
                    continue
                name = str(coalesce(value, "metricId", "metric", default="unknown"))
                result.append({
                    "metric": name.replace("_", "."),
                    "value": float(coalesce(value, "metricValue", "value", default=0) or 0),
                    "unit": str(coalesce(value, "unit", default="")),
                    "status": "normal",
                })
        return result

    def cluster_daily_growth_gb(self, cluster_id: str) -> float:
        stores = [item for item in self.datastores() if item.get("cluster_id") == cluster_id]
        growth = total(item.get("raw", {}).get("dailyGrowthGB") for item in stores)
        if growth <= 0:
            raise ValueError("FusionCompute did not provide daily capacity growth; configure eDME history for forecasting")
        return growth

    def storage_pool_usage(self, pool_id: str | None = None) -> list[dict[str, Any]]:
        stores = self.datastores()
        return [item for item in stores if not pool_id or item["id"] == pool_id]

    def overview(self) -> dict[str, Any]:
        clusters, hosts, vms, stores, alarms = (
            self.clusters(), self.hosts(), self.vms(), self.datastores(), self.alarms()
        )
        return {
            "cluster_count": len(clusters),
            "host_count": len(hosts),
            "vm_count": len(vms),
            "datastore_count": len(stores),
            "active_alarm_count": len(alarms),
            "avg_cpu_usage": round(mean([item["cpu_usage"] for item in clusters] or [0]), 4),
            "avg_memory_usage": round(mean([item["memory_usage"] for item in clusters] or [0]), 4),
            "capacity_risks": [
                item for item in stores
                if item["capacity_gb"] and item["free_gb"] / item["capacity_gb"] < 0.12
            ],
        }
