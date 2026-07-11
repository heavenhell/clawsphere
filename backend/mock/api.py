from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.mock.repository import repo


router = APIRouter(prefix="/mock", tags=["northbound-mock"])


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
