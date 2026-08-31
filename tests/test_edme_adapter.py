"""eDME 24.1 adapter tests driven by the real northbound response shapes.

The payloads below mirror what the live platform returns — percent-valued
usage, `;`-joined host IPs, capacity in MB, alarm MOI as a comma-separated
Chinese key=value string, indicators as a bare `indicator_ids` array — because
every normalization bug this adapter can have is a bug about *that* shape, not
about a tidy invented one.

The *shapes* are real; the *values* must never be. Addresses come from the
RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24) and every name,
serial and URN is a made-up placeholder. Never seed a fixture from a live
platform response or a runtime log: this repository is public, and a management
IP or hostname copied out of an ops log is reconnaissance material even though
it is not a credential. `test_no_real_environment_data.py` enforces the part of
this that a machine can check.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from backend.adapters.edme import EDMERestAdapter
from backend.platform_config import PlatformCredentials, load_runtime_config
from backend.providers import ConfiguredRepository


# --- Real response fixtures --------------------------------------------------

SITES = {"sites": [{
    "id": "urn:sites:DEMO", "name": "site-demo", "version": "8.6.1",
    "cpu_usage": 41, "memory_usage": 63, "host_num": 6, "vm_num": 58,
    "cluster_num": 2, "ip_address": "192.0.2.10",
}]}

CLUSTERS = {"clusters": [
    {
        "id": "urn:sites:DEMO:clusters:11", "name": "ManageCluster",
        "cpu_usage": 38, "memory_usage": 55, "host_num": 2, "vm_num": 22,
        "datastore_num": 3, "site_id": "urn:sites:DEMO", "vr_type": "CNA",
    },
    {
        "id": "urn:sites:DEMO:clusters:12", "name": "ServiceCluster",
        "cpu_usage": 72, "memory_usage": 81, "host_num": 4, "vm_num": 36,
        "datastore_num": 5, "site_id": "urn:sites:DEMO", "vr_type": "CNA",
    },
]}

HOSTS = {"hosts": [{
    "id": "urn:sites:DEMO:hosts:178", "name": "HOST-01",
    "cluster_id": "urn:sites:DEMO:clusters:11", "cluster_name": "ManageCluster",
    # The platform joins management and service IPs with a semicolon.
    "ip_address": "192.0.2.11;198.51.100.11",
    "status": "NORMAL", "cpu_usage": 58, "memory_usage": 77, "vm_num": 12,
    "sn": "SN-DEMO-0000000001",
}]}


def _vm(identifier: str, name: str, cluster: str, status: str = "running") -> dict:
    return {
        "id": identifier, "name": name, "status": status,
        "host_id": "urn:sites:DEMO:hosts:178", "host_name": "HOST-01",
        "cluster_id": cluster, "cluster_name": "ManageCluster",
        # cpu/memory arrive as nested objects, not scalars.
        "cpu": {"quantity": 4}, "memory": {"quantity_size": 8192},
        "ip_address": "198.51.100.51", "is_template": False, "disk_num": 2,
    }


ALARMS = {"resCode": 1, "iterator": None, "hits": [
    {
        "alarmId": "0x8100302", "alarmName": "主机CPU使用率超过阈值", "severity": 2,
        "cleared": 0, "meName": "VRM",
        "moi": "对象类型=主机, 对象名称=HOST-01, 站点IP=192.0.2.10, 主机URN=urn:sites:DEMO:hosts:178",
        "firstOccurUtc": 1783792800000, "latestOccurUtc": 1783793700000,
        "additionalInformation": "CPU 使用率 91%，持续 15 分钟",
    },
    {
        # No 对象名称, so the URN is what identifies the object.
        "alarmId": "0x8100411", "alarmName": "虚拟机内存不足", "severity": 1,
        "cleared": 0, "meName": "VRM",
        "moi": "对象类型=虚拟机, 虚拟机URN=urn:sites:DEMO:vms:2049",
        "firstOccurUtc": 1783793100000, "latestOccurUtc": 1783794000000,
    },
    {
        # Storage alarms arrive with no MOI at all; meName is the only clue.
        "alarmId": "0x2003001", "alarmName": "Storage pool capacity exceeded", "severity": 3,
        "cleared": 1, "meName": "storage-demo-01", "moi": "",
        "firstOccurUtc": 1783790000000, "latestOccurUtc": 0,
    },
]}

STORAGE_POOLS = {"datas": [{
    "id": "0", "name": "pool-demo-001", "storage_id": "storage-demo-1",
    "storage_name": "storage-demo-01", "health_status": "NORMAL", "running_status": "Online",
    # Capacity is reported in MB.
    "total_capacity": 26214400, "free_capacity": 19267584, "consumed_capacity": 6946816,
    "usage_type": "BLOCK", "disk_types": ["SSD"], "raid_level": ["RAID5"],
}]}

OBJECT_TYPES = {"object_types": [{
    # `id` and `obj_type_id` differ, and the metrics API keys off obj_type_id.
    "id": 3, "obj_type_id": 1001, "name": "SYS_StorageDevice",
}]}

INDICATORS = {"data": {"indicator_ids": [12001, 12002, 12003]}}


# --- Harness -----------------------------------------------------------------

def _adapter(handler, session: str = "edme-session") -> tuple[EDMERestAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(
        base_url="https://edme.local:26335", transport=httpx.MockTransport(recording)
    )
    adapter = EDMERestAdapter(
        PlatformCredentials(ip="edme.local", session=session), client=client
    )
    return adapter, seen


def _routes(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    table = {
        "/rest/vmmgmt/v1/sites/query": SITES,
        "/rest/vmmgmt/v1/clusters/query": CLUSTERS,
        "/rest/vmmgmt/v1/hosts/query": HOSTS,
        "/rest/alarmmgmt/v1/alarms/current-alarm/query": ALARMS,
        "/rest/storagemgmt/v1/storagepools/query": STORAGE_POOLS,
        "/rest/metrics/v1/mgr-svc/obj-types": OBJECT_TYPES,
        "/rest/metrics/v1/mgr-svc/obj-types/1001/indicators": INDICATORS,
    }
    if path in table:
        return httpx.Response(200, json=table[path])
    return httpx.Response(404, json={})


# --- Virtualization normalization -------------------------------------------

def test_percent_usage_and_joined_ips_are_normalized():
    adapter, _ = _adapter(_routes)

    site = adapter.virtual_sites()[0]
    host = adapter.virtual_hosts()[0]

    # The platform reports 41 meaning 41%; downstream code compares against
    # ratios, so shipping the raw number would read as 4100% utilization.
    assert site["cpu_usage"] == 0.41
    assert site["vm_count"] == 58
    assert host["memory_usage"] == 0.77
    # Only the management IP is useful; the service IP must not ride along.
    assert host["management_ip"] == "192.0.2.11"
    assert host["status"] == "normal"


def test_nested_cpu_and_memory_objects_are_flattened():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            return httpx.Response(200, json={"vms": [_vm("urn:vms:1", "kylin-perf-1", "c-1")]})
        return _routes(request)

    adapter, _ = _adapter(handler)
    vm = adapter.virtual_vms(cluster_id="c-1")[0]

    assert vm["cpu"] == 4
    assert vm["memory_mb"] == 8192
    assert vm["name"] == "kylin-perf-1"


# --- The pagination workaround ----------------------------------------------

def test_unfiltered_vm_listing_fans_out_across_clusters():
    """vms/query ignores limit/offset, so one unfiltered call under-reports.

    The adapter must instead ask per cluster and merge, or the agent answers
    "多少台 VM" with whatever fit on the first page.
    """
    per_cluster = {
        "urn:sites:DEMO:clusters:11": [_vm("urn:vms:1", "vm-a", "urn:sites:DEMO:clusters:11")],
        "urn:sites:DEMO:clusters:12": [
            _vm("urn:vms:2", "vm-b", "urn:sites:DEMO:clusters:12"),
            _vm("urn:vms:3", "vm-c", "urn:sites:DEMO:clusters:12"),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            body = json.loads(request.content)
            return httpx.Response(200, json={"vms": per_cluster.get(body.get("cluster_id"), [])})
        return _routes(request)

    adapter, seen = _adapter(handler)
    vms = adapter.virtual_vms()

    assert {item["id"] for item in vms} == {"urn:vms:1", "urn:vms:2", "urn:vms:3"}
    queries = [r for r in seen if r.url.path == "/rest/vmmgmt/v1/vms/query"]
    assert len(queries) == 2
    assert all(json.loads(r.content).get("cluster_id") for r in queries)


def test_a_vm_reported_by_two_clusters_is_not_counted_twice():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            return httpx.Response(200, json={"vms": [_vm("urn:vms:9", "shared", "c")]})
        return _routes(request)

    adapter, _ = _adapter(handler)
    # Both cluster queries answer with the same VM; merging is by id, so the
    # inventory must not double-count it into a wrong total.
    assert len(adapter.virtual_vms()) == 1


def _empty_vms(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/rest/vmmgmt/v1/vms/query":
        return httpx.Response(200, json={"vms": []})
    return _routes(request)


def test_naming_one_cluster_stays_a_single_request():
    adapter, seen = _adapter(_empty_vms)
    adapter.virtual_vms(cluster_id="urn:sites:DEMO:clusters:11")

    # The caller named the one cluster to ask; fanning out would be wasted calls.
    queries = [r for r in seen if r.url.path == "/rest/vmmgmt/v1/vms/query"]
    assert len(queries) == 1
    assert json.loads(queries[0].content)["cluster_id"] == "urn:sites:DEMO:clusters:11"


def test_a_status_filter_is_pushed_into_each_cluster_query():
    """A status filter does not bound the result to one page.

    "所有已停止的虚拟机" across a whole site can exceed the page limit just as an
    unfiltered listing can, so the filter has to ride along with the fan-out
    rather than replace it.
    """
    adapter, seen = _adapter(_empty_vms)
    adapter.virtual_vms(status="stopped")

    queries = [json.loads(r.content) for r in seen if r.url.path == "/rest/vmmgmt/v1/vms/query"]
    assert len(queries) == 2
    assert all(q["status"] == ["stopped"] for q in queries)
    assert {q["cluster_id"] for q in queries} == {
        "urn:sites:DEMO:clusters:11", "urn:sites:DEMO:clusters:12",
    }


def test_site_id_narrows_which_clusters_are_visited():
    adapter, seen = _adapter(_empty_vms)
    adapter.virtual_vms(site_id="urn:sites:DEMO")
    both = [r for r in seen if r.url.path == "/rest/vmmgmt/v1/vms/query"]

    adapter, seen = _adapter(_empty_vms)
    adapter.virtual_vms(site_id="urn:sites:OTHER")
    none_matching = [r for r in seen if r.url.path == "/rest/vmmgmt/v1/vms/query"]

    assert len(both) == 2
    # No cluster belongs to that site, so there is nothing to fan out over and
    # the single fallback query carries the site filter.
    assert len(none_matching) == 1
    assert json.loads(none_matching[0].content)["site_id"] == "urn:sites:OTHER"


def test_a_filtered_listing_does_not_warn_about_being_short(caplog):
    """`vm_count` is the cluster's total, so a filtered query returning fewer is
    correct, not evidence of truncation. Warning there would train the reader to
    ignore the warning that matters."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            return httpx.Response(200, json={"vms": [_vm("urn:vms:1", "vm-a", "c", "stopped")]})
        return _routes(request)

    adapter, _ = _adapter(handler)
    with caplog.at_level(logging.WARNING, logger="backend.adapters.edme"):
        adapter.virtual_vms(status="stopped")

    assert not [r for r in caplog.records if "incomplete" in r.message]


def test_a_short_cluster_listing_is_logged_rather_than_passed_off_as_complete(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            # Cluster 11 claims vm_num=22 but only one VM comes back.
            return httpx.Response(200, json={"vms": [_vm("urn:vms:1", "vm-a", "c")]})
        return _routes(request)

    adapter, _ = _adapter(handler)
    with caplog.at_level(logging.WARNING, logger="backend.adapters.edme"):
        adapter.virtual_vms()

    # Silence here is what produces a confidently wrong VM count downstream.
    assert any("incomplete" in record.message for record in caplog.records)


# --- Alarms ------------------------------------------------------------------

def test_alarm_moi_yields_object_type_and_id():
    adapter, _ = _adapter(_routes)
    alarms = adapter.virtual_alarms()
    host_alarm, vm_alarm, storage_alarm = alarms

    assert (host_alarm["object_type"], host_alarm["object_id"]) == ("host", "HOST-01")
    # With no 对象名称 present the URN carries the identity.
    assert vm_alarm["object_type"] == "vm"
    assert vm_alarm["object_id"] == "urn:sites:DEMO:vms:2049"
    # No MOI at all: meName is the only signal left.
    assert storage_alarm["object_type"] == "storage"


def test_numeric_severity_becomes_the_label_the_agent_filters_on():
    adapter, _ = _adapter(_routes)
    alarms = adapter.virtual_alarms()

    assert [item["severity"] for item in alarms] == ["major", "critical", "minor"]
    # The raw code is kept: `_summarize_tool_result` groups on it for big lists.
    assert alarms[0]["severity_code"] == 2


def test_cleared_flag_and_epoch_millis_are_translated():
    adapter, _ = _adapter(_routes)
    alarms = adapter.virtual_alarms()

    assert alarms[0]["status"] == "active"
    assert alarms[2]["status"] == "cleared"
    assert alarms[0]["first_seen"] == "2026-07-11T18:00:00+00:00"
    # A zero timestamp means "never", not 1970.
    assert alarms[2]["last_seen"] is None


# --- Storage -----------------------------------------------------------------

def test_storage_capacity_is_converted_from_mb_to_gb():
    adapter, _ = _adapter(_routes)
    pool = adapter.edme_storage_pools()[0]
    datastore = adapter.datastores()[0]

    assert pool["capacity_gb"] == 25600.0
    assert pool["free_gb"] == 18816.0
    # Reporting MB as GB would overstate free space by 1024x and silence every
    # capacity risk check downstream.
    assert datastore["capacity_gb"] == 25600.0
    assert datastore["cluster_id"] == "storage-demo-1"
    assert datastore["status"] == "normal"


# --- Metrics catalog ---------------------------------------------------------

def test_indicator_ids_array_is_expanded_against_obj_type_id():
    adapter, seen = _adapter(_routes)
    indicators = adapter.edme_indicators()

    # obj_type_id (1001), not id (3), addresses the indicators endpoint.
    assert any(r.url.path.endswith("/obj-types/1001/indicators") for r in seen)
    assert [item["id"] for item in indicators] == [12001, 12002, 12003]
    assert indicators[0]["objectTypeId"] == 1001


def test_history_falls_back_to_storage_ids_when_resourcedb_is_denied():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/rest/resourcedb/"):
            # A northbound account without resourcedb permission gets 403 here;
            # the token is fine, so re-login cannot fix it.
            return httpx.Response(403, json={"error": "no permission"})
        if request.url.path.endswith("/history-data/action/query"):
            return httpx.Response(200, json={"history": [{"obj_id": "storage-demo-1", "value": 42}]})
        return _routes(request)

    adapter, seen = _adapter(handler)
    history = adapter.edme_history()

    assert history == [{"obj_id": "storage-demo-1", "value": 42}]
    body = json.loads([r for r in seen if r.url.path.endswith("/action/query")][-1].content)
    assert body["obj_ids"] == ["storage-demo-1"]
    assert body["obj_type_id"] == 1001


# --- Repository routing ------------------------------------------------------

def _edme_repo(tmp_path) -> ConfiguredRepository:
    path = tmp_path / "platforms.json"
    path.write_text(
        json.dumps({"edme": {"ip": "edme.local", "session": "s"}, "mcp": {"agent_mode": "mcp"}}),
        encoding="utf-8",
    )
    repository = ConfiguredRepository(load_runtime_config(path))
    adapter, _ = _adapter(_routes)
    repository.edme = adapter
    repository.storage = adapter
    return repository


def test_metrics_refuse_to_serve_mock_data_when_only_edme_is_configured(tmp_path):
    repository = _edme_repo(tmp_path)

    # Metrics come from FusionCompute. With eDME live and FusionCompute absent,
    # the mock repository is still wired in as `compute`; returning its numbers
    # would present demo data as live platform state.
    with pytest.raises(ValueError, match="FusionCompute"):
        repository.vm_metrics("urn:vms:1")
    with pytest.raises(ValueError, match="FusionCompute"):
        repository.cluster_daily_growth_gb("urn:sites:DEMO:clusters:11")


def test_overview_counts_come_from_the_platform_not_from_a_short_listing(tmp_path):
    repository = _edme_repo(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            # Only one VM comes back although the clusters report 22 + 36.
            return httpx.Response(200, json={"vms": [_vm("urn:vms:1", "vm-a", "c")]})
        return _routes(request)

    repository.edme, _ = _adapter(handler)
    overview = repository.overview()

    # len(vms) would say 1. The clusters' own vm_num is the platform's total and
    # survives a truncated listing.
    assert overview["vm_count"] == 58
    assert overview["host_count"] == 6
    assert overview["cluster_count"] == 2


def test_mock_mode_keeps_counting_what_it_actually_has(tmp_path):
    path = tmp_path / "platforms.json"
    path.write_text(json.dumps({"mcp": {"agent_mode": "local"}}), encoding="utf-8")
    repository = ConfiguredRepository(load_runtime_config(path))

    overview = repository.overview()

    # Mock clusters advertise 38 + 24 VMs while the fixture holds far fewer; the
    # platform-reported total is an eDME workaround and must not leak here.
    assert overview["vm_count"] == len(repository.vms())


# --- Fan-out failure modes ---------------------------------------------------
# Querying per cluster multiplies the ways a listing can fail, so each failure
# mode needs its own answer.

def test_one_failing_cluster_does_not_cost_the_whole_inventory(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            body = json.loads(request.content)
            if body.get("cluster_id", "").endswith(":11"):
                return httpx.Response(500, json={"error": "internal"})
            return httpx.Response(200, json={"vms": [_vm("urn:vms:2", "vm-b", "c")]})
        return _routes(request)

    adapter, _ = _adapter(handler)
    with caplog.at_level(logging.WARNING, logger="backend.adapters.edme"):
        vms = adapter.virtual_vms()

    # Losing every VM because one cluster is unhealthy is worse than reporting
    # the rest and saying so.
    assert [item["id"] for item in vms] == ["urn:vms:2"]
    assert any("failed for cluster" in record.message for record in caplog.records)


def test_a_total_query_failure_raises_instead_of_reporting_an_empty_site():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            return httpx.Response(503, json={"error": "unavailable"})
        return _routes(request)

    adapter, _ = _adapter(handler)
    # An empty list here would read as "this site has no VMs" — a confident
    # falsehood. The error has to reach the caller.
    with pytest.raises(httpx.HTTPStatusError):
        adapter.virtual_vms()


def test_a_vm_without_an_id_is_still_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            body = json.loads(request.content)
            if body.get("cluster_id", "").endswith(":11"):
                anonymous = _vm("", "vm-no-id", "c")
                return httpx.Response(200, json={"vms": [anonymous]})
            return httpx.Response(200, json={"vms": []})
        return _routes(request)

    adapter, _ = _adapter(handler)
    vms = adapter.virtual_vms()

    # It cannot be deduplicated, but dropping it would quietly shrink the count.
    assert [item["name"] for item in vms] == ["vm-no-id"]


def test_a_site_with_no_clusters_falls_back_to_a_single_query():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/clusters/query":
            return httpx.Response(200, json={"clusters": []})
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            return httpx.Response(200, json={"vms": [_vm("urn:vms:1", "vm-a", "c")]})
        return _routes(request)

    adapter, seen = _adapter(handler)
    vms = adapter.virtual_vms()

    assert len(vms) == 1
    queries = [r for r in seen if r.url.path == "/rest/vmmgmt/v1/vms/query"]
    assert len(queries) == 1 and json.loads(queries[0].content) == {}


# --- Repository routing: remaining branches ---------------------------------

def test_the_aggregate_metrics_endpoint_is_guarded_too(tmp_path):
    repository = _edme_repo(tmp_path)
    # vm_metrics and cluster_daily_growth_gb are covered above; metrics() shares
    # the same guard and must not be the one gap that leaks mock data.
    with pytest.raises(ValueError, match="FusionCompute"):
        repository.metrics()


def test_overview_falls_back_to_listing_length_when_the_platform_reports_nothing(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/vmmgmt/v1/clusters/query":
            stripped = [{**item, "vm_num": 0, "host_num": 0} for item in CLUSTERS["clusters"]]
            return httpx.Response(200, json={"clusters": stripped})
        if request.url.path == "/rest/vmmgmt/v1/vms/query":
            return httpx.Response(200, json={"vms": [_vm("urn:vms:1", "vm-a", "c")]})
        return _routes(request)

    repository = _edme_repo(tmp_path)
    repository.edme, _ = _adapter(handler)
    overview = repository.overview()

    # max() must not turn an absent platform total into a zero count.
    assert overview["vm_count"] == 1
    assert overview["host_count"] == 1


def test_overview_does_not_pay_for_the_cluster_list_twice(tmp_path):
    """overview() needs clusters, and so does the VM fan-out.

    Fetching it once and handing it down turns 2 + N requests into 1 + N. On a
    site with many clusters the duplicate is not free, and it is invisible
    unless asserted.
    """
    repository = _edme_repo(tmp_path)
    adapter, seen = _adapter(_empty_vms)
    repository.edme = adapter
    repository.storage = adapter

    repository.overview()

    cluster_queries = [r for r in seen if r.url.path == "/rest/vmmgmt/v1/clusters/query"]
    assert len(cluster_queries) == 1
