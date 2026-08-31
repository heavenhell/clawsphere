import json

from backend.agent.copilot import HISTORICAL_TOOLS, verify_resource_claims
from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import TOOL_REGISTRY, call_tool
from backend.memory.database import MemoryDatabase
from backend.memory.long_term import LongTermMemory, memory_root


def _store(tmp_path) -> LongTermMemory:
    return LongTermMemory(
        root=tmp_path / "memory",
        database=MemoryDatabase(tmp_path / "index.db"),
    )


def _remember(store, **overrides):
    payload = {
        "tenant_id": "t-1",
        "user_id": "u-1",
        "conversation_id": "c-1",
        "fact_type": "incident",
        "description": "vm-1001 因同宿主机争用导致 CPU Ready 偏高",
        "body": "判定为同宿主机批处理 VM 争用，建议迁移。",
        "resource_ids": ["vm-1001", "host-001"],
        "tools_used": ["get_vm_metrics"],
    }
    payload.update(overrides)
    return store.remember(**payload)


def test_fact_round_trips_through_the_markdown_file(tmp_path):
    store = _store(tmp_path)
    written = _remember(store)

    reloaded = store.load("t-1", "u-1", written.name)

    # The file is the source of truth, so it must carry everything the index
    # does — otherwise a rebuild would silently lose fields.
    assert reloaded.description == written.description
    assert reloaded.resource_ids == ("host-001", "vm-1001")
    assert reloaded.tools_used == ("get_vm_metrics",)
    assert reloaded.fact_type == "incident"
    assert "同宿主机批处理 VM 争用" in reloaded.body


def test_same_arc_updates_one_fact_instead_of_fragmenting(tmp_path):
    store = _store(tmp_path)
    first = _remember(store, description="排查中：CPU Ready 偏高", body="正在确认宿主机负载。")
    second = _remember(store, description="已验证：迁移后恢复", body="迁移批处理 VM 后 CPU Ready 降至 2%。")

    # Five turns of one investigation must not become five useless records; the
    # valuable memory is the closed loop, which only exists at the end.
    assert first.name == second.name
    assert len(list((store.owner_dir("t-1", "u-1") / "facts").glob("*.md"))) == 1
    assert "降至 2%" in store.load("t-1", "u-1", first.name).body


def test_different_resources_produce_separate_facts(tmp_path):
    store = _store(tmp_path)
    first = _remember(store, resource_ids=["vm-1001"])
    second = _remember(store, resource_ids=["vm-1042"])
    assert first.name != second.name


def test_recall_is_scoped_to_the_owner(tmp_path):
    store = _store(tmp_path)
    _remember(store, tenant_id="t-1", user_id="u-1")

    assert store.search(tenant_id="t-1", user_id="u-1", resource_id="vm-1001")
    # Operational history leaking across users or tenants is a privilege
    # escalation, not a convenience.
    assert store.search(tenant_id="t-1", user_id="u-2", resource_id="vm-1001") == []
    assert store.search(tenant_id="t-2", user_id="u-1", resource_id="vm-1001") == []


def test_recall_by_resource_id_is_exact(tmp_path):
    store = _store(tmp_path)
    _remember(store, resource_ids=["vm-1001"], description="vm-1001 的结论")
    _remember(store, resource_ids=["vm-1042"], description="vm-1042 的结论")

    found = store.search(tenant_id="t-1", user_id="u-1", resource_id="VM-1001")

    assert [fact.description for fact in found] == ["vm-1001 的结论"]


def test_keyword_recall_drops_unrelated_facts_instead_of_padding(tmp_path):
    store = _store(tmp_path)
    _remember(store, resource_ids=["vm-1001"], description="CPU 争用导致虚拟机变慢", body="迁移后恢复")
    _remember(store, resource_ids=["ds-002"], description="数据存储容量不足", body="清理快照释放空间")

    found = store.search(tenant_id="t-1", user_id="u-1", keywords="CPU 争用", limit=3)

    # Returning an unrelated fact as "history" is worse than returning nothing.
    assert len(found) == 1
    assert "CPU 争用" in found[0].description


def test_recall_payload_carries_age_so_history_cannot_read_as_current_state(tmp_path):
    store = _store(tmp_path)
    fact = _remember(store, observed_at="2026-01-01T00:00:00+00:00")
    payload = fact.recall_payload()
    assert payload["observed_at"] == "2026-01-01T00:00:00+00:00"
    assert payload["age_days"] > 0


def test_index_rebuilds_from_the_files_alone(tmp_path):
    store = _store(tmp_path)
    _remember(store, resource_ids=["vm-1001"])
    _remember(store, resource_ids=["vm-1042"], conversation_id="c-2")
    store.database.clear_long_term_facts("t-1", "u-1")
    assert store.search(tenant_id="t-1", user_id="u-1", resource_id="vm-1001") == []

    restored = store.rebuild_index("t-1", "u-1")

    assert restored == 2
    assert store.search(tenant_id="t-1", user_id="u-1", resource_id="vm-1001")


def test_path_components_cannot_escape_the_memory_root(tmp_path):
    store = _store(tmp_path)
    path = store.fact_path("../../etc", "..\\..\\root", "../../../evil")
    assert store.root.resolve() in path.resolve().parents
    assert ".." not in path.parts


def test_unknown_fact_type_is_rejected(tmp_path):
    store = _store(tmp_path)
    assert _remember(store, fact_type="not-a-type") is None
    assert _remember(store, description="  ") is None


def test_history_tool_is_registered_and_scoped_by_caller():
    spec = TOOL_REGISTRY["search_session_history"]
    assert spec.needs_caller is True
    assert spec.risk == "none"
    # The caller identity must not be a model-supplied parameter.
    assert "_caller" not in spec.input_model.model_json_schema()["properties"]


def test_history_tool_call_returns_historical_marker():
    response = call_tool(ToolRequest(
        tool_name="search_session_history",
        params={"resource_id": "vm-1001"},
        caller_user_id="history-tool-user",
        caller_roles=["readonly"],
        tenant_id="history-tool-tenant",
        task_id="history-tool-task",
    ))
    assert response.success is True
    assert response.data["is_historical"] is True
    assert "不代表资源的当前状态" in response.data["notice"]


def test_historical_results_cannot_ground_a_current_state_claim():
    history_result = [{
        "tool_name": "search_session_history",
        "success": True,
        "data": {"is_historical": True, "facts": [{"resource_ids": ["vm-1042"]}]},
    }]

    grounded, reason = verify_resource_claims(
        "vm-1042 当前 CPU 为 95%。",
        [{"id": "vm-1042", "kind": "state_assertion", "from_tool": "search_session_history"}],
        history_result,
    )
    assert grounded is False
    assert "vm-1042" in reason

    # Declaring it as a past example is the supported way to reference history.
    grounded, _ = verify_resource_claims(
        "vm-1042 在约 12 天前的记录里出现过类似问题。",
        [{"id": "vm-1042", "kind": "example"}],
        history_result,
    )
    assert grounded is True


def test_live_results_still_ground_claims_alongside_history():
    grounded, reason = verify_resource_claims(
        "vm-1001 当前 CPU 使用率 86%。",
        [{"id": "vm-1001", "kind": "state_assertion", "from_tool": "get_vm_metrics"}],
        [
            {"tool_name": "get_vm_metrics", "success": True, "data": {"vm": {"id": "vm-1001"}}},
            {"tool_name": "search_session_history", "success": True, "data": {"facts": []}},
        ],
    )
    assert grounded is True, reason
    assert "search_session_history" in HISTORICAL_TOOLS


def test_hand_edited_file_with_yaml_style_list_is_read_correctly(tmp_path):
    store = _store(tmp_path)
    fact = _remember(store)
    path = store.fact_path("t-1", "u-1", fact.name)
    # An operator correcting the file by hand writes YAML, not JSON. Naively
    # iterating that string would yield one "resource id" per character.
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace('resource_ids: ["host-001", "vm-1001"]', "resource_ids: [vm-1001, host-001]")
        .replace('type: "incident"', "type: incident"),
        encoding="utf-8",
    )

    reloaded = store.load("t-1", "u-1", fact.name)

    assert reloaded.fact_type == "incident"
    assert set(reloaded.resource_ids) == {"vm-1001", "host-001"}
    assert store.rebuild_index("t-1", "u-1") == 1
    assert store.search(tenant_id="t-1", user_id="u-1", resource_id="vm-1001")


def test_naive_timestamp_does_not_break_recall(tmp_path):
    store = _store(tmp_path)
    # Hand-edited timestamps often lose the timezone; subtracting a naive value
    # from an aware `now` would raise and take the whole recall down.
    fact = _remember(store, observed_at="2026-01-01T00:00:00")
    payload = fact.recall_payload()
    assert payload["age_days"] is not None and payload["age_days"] > 0


def test_recall_stays_inside_the_cross_session_token_budget(tmp_path):
    from backend.memory.context_manager import CROSS_SESSION_TOKEN_BUDGET, estimate_tokens
    from backend.memory.long_term import fit_recall_budget

    store = _store(tmp_path)
    for index in range(5):
        _remember(
            store,
            conversation_id=f"c-{index}",
            resource_ids=[f"vm-{1000 + index}"],
            description=f"第{index}条结论",
            body="容量告警排查记录与处置结论。" * 100,
        )
    facts = store.search(tenant_id="t-1", user_id="u-1", limit=5)
    assert len(facts) == 5

    payloads = fit_recall_budget(facts)

    spent = estimate_tokens([
        {"role": "system", "content": json.dumps(payloads, ensure_ascii=False)}
    ])
    assert spent <= CROSS_SESSION_TOKEN_BUDGET
    # Metadata carries the staleness warning, so a fact is trimmed before it is
    # dropped — the caller must still see that history exists.
    assert payloads and payloads[0]["observed_at"]


def test_memory_root_is_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("DCS_MEMORY_DIR", str(tmp_path / "custom"))
    assert memory_root() == tmp_path / "custom"


def test_index_file_lists_every_fact_for_human_review(tmp_path):
    store = _store(tmp_path)
    _remember(store, resource_ids=["vm-1001"], description="第一条结论")
    _remember(store, resource_ids=["vm-1042"], conversation_id="c-2", description="第二条结论")

    index = (store.owner_dir("t-1", "u-1") / "INDEX.md").read_text(encoding="utf-8")

    assert "第一条结论" in index and "第二条结论" in index
    assert "vm-1001" in index and "vm-1042" in index


def test_stored_frontmatter_is_valid_and_machine_readable(tmp_path):
    store = _store(tmp_path)
    fact = _remember(store)
    raw = store.fact_path("t-1", "u-1", fact.name).read_text(encoding="utf-8")
    header = raw.split("---")[1]
    values = dict(
        (line.split(":", 1)[0].strip(), json.loads(line.split(":", 1)[1].strip()))
        for line in header.strip().splitlines()
    )
    assert values["type"] == "incident"
    assert values["resource_ids"] == ["host-001", "vm-1001"]
