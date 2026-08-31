from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from time import monotonic
from typing import Any

import httpx

from backend.adapters.common import coalesce, extract_items, ratio
from backend.interfaces.dorado import DoradoInterface
from backend.interfaces.edme import EDMEInterface
from backend.platform_config import PlatformCredentials


LOGGER = logging.getLogger(__name__)


class EDMERestAdapter(EDMEInterface, DoradoInterface):
    """eDME 24.1 operations-plane adapter with token refresh and response normalization."""

    def __init__(self, config: PlatformCredentials, client: httpx.Client | None = None):
        self.config = config
        self.client = client or httpx.Client(
            base_url=config.base_url(26335),
            verify=config.verify,
            timeout=httpx.Timeout(30, connect=10),
        )
        self._token: str | None = config.session.strip() or None
        self._token_expires_at = float("inf") if self._token else 0.0
        self._auth_lock = threading.Lock()

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Charset": "utf8",
            "Content-Type": "application/json",
        }

    def _login(self, force: bool = False) -> None:
        with self._auth_lock:
            if not force and self._token and monotonic() < self._token_expires_at:
                return
            if not self.config.can_login:
                raise RuntimeError("eDME session is missing or expired and no username/password is configured")
            response = self.client.put(
                "/rest/plat/smapp/v1/sessions",
                headers=self._headers(),
                json={
                    "grantType": "password",
                    "userName": self.config.username,
                    "value": self.config.password,
                },
            )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("accessSession")
            if not token:
                raise RuntimeError("eDME login response did not include accessSession")
            expires = int(payload.get("expires") or 1800)
            self._token = str(token)
            self._token_expires_at = monotonic() + max(30, expires - 30)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._login()
        headers = self._headers()
        headers["X-Auth-Token"] = self._token or ""
        headers.update(kwargs.pop("headers", {}))
        response = self.client.request(method, path, headers=headers, **kwargs)
        if response.status_code in {401, 403}:
            self._login(force=True)
            headers["X-Auth-Token"] = self._token or ""
            response = self.client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def edme_current_alarms(
        self,
        severity: int | None = None,
        iterator: str | None = None,
    ) -> dict[str, Any]:
        query = {"cleared": 0}
        if severity is not None:
            query["severity"] = severity
        payload = self._request(
            "POST",
            "/rest/alarmmgmt/v1/alarms/current-alarm/query",
            json={"query": query, "iterator": iterator},
        )
        return {
            "hits": payload.get("hits") or [],
            "iterator": payload.get("iterator"),
            "resCode": payload.get("resCode", 1),
        }

    def edme_resource_instances(
        self,
        class_name: str,
        page_no: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"/rest/resourcedb/v1/instances/{class_name}",
            params={"pageNo": page_no, "pageSize": page_size},
        )
        return {
            "objList": payload.get("objList") or [],
            "totalNum": int(payload.get("totalNum") or 0),
            "pageSize": int(payload.get("pageSize") or page_size),
            "totalPageNo": int(payload.get("totalPageNo") or 1),
            "currentPage": int(payload.get("currentPage") or page_no),
        }

    def edme_object_types(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/rest/metrics/v1/mgr-svc/obj-types")
        return extract_items(payload, "object_types", "obj_types")

    def edme_indicators(self, object_type_id: int | None = None) -> list[dict[str, Any]]:
        if object_type_id is None:
            object_types = self.edme_object_types()
            if not object_types:
                return []
            object_type_id = int(
                coalesce(object_types[0], "obj_type_id", "id", default=0) or 0
            )
        payload = self._request(
            "GET", f"/rest/metrics/v1/mgr-svc/obj-types/{object_type_id}/indicators"
        )
        data = payload.get("data") or {}
        ids = data.get("indicator_ids") or []
        return [
            {"id": int(item), "objectTypeId": object_type_id}
            for item in ids
            if item is not None
        ]

    def edme_history(
        self,
        object_ids: list[str] | None = None,
        indicator_ids: list[int] | None = None,
        time_range: str = "LAST_1_HOUR",
    ) -> list[dict[str, Any]]:
        object_types = self.edme_object_types()
        if not object_types:
            return []
        object_type_id = int(
            coalesce(object_types[0], "obj_type_id", "id", default=0) or 0
        )
        indicators = self.edme_indicators(object_type_id)
        resolved_indicator_ids = indicator_ids or [
            int(coalesce(item, "id", "indicator_id", default=0) or 0) for item in indicators
        ]
        if not object_ids:
            try:
                object_ids = [
                    str(coalesce(item, "pid", "id", default=""))
                    for item in self.edme_resource_instances("SYS_StorageDevice", 1, 1000)["objList"]
                ]
            except Exception:
                # resourcedb may not be granted; fall back to managed storage ids.
                object_ids = [
                    item["storage_id"]
                    for item in self.edme_storage_pools()
                    if item.get("storage_id")
                ]
        if not object_ids or not resolved_indicator_ids:
            return []
        payload = self._request(
            "POST",
            "/rest/metrics/v1/data-svc/history-data/action/query",
            json={
                "obj_type_id": object_type_id,
                "indicator_ids": resolved_indicator_ids,
                "obj_ids": object_ids,
                "interval": "MINUTE",
                "range": time_range,
            },
        )
        return extract_items(payload, "history", "series")

    def edme_storage_pools(self, limit: int = 1000, offset: int = 0) -> list[dict[str, Any]]:
        """Query storage pools via storagemgmt (northbound permission is configured).

        Falls back to an empty list on failure so callers can keep their
        existing normalization path. Capacity is converted from MB to GB.
        """
        payload = self._request(
            "POST",
            "/rest/storagemgmt/v1/storagepools/query",
            json={"limit": limit, "offset": offset},
        )
        items = extract_items(payload, "datas", "storage_pools", "data")
        return [
            {
                "id": str(coalesce(item, "id", default="")),
                "name": str(coalesce(item, "name", default="")),
                "storage_id": str(coalesce(item, "storage_id", default="")),
                "storage_name": str(coalesce(item, "storage_name", default="")),
                "status": str(
                    coalesce(item, "health_status", "status", default="unknown")
                ).lower(),
                "running_status": str(coalesce(item, "running_status", default="")),
                "capacity_gb": float(coalesce(item, "total_capacity", default=0) or 0) / 1024,
                "free_gb": float(coalesce(item, "free_capacity", default=0) or 0) / 1024,
                "used_gb": float(coalesce(item, "consumed_capacity", default=0) or 0) / 1024,
                "usage_type": str(coalesce(item, "usage_type", default="")),
                "disk_types": coalesce(item, "disk_types", default=[]) or [],
                "raid_level": coalesce(item, "raid_level", default=[]) or [],
                "raw": item,
            }
            for item in items
        ]

    def datastores(self) -> list[dict[str, Any]]:
        pools = self.edme_storage_pools()
        return [{
            "id": item["id"],
            "name": item["name"],
            "cluster_id": item["storage_id"],
            "status": item["status"],
            "capacity_gb": item["capacity_gb"],
            "free_gb": item["free_gb"],
            "latency_ms": 0.0,
            "raw": item["raw"],
        } for item in pools]

    def storage_pool_usage(self, pool_id: str | None = None) -> list[dict[str, Any]]:
        stores = self.datastores()
        return [item for item in stores if not pool_id or item["id"] == pool_id]

    # ------------------------------------------------------------------
    # Virtualization (vmmgmt) — sites / clusters / hosts / VMs
    # ------------------------------------------------------------------
    def _vmmgmt_query(self, resource: str) -> list[dict[str, Any]]:
        payload = self._request(
            "POST",
            f"/rest/vmmgmt/v1/{resource}/query",
            json={"limit": 1000, "offset": 0},
        )
        return extract_items(payload, resource)

    def virtual_sites(self) -> list[dict[str, Any]]:
        return [{
            "id": str(coalesce(item, "id", default="")),
            "name": str(coalesce(item, "name", default="")),
            "status": "normal",
            "version": str(coalesce(item, "version", default="")),
            "cpu_usage": ratio(coalesce(item, "cpu_usage", default=0)),
            "memory_usage": ratio(coalesce(item, "memory_usage", default=0)),
            "host_count": int(coalesce(item, "host_num", default=0) or 0),
            "vm_count": int(coalesce(item, "vm_num", default=0) or 0),
            "cluster_count": int(coalesce(item, "cluster_num", default=0) or 0),
            "ip_address": str(coalesce(item, "ip_address", default="")),
            "raw": item,
        } for item in self._vmmgmt_query("sites")]

    def virtual_clusters(self) -> list[dict[str, Any]]:
        return [{
            "id": str(coalesce(item, "id", default="")),
            "name": str(coalesce(item, "name", default="")),
            "status": "normal",
            "cpu_usage": ratio(coalesce(item, "cpu_usage", default=0)),
            "memory_usage": ratio(coalesce(item, "memory_usage", default=0)),
            "host_count": int(coalesce(item, "host_num", default=0) or 0),
            "vm_count": int(coalesce(item, "vm_num", default=0) or 0),
            "datastore_count": int(coalesce(item, "datastore_num", default=0) or 0),
            "site_id": str(coalesce(item, "site_id", default="")),
            "vr_type": str(coalesce(item, "vr_type", default="")),
            "raw": item,
        } for item in self._vmmgmt_query("clusters")]

    def virtual_hosts(self) -> list[dict[str, Any]]:
        return [{
            "id": str(coalesce(item, "id", default="")),
            "name": str(coalesce(item, "name", default="")),
            "cluster_id": str(coalesce(item, "cluster_id", default="")),
            "cluster_name": str(coalesce(item, "cluster_name", default="")),
            "management_ip": str(coalesce(item, "ip_address", default="")).split(";")[0],
            "status": str(coalesce(item, "status", default="normal")).lower(),
            "cpu_usage": ratio(coalesce(item, "cpu_usage", default=0)),
            "memory_usage": ratio(coalesce(item, "memory_usage", default=0)),
            "vm_count": int(coalesce(item, "vm_num", default=0) or 0),
            "sn": str(coalesce(item, "sn", default="")),
            "raw": item,
        } for item in self._vmmgmt_query("hosts")]

    def _query_vms(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        payload = self._request("POST", "/rest/vmmgmt/v1/vms/query", json=query)
        items = extract_items(payload, "vms")
        cpu = lambda item: (coalesce(item, "cpu", default={}) or {}).get("quantity")
        mem = lambda item: (coalesce(item, "memory", default={}) or {}).get("quantity_size")
        return [{
            "id": str(coalesce(item, "id", default="")),
            "name": str(coalesce(item, "name", default="")),
            "status": str(coalesce(item, "status", default="")).lower(),
            "host_id": str(coalesce(item, "host_id", default="")),
            "host_name": str(coalesce(item, "host_name", default="")),
            "cluster_id": str(coalesce(item, "cluster_id", default="")),
            "cluster_name": str(coalesce(item, "cluster_name", default="")),
            "cpu": int(cpu(item) or 0),
            "memory_mb": int(mem(item) or 0),
            "ip": str(coalesce(item, "ip_address", default="")),
            "is_template": bool(coalesce(item, "is_template", default=False)),
            "disk_num": int(coalesce(item, "disk_num", default=0) or 0),
            "raw": item,
        } for item in items]

    def virtual_vms(
        self,
        site_id: str | None = None,
        cluster_id: str | None = None,
        name: str | None = None,
        status: str | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List VMs, working around vmmgmt's missing pagination.

        `vms/query` ignores limit/offset and only ever returns its first page,
        so an unfiltered call silently under-reports on any real site. Filtering
        *is* honored, so the unfiltered case is answered by fanning out one
        query per cluster and merging — each cluster is well under a page on
        realistic sites, and `virtual_clusters()` is the authoritative cluster
        list. Callers that pass a filter already narrowed the query themselves
        and get a single request.

        Completeness is checked against each cluster's own `vm_num` rather than
        assumed: a short result is logged, because a silently truncated
        inventory is exactly what makes the agent state a wrong VM count.
        """
        if site_id or cluster_id or name or status:
            query: dict[str, Any] = {}
            if site_id:
                query["site_id"] = site_id
            if cluster_id:
                query["cluster_id"] = cluster_id
            if name:
                query["name"] = name
            if status:
                query["status"] = [status] if isinstance(status, str) else list(status)
            return self._query_vms(query)

        clusters = self.virtual_clusters()
        if not clusters:
            return self._query_vms({})
        merged: dict[str, dict[str, Any]] = {}
        for cluster in clusters:
            identifier = cluster.get("id")
            if not identifier:
                continue
            found = self._query_vms({"cluster_id": identifier})
            expected = int(cluster.get("vm_count") or 0)
            if expected and len(found) < expected:
                LOGGER.warning(
                    "eDME vms/query returned %s of %s VMs for cluster %s; "
                    "inventory is incomplete and counts derived from it will be low",
                    len(found), expected, identifier,
                )
            for item in found:
                if item["id"]:
                    merged[item["id"]] = item
        return list(merged.values())

    # ------------------------------------------------------------------
    # Alarms (alarmmgmt) — normalized for the agent-facing alarm contract
    # ------------------------------------------------------------------
    _SEVERITY_LABELS = {1: "critical", 2: "major", 3: "minor", 4: "warning"}
    _OBJECT_TYPE_MAP = {
        "主机": "host",
        "虚拟机": "vm",
        "存储": "storage",
        "存储池": "storage",
        "存储设备": "storage",
        "数据存储": "datastore",
        "站点": "site",
        "集群": "cluster",
        "网络": "network",
        "数据库": "database",
        "硬盘": "storage",
        "硬盘域": "storage",
        "控制器": "controller",
    }

    @staticmethod
    def _utc_millis_to_iso(value: Any) -> str | None:
        try:
            millis = int(value or 0)
        except (TypeError, ValueError):
            return None
        if millis <= 0:
            return None
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()

    def virtual_alarms(self) -> list[dict[str, Any]]:
        payload = self._request(
            "POST",
            "/rest/alarmmgmt/v1/alarms/current-alarm/query",
            json={"query": {"cleared": 0}, "iterator": None},
        )
        hits = payload.get("hits") or []
        out: list[dict[str, Any]] = []
        for item in hits:
            moi = str(coalesce(item, "moi", default="") or "")
            me_name = str(coalesce(item, "meName", default="") or "")
            object_type, object_id = self._parse_moi(moi, me_name)
            severity_num = int(coalesce(item, "severity", default=0) or 0)
            out.append({
                "id": str(coalesce(item, "alarmId", default="")),
                "severity": self._SEVERITY_LABELS.get(severity_num, "unknown"),
                "severity_code": severity_num,
                "name": str(coalesce(item, "alarmName", default="")),
                "object_type": object_type,
                "object_id": object_id,
                "status": "active" if not int(coalesce(item, "cleared", default=0) or 0) else "cleared",
                "first_seen": self._utc_millis_to_iso(coalesce(item, "firstOccurUtc", default=0)),
                "last_seen": self._utc_millis_to_iso(coalesce(item, "latestOccurUtc", default=0)),
                "me_name": me_name,
                "additional_information": str(coalesce(item, "additionalInformation", default="")),
                "raw": item,
            })
        return out

    @classmethod
    def _parse_moi(cls, moi: str, me_name: str = "") -> tuple[str, str]:
        """Extract object type and object id from eDME MOI text, with meName fallback.

        Example: "对象类型=主机, 对象名称=HOST-01, 站点IP=..., 主机URN=urn:...:hosts:178"
        """
        obj_type = "unknown"
        obj_id = ""
        if moi:
            for part in moi.split(","):
                part = part.strip()
                if "=" not in part:
                    continue
                key, _, value = part.partition("=")
                key = key.strip()
                value = value.strip()
                if key == "对象类型":
                    obj_type = cls._OBJECT_TYPE_MAP.get(value, value or "unknown")
                elif key.endswith("URN") and not obj_id:
                    if "hosts" in value:
                        obj_type = "host"
                    elif "vms" in value:
                        obj_type = "vm"
                    elif "clusters" in value:
                        obj_type = "cluster"
                    elif "datastores" in value:
                        obj_type = "datastore"
                    obj_id = value
                elif key in ("虚拟机ID", "虚拟机名称") and not obj_id:
                    obj_type = "vm"
                    obj_id = value
                elif key == "站点IP" and obj_type == "unknown" and not obj_id:
                    obj_type = "site"
                    obj_id = value
                elif key in ("主机名称",) and not obj_id:
                    obj_type = "host"
                    obj_id = value
                elif key in ("硬盘槽位号", "硬盘序列号", "硬盘域名字", "硬盘域ID") and obj_type == "unknown":
                    obj_type = "storage"
                elif key in ("对象名称",) and not obj_id:
                    obj_id = value
        if obj_type == "unknown" and me_name:
            upper = me_name.upper()
            if "STORAGE" in upper or "SNS" in upper or "DORADO" in upper or "OCEANSTOR" in upper:
                obj_type = "storage"
            elif me_name in ("Ac Controller", "AC", "SMC"):
                obj_type = "network"
            elif me_name in ("VRM", "FusionCompute"):
                obj_type = "cluster"
            elif me_name == "OSS":
                obj_type = "platform"
            else:
                obj_type = me_name
        return obj_type, obj_id
