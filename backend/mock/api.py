from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from backend.mock.repository import repo


router = APIRouter(prefix="/mock", tags=["northbound-mock"])
EDME_MOCK_TOKEN = "edme-mock-access-session"


def _require_edme_token(x_auth_token: str | None = Header(default=None, alias="X-Auth-Token")):
    if x_auth_token != EDME_MOCK_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or expired eDME X-Auth-Token")
    return x_auth_token


@router.get("/fusioncompute/sites")
def list_sites():
    return repo.sites()


@router.get("/fusioncompute/clusters")
def list_clusters():
    return repo.clusters()


@router.get("/fusioncompute/hosts")
def list_hosts():
    return repo.hosts()


@router.get("/fusioncompute/vms")
def list_vms():
    return repo.vms()


@router.get("/fusioncompute/vms/{vm_id}/metrics")
def vm_metrics(vm_id: str):
    points = [point for point in repo.metrics() if point.get("resource_id") == vm_id]
    if not points:
        raise HTTPException(status_code=404, detail="VM metrics not found")
    return points


@router.get("/fusioncompute/alarms")
def list_alarms():
    return repo.alarms()


@router.get("/dorado/storage-pools")
def storage_pools(pool_id: str | None = None):
    return repo.storage_pool_usage(pool_id)


@router.put("/edme/rest/plat/smapp/v1/sessions")
def create_edme_session(payload: dict[str, Any]):
    if payload.get("grantType") != "password" or not payload.get("userName") or not payload.get("value"):
        raise HTTPException(status_code=400, detail="grantType, userName and value are required")
    return {
        "accessSession": EDME_MOCK_TOKEN,
        "roaRand": "edme-mock-roa-rand",
        "expires": 1800,
        "additionalInfo": {"expires": "2", "passwdStatus": "normal"},
    }


@router.delete("/edme/rest/plat/smapp/v1/sessions", status_code=204)
def delete_edme_session(_token: str = Depends(_require_edme_token)):
    return None


@router.post("/edme/rest/alarmmgmt/v1/alarms/current-alarm/query")
def query_edme_current_alarms(payload: dict[str, Any], _token: str = Depends(_require_edme_token)):
    query = payload.get("query") or {}
    return repo.edme_current_alarms(query.get("severity"), payload.get("iterator"))


@router.get("/edme/rest/resourcedb/v1/instances/{class_name}")
def query_edme_resources(
    class_name: str,
    pageNo: int = 1,
    pageSize: int = 20,
    _token: str = Depends(_require_edme_token),
):
    return repo.edme_resource_instances(class_name, pageNo, pageSize)


@router.get("/edme/rest/resourcedb/v1/instances/{class_name}/{instance_id}")
def get_edme_resource(
    class_name: str,
    instance_id: str,
    _token: str = Depends(_require_edme_token),
):
    resources = repo.edme_resource_instances(class_name, 1, 1000)["objList"]
    resource = next((item for item in resources if item["id"] == instance_id), None)
    if not resource:
        raise HTTPException(status_code=404, detail="eDME resource not found")
    return resource


@router.get("/edme/rest/metrics/v1/mgr-svc/obj-types")
def get_edme_object_types(_token: str = Depends(_require_edme_token)):
    return {"status_code": 200, "error_code": 0, "error_msg": "Successful.", "data": repo.edme_object_types()}


@router.get("/edme/rest/metrics/v1/mgr-svc/obj-types/{object_type_id}/indicators")
def get_edme_indicators(object_type_id: int, _token: str = Depends(_require_edme_token)):
    return {
        "status_code": 200,
        "error_code": 0,
        "error_msg": "Successful.",
        "data": repo.edme_indicators(object_type_id),
    }


@router.post("/edme/rest/metrics/v1/mgr-svc/indicators")
def get_edme_indicator_metadata(
    indicator_ids: list[int],
    _token: str = Depends(_require_edme_token),
):
    indicators = [item for item in repo.edme_indicators() if item["id"] in indicator_ids]
    return {"status_code": 200, "error_code": 0, "error_msg": "Successful.", "data": indicators}


@router.post("/edme/rest/metrics/v1/data-svc/history-data/action/query")
def query_edme_history(payload: dict[str, Any], _token: str = Depends(_require_edme_token)):
    points = repo.edme_history(
        payload.get("obj_ids"),
        payload.get("indicator_ids"),
        payload.get("range", "LAST_1_HOUR"),
    )
    return {"status_code": 200, "error_code": 0, "error_msg": "Successful.", "data": points}
