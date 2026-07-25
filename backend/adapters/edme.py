from __future__ import annotations

import threading
from time import monotonic
from typing import Any

import httpx

from backend.adapters.common import coalesce, extract_items
from backend.interfaces.dorado import DoradoInterface
from backend.interfaces.edme import EDMEInterface
from backend.platform_config import PlatformCredentials


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
            object_type_id = int(coalesce(object_types[0], "id", "obj_type_id", default=0) or 0)
        payload = self._request(
            "GET", f"/rest/metrics/v1/mgr-svc/obj-types/{object_type_id}/indicators"
        )
        items = extract_items(payload, "indicators")
        return [{**item, "objectTypeId": object_type_id} for item in items]

    def edme_history(
        self,
        object_ids: list[str] | None = None,
        indicator_ids: list[int] | None = None,
        time_range: str = "LAST_1_HOUR",
    ) -> list[dict[str, Any]]:
        object_types = self.edme_object_types()
        if not object_types:
            return []
        object_type_id = int(coalesce(object_types[0], "id", "obj_type_id", default=0) or 0)
        indicators = self.edme_indicators(object_type_id)
        resolved_indicator_ids = indicator_ids or [
            int(coalesce(item, "id", "indicator_id", default=0) or 0) for item in indicators
        ]
        if not object_ids:
            object_ids = [
                str(coalesce(item, "pid", "id", default=""))
                for item in self.edme_resource_instances("SYS_StorageDevice", 1, 1000)["objList"]
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

    def datastores(self) -> list[dict[str, Any]]:
        pools = self.edme_resource_instances("SYS_StoragePool", 1, 1000)["objList"]
        return [{
            "id": str(coalesce(item, "id", "pid", default="")),
            "name": str(coalesce(item, "name", default="")),
            "cluster_id": str(coalesce(item, "parentId", "storageDeviceId", default="")),
            "status": str(coalesce(item, "healthStatus", "status", default="unknown")).lower(),
            "capacity_gb": float(coalesce(item, "totalCapacityGB", "totalCapacity", default=0) or 0),
            "free_gb": float(coalesce(item, "freeCapacityGB", "freeCapacity", default=0) or 0),
            "latency_ms": float(coalesce(item, "latency", "latencyMs", default=0) or 0),
            "raw": item,
        } for item in pools]

    def storage_pool_usage(self, pool_id: str | None = None) -> list[dict[str, Any]]:
        stores = self.datastores()
        return [item for item in stores if not pool_id or item["id"] == pool_id]
