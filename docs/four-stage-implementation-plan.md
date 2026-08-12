# 四阶段 Agent 重排 —— 分步实施计划（第二批：进程内实现）

> 承接 [four-stage-agent-architecture-proposal.md](./four-stage-agent-architecture-proposal.md)。本文只覆盖该提案第十二节里的"第二批"：用 `TOOL_REGISTRY` 作为唯一目录源的进程内四阶段实现，不含真正 MCP Client 化（第三批，见文末 PR7 之外的范围说明）。
>
> 按 PR 划分，保证每个 PR 落地后系统仍可运行、测试仍全绿，不是"最后一次性打通"。

## 〇、把提案落到代码前先拍板的几个点

提案里有几处留白，这里给出具体选择，后续 PR 按这个来，不用重新讨论：

1. **LLM 2 的 `ToolSearch` 元工具用 function calling 实现**，复用现有 `call_deepseek_agent_plan(system_prompt, history, tools)`，`tools=[<ToolSearch 的合成 schema>]`。不用 JSON 模式。原因：`call_deepseek_agent_plan` 已经会把 `tool_calls[].function.arguments` 解析成 JSON，让 LLM2"调用" `ToolSearch` 等于直接拿到 `{query, top_k, required_capabilities}`，零新增解析代码。LLM1（Skill Router）和 LLM4（Responder）继续用 `call_deepseek_json`（纯 JSON 模式，无 tools），因为它们是单一固定结构的决策，不是工具调用。
2. **新增 `ToolSearchRequest(ToolParams)`**（`backend/mcp/schemas.py`）：既给合成的 `ToolSearch` 工具提供 JSON Schema，也用来校验/收窄 LLM2 的输出（`top_k` 边界、`query` 长度）——复用现有"Pydantic 模型 → `model_json_schema()` 给模型，`model_validate()` 收模型输出"的既有模式。
3. **三个新的、可被直接 monkeypatch 的薄包装函数**：`copilot.py` 里加 `_call_skill_router`、`_call_tool_search_planner`、`_call_tool_call_planner`，各自一行代理到 `llm.py` 里对应函数，保留现有 `try/except Exception: return None` 的保护写法。这不是可选的代码整洁——**是测试能否继续工作的关键**：LLM2 和 LLM3 最终都会调用同一个 `call_deepseek_agent_plan`，如果不给每个阶段一个独立的模块级名字，测试里 `monkeypatch.setattr(copilot, "call_deepseek_agent_plan", ...)` 就没法区分"工具检索阶段"和"工具调用阶段"（详见第三节测试迁移）。
4. **`tool_catalog_search`（新的、基于 BM25 检索工具的宿主节点）自己做 RBAC 预过滤**，和现有 `_rbac_tool_catalog` 的过滤方式一致。这是提案自己的安全验收标准要求的（"未授权工具既不能被 Tool Search 返回"）。这也意味着：对当前角色完全不可见的工具，被拒绝的时机会比今天更早（提前到 `tool_catalog_search`，而不是等到 `guardrail`）——这是一个刻意的、有意的行为变化（见第三节测试迁移里的说明）。
5. **"offered-set"强制检查（护栏加固）独立成一个 PR（PR4）**，不和图重排的 PR 合并。实现方式是给现有 `validate_tool_calls(...)` 加一个可选的尾部参数 `allowed_tool_names: set[str] | None = None`（默认 `None` = 现状行为，对现有调用方零影响），在同一个逐条校验循环里、**现有 RBAC 检查之后**插入这个检查——这样"角色本来就无权调用"这条错误信息谁先触发不会变，不影响已有测试断言。
6. **`authorization_epoch` 只用在它真正有意义的地方**：HITL 暂停（`interrupt()`）到恢复之间——这段时间里 Skill/工具目录可能被重新部署过。**不**在单次同步 `invoke()` 内部逐节点重新计算（一轮对话内角色/租户已经在 state 里冻结，没有 HITL 就不可能中途漂移）。实现为一个辅助函数 + `tool_executor` 里新增的一处检查，依据 `TOOL_CATALOG_VERSION`（`tools.py` 新常量）和 `skill_catalog_version()`（`loader.py` 新函数）。
7. **`plan_source` 向后兼容**：`"deepseek_agent"` 含义不变（LLM3/`tool_call_planner` 产出了 `tool_calls_proposed`），因为 LLM3 确实还是在调用 `call_deepseek_agent_plan`。新的短路场景用新增值，不覆盖旧值：`"skill_router_direct_answer"`、`"skill_router_clarification"`、`"skill_router_fallback"`、`"tool_search_no_candidates"`。`plan`（`list[str]`，前端渲染成 chips）继续在每个阶段被填充简短原因——**不**被 `route_decisions` 取代，后者是新增的、结构化的，供 Responder/观测/测试使用。`retrieved_docs` 这个前端读的字段（`frontend/src/main.jsx` 的"Skill 命中"面板读 `.id/.title/.score`）不再由任何图节点直接写入；改为 `_format_result` 从新的 `loaded_skills` 状态字段按相同形状合成，前端不用改。`retrieved_history` 改名为内部状态字段 `retrieved_cases`（repo 内确认没有其他读者），并且第一次被真正接入 Responder payload（目前是死代码，处理方式对齐提案第五节）。
8. **`backend/mcp/mcp_server.py` 的 18/16 注册漂移不在这批的关键路径上**：`copilot.py` 直接调 `call_tool()`/`TOOL_REGISTRY`，从不经过 FastMCP server。建议把补齐 2 个缺失注册（`list_clusters`、`get_vm_detail`）做成一个完全独立、可并行的小 PR（PR7），但它不阻塞 PR1–PR6。

---

## 一、PR1 —— 状态与 Schema 基础设施（无行为变化）

**`backend/mcp/tools.py`**
- `ToolSpec` 新增两个字段（放在 `fn` 之后，避开"无默认值参数不能排在有默认值参数后面"的顺序问题）：`category: str`、`tags: list[str]`。默认值由装饰器给，不由 dataclass 给。
- `mcp_tool(...)` 装饰器新增 `category="general"`、`tags=None` 关键字参数。
- 新增模块常量 `TOOL_CATALOG_VERSION = 1`（手动维护，工具定义有实质变化时手动升版，供 `authorization_epoch` 消费）。
- 给全部 18 个 `@mcp_tool(...)` 标注具体 category/tags（例：`list_alarms`→`alert`；`get_vm_metrics`→`performance`；`restart_vm`/`scale_cluster`/`modify_ha_policy`→`admin`；其余按资源域归类到 `resource`/`capacity`）。

**`backend/mcp/schemas.py`**
- 新增 `ToolSearchRequest(ToolParams)`：`query: str = Field(min_length=1, max_length=200)`、`top_k: int = Field(default=5, ge=1, le=5)`、`required_capabilities: list[str] = Field(default_factory=list, max_length=10)`。对应提案第十节第 9 条（限制查询长度/字符集/top_k）。

**`backend/skills/loader.py`**
- 新增和 `retriever.py` 里 `ensure_knowledge_seeded()` 同构的懒加载缓存：`_index_lock`、`_SKILL_INDEX`、`_CATALOG_VERSION`。
- `_load_index()`：懒加载、线程安全，首次调用时从 `load_all_skills()` 构建 `{skill.id: skill}`。
- `reload_skills() -> int`：强制重建索引并升版本号，返回新版本号（batch 2 不需要热加载 HTTP 入口，这个函数存在只是为了让 `authorization_epoch` 有真实可反应的东西，也方便测试模拟目录中途变化）。
- `skill_catalog_version() -> int`。
- `load_skill_by_id(skill_id: str) -> SkillDocument | None`：精确按 ID 查找，替代 `retrieve_skill_detail()` 里 `:tier2`→`:tier3` 字符串替换的脆弱写法。
- `list_skills_for_roles(skills, roles) -> list[SkillDocument]`：纯函数，按 `applicable_roles` 过滤。
- `skill_catalog_tier1(roles) -> str`：`startup_skill_summaries()` 的角色过滤版，供新的 `SKILL_ROUTER_PROMPT` 使用，让未授权 Skill 从源头就不进入 LLM1 的 Prompt。
- `load_skill`、`load_all_skills`、`startup_skill_summaries` 保持不变（`tests/test_memory.py` 里还有测试直接依赖）。
- `backend/skills/__init__.py`：把新增的几个函数加入显式导出列表。

**`backend/agent/copilot.py`** —— 只加 `CopilotState` 字段，先不接线：
```
skill_decision, selected_skill_ids, loaded_skills, skill_catalog_version
tool_search_request, tool_search_candidates, selected_tool_schemas, tool_catalog_version
route_decisions, authorization_epoch
llm_stage_metrics, agent_step_count
retrieved_cases  # 取代 retrieved_history
```
- `retrieved_docs`/`retrieved_history` 作为 **state 字段**移除（后续没有节点再写它们）；对外的 JSON 输出仍然保留 `retrieved_docs`/`retrieved_history` 这两个 key（由 `_format_result` 合成，见 PR5），保证 API 兼容。
- 新增纯函数 `authorization_epoch(roles, tenant_id) -> str`，返回 `f"{tenant_id}:{'|'.join(sorted(roles))}:{skill_catalog_version()}:{TOOL_CATALOG_VERSION}"`。这个 PR 里先不接调用方。
- 新增常量 `MAX_LLM_CALLS_PER_TURN = 8`（先不使用）。

**`backend/agent/llm.py` / `backend/agent/__init__.py`**
- 删除确认无引用的 `call_deepseek_tool_plan`（一个已存在但当前流水线不用的规划函数），避免后面又造一个"第五种规划函数"。

**顺序要求**：`ToolSpec.category/tags` 和 `ToolSearchRequest` 必须先于 PR2/PR3 落地；`loader.py` 的按 ID 查找 API 必须先于 PR3 的 `skill_loader` 节点。`CopilotState` 字段新增和其他改动无依赖，可以最先合并。

**验收**：跑全量现有测试，预期零行为变化、全绿。新增测试：`test_tool_spec_has_category_and_tags_for_all_registered_tools`、`test_load_skill_by_id_returns_known_id_and_none_for_unknown`、`test_list_skills_for_roles_filters_by_applicable_roles`、`test_reload_skills_bumps_skill_catalog_version`。

---

## 二、PR2 —— 工具元数据 BM25 索引 + 未接线的宿主函数

**`backend/memory/retriever.py`**
- 新增 `_tool_corpus()`：直接从 `TOOL_REGISTRY.values()` 构建内存态、不落库的 `{tool_name, category, tags, description, tokens}` 列表——**刻意不走 `knowledge_store`**。原因：`knowledge_store` 的权限模型是三档 `permission` 字符串（`public/internal/confidential`），而工具 RBAC 是精确的角色成员检查（`auth_roles` 列表）。这是两套不同的模型，硬塞进权限档位模型很脆弱（目前只是碰巧对，一旦出现 `auth_roles=["ops"]` 这种非典型组合就会悄悄错）。这里复用的是"现有 BM25 引擎"（`rank_bm25.BM25Okapi` + `tokenize()`），不是"`knowledge_store` 持久层"，属于对提案第四节"可以继续用现有 BM25"的一种落地方式，不算偏离。
- 懒加载缓存（同 `ensure_knowledge_seeded()` 的写法），因为 `TOOL_REGISTRY` 在 import 之后不会再变。
- 新增 `retrieve_tools(query, roles, tenant_id="global", top_k=5) -> list[dict]`：① 先按 `auth_roles` RBAC 过滤（和 `_rbac_tool_catalog` 一致）；② 只对过滤后的子集建 `BM25Okapi`（只有 18 个工具，开销可忽略）；③ 按 `tokenize(query)` 打分；④ 返回 top_k，含 `score`、`retrieval_mode: "bm25"`。
- `retrieve()`（Skill BM25）和 `retrieve_history()`（历史案例 BM25）保持不变——`retrieve()` 在 PR3 之后不再被图调用，但保留是因为 `tests/test_memory.py` 直接调用它。

**`backend/agent/copilot.py`** —— 新增两个普通函数，写好但**先不接入 `build_graph()`**：
- `skill_loader(state)`：读 `state["skill_decision"]["skill_ids"]`，上限 `MAX_SKILLS_PER_TURN = 3`，逐个用 `loader.load_skill_by_id` 解析，剔除不存在的 ID 和当前角色不适用的 ID（二次防御性检查），返回 `loaded_skills`/`selected_skill_ids`/`skill_catalog_version`。全部被剔除也**不短路**——直接把空 `loaded_skills` 传给下一阶段，因为提案没有定义"Skill 加载为空"这个短路分支。
- `tool_catalog_search(state)`：读 `state["tool_search_request"]`，调 `retriever.retrieve_tools(...)`，用 `_rbac_tool_catalog` 新增的可选 `names: set[str] | None = None` 过滤参数构建 `selected_tool_schemas`（完整 function-calling schema，形状和现在 `_rbac_tool_catalog` 输出一致，只是候选集变小）。

两者都直接单测（手工构造 `state` 字典调用），不碰图——这就是 PR2 能独立于 PR3 之外先合并的原因。

**顺序要求**：依赖 PR1。和 PR3 的 Prompt/图改动相互独立，可单独评审合并。

**验收**：现有测试全绿（新函数还没接进图，属死代码）。新增测试：`test_retrieve_tools_excludes_rbac_unauthorized_tools_before_ranking`、`test_retrieve_tools_finds_exact_name_match_as_top_hit`、`test_skill_loader_drops_unknown_and_non_applicable_skill_ids`、`test_tool_catalog_search_returns_full_schemas_for_candidates_only`。

---

## 三、PR3 —— 图重排：四阶段流水线 + Prompt + 测试迁移（主变更）

这是最大的一个 PR，必须在合并时保持测试全绿——现有约 8 个 HITL/安全测试要**在这个 PR 里**一起迁移，不能拖到后面。

**`backend/agent/copilot.py`**

新 Prompt（取代 `ROUTER_PROMPT`；`RESPONDER_PROMPT` 的 payload 结构变化在 PR5，这里不动）：
- `SKILL_ROUTER_PROMPT`：身份 + `decision ∈ {use_skill, direct_answer, clarification_required}` 的输出契约（`decision, skill_ids, arguments, confidence, missing_context, reason_summary`），Skill 目录按调用时的角色现算（`loader.skill_catalog_tier1(roles)`），不再用模块级常量 `startup_skill_summaries()`——让目录随角色变化，顺带解决 `architecture-issues-analysis.md` 里"目录永不变化"在 RBAC 维度上的问题。
- `TOOL_SEARCH_PROMPT`："给定下面完整加载的 Skill 内容，判断是否需要真实工具数据，需要就调用 `ToolSearch`" + 一段静态的工具分类说明（只讲 5 个 category，不列具体工具名和 schema）。
- `TOOL_CALL_PROMPT`（`ROUTER_PROMPT` 改名而来，规则收窄为"给定 Skill + 这些候选工具 schema，决定真实 tool_calls"，保留原来关于写操作/资源 ID/不能瞎猜参数的规则）。
- 新增 `IDENTITY_BLOCK`，作为公共前缀加到四个阶段 Prompt 开头——顺带解决 `architecture-issues-analysis.md` 的 P1（Prompt 前缀缓存/身份重复声明）问题。

新增薄包装函数（原因见第〇节第 3 条）：
```
_call_skill_router(payload: str) -> dict | None
_call_tool_search_planner(history, tools) -> dict | None
_call_tool_call_planner(history, tools) -> dict | None
```
每个都是 `try: return <底层调用>; except Exception: return None`，和现在 `llm_router` 的写法一致。

`_llm_history` 签名变化（向后兼容）：`_llm_history(state, extra_context: str = "")`——非空时在现有 summary/relevant/recent/当前消息之前插一条 `{"role":"system","content": f"已选 Skill 完整内容:\n{extra_context}"}`。`tool_search_planner` 和 `tool_call_planner` 都要用（两者都需要"已选完整 Skill"）。默认空字符串不改变任何现有调用方的输出。

新增 `_skill_router_payload(state) -> str`：JSON payload（风格对齐 `_responder_payload`），含 `message`、`conversation_summary`、`recent_messages`、`skill_catalog = loader.skill_catalog_tier1(state["user_roles"])`。

新图节点（取代 `llm_router`，`memory_retriever` 改名）：
- `history_retriever`（`memory_retriever` 改名）：只调 `retrieve_history()` → `retrieved_cases`，去掉 BM25-Skill 检索（这条通道被"精确加载 Skill"取代）——这正是 `architecture-issues-analysis.md` 问题一描述的"Router 选了但 BM25 没命中"问题的根治方式。
- `skill_router`（LLM1）：调 `_call_skill_router(_skill_router_payload(state))`。`None` → `llm_available=False`（沿用现有模式）。`decision` 非法或 `use_skill` 但 `skill_ids` 为空 → 降级为 `direct_answer` 行为，`plan_source="skill_router_fallback"`（失败关闭，不重试——只有 Responder 重试，上游规划阶段失败就直接走"不使用工具"，不做循环）。
- `route_after_skill_router`：只有 `decision=="use_skill"` 且 `skill_ids` 非空才走 `skill_loader`，否则直接 `llm_responder`。
- `skill_loader`（PR2 已写好）→ 无条件边到 `tool_search_planner`。
- `tool_search_planner`（LLM2）：调 `_call_tool_search_planner(...)`，解析唯一预期的 `ToolSearch` 调用，用 `ToolSearchRequest.model_validate` 校验，存 `tool_search_request`。空 `tool_calls`（模型主动放弃）等同于"无候选"。
- `route_after_tool_search` → `tool_catalog_search` 或 `llm_responder`。
- `tool_catalog_search`（PR2 已写好）→ `route_after_tool_catalog_search`，按 `tool_search_candidates` 是否非空决定走 `tool_call_planner` 还是 `llm_responder`。
- `tool_call_planner`（LLM3，取代 `llm_router` 的选工具职责）：调 `_call_tool_call_planner(...)`，**照搬**现有 `MAX_TOOL_CALLS_PER_TURN` 上限和 `step_budget_hit` 逻辑。`plan_source="deepseek_agent"`（字符串不变，见第〇节第 7 条）。
- `tool_call_planner → guardrail` 无条件边，**`guardrail` 及之后（`hitl_interrupt`、`tool_executor`、两个 `route_after_*`）完全不动**（`guardrail` 节点在 PR4 才加 offered-set 检查）。
- `llm_responder`、`memory_writer`、`error_handler` 这个 PR 里不动（payload 增强是 PR5）。

`build_graph()` 按提案第四节的图重写节点/边。`run_copilot()` 的初始 state 字典补上所有新字段的初始值。

**测试迁移（必须随本 PR 一起提交）**

核心问题：`tests/test_hitl.py`/`tests/test_security_regressions.py` 里的 `_propose()` 及内联 monkeypatch 直接打 `copilot.call_deepseek_agent_plan`（约 8 个测试：`test_readonly_write_request_is_blocked_before_tool_execution`、`test_high_risk_change_pauses_then_resumes_once`、`test_rejected_change_never_executes`、`test_no_proposed_tool_means_no_execution`、`test_approval_uses_highest_proposed_tool_risk`、`test_write_is_revalidated_after_approval_wait`、`test_pending_approval_blocks_later_message_in_same_conversation`、`test_llm_proposed_tools_are_audited_under_the_real_identity`、`test_llm_tool_plan_still_passes_rbac`、`test_step_budget_caps_tool_calls_per_turn`）。PR3 之后这一个补丁点根本到不了工具调用阶段（LLM1 会真的打网络请求，测试环境没配 key 会返回 `None` 并在 `call_deepseek_agent_plan` 之前就短路掉）。

**迁移方式**：把 `_propose(monkeypatch, tool_name, params, reason=...)` 改成同时打 **四个**点（建议留在 `test_hitl.py` 里，`test_security_regressions.py` 里现在内联重复的写法改成导入复用，或者提到 `conftest.py` 做成 fixture）：
1. `copilot._call_skill_router` → 固定返回 `{"decision":"use_skill","skill_ids":["resource_query"],"arguments":{},"confidence":0.9,"missing_context":[],"reason_summary":"test stub"}`（用任意一个真实存在、总能加载成功的 skill id 即可，Skill 内容不影响 LLM3 能调哪些工具，只是提供背景信息）。
2. `copilot._call_tool_search_planner` → `{"tool_calls":[{"tool_name":"ToolSearch","params":{"query": tool_name, "top_k":5, "required_capabilities":[]}}],"reason":"test stub"}`。
3. `copilot.tool_catalog_search`（宿主函数，不是 LLM 调用）——直接 monkeypatch 成返回 `{"tool_search_candidates":[...], "selected_tool_schemas":[<tool_name 对应的完整 schema>]}`，**绕过真实的 RBAC 预过滤**。这一步只对两个测试是必须的：`test_readonly_write_request_is_blocked_before_tool_execution`、`test_llm_tool_plan_still_passes_rbac`——这两个测试的意义就是证明"哪怕上游貌似提供了这个工具，`guardrail` 的 RBAC 复检依然独立拦住它"，这和现在 `_propose()` 绕过 `_rbac_tool_catalog` 真实过滤效果的做法是同一个道理。对目标工具本来就在测试角色 RBAC 范围内的其余约 6 个测试，这一步可以跳过，让真实的、RBAC 过滤过的 `tool_catalog_search` 跑起来，多一层集成测试覆盖——具体哪些测试跳过、哪些不跳过，需要在迁移时逐个标注。
4. `copilot._call_tool_call_planner` → 和今天 `_propose()` 一样的固定返回 `{"tool_calls":[{"tool_name":tool_name,"params":params}],"reason":reason}`。

这样改完，**现有约 8 个测试里的全部断言都不用变**，包括两条关键断言：
- `test_llm_tool_plan_still_passes_rbac`：`result["plan_source"] == "deepseek_agent"` 仍然成立（LLM3 确实跑了并产出了 `tool_calls_proposed`），`"无权调用工具" in result["answer"]` 仍然成立（`guardrail` 现有 RBAC 检查在逐条校验循环里排第一，PR4 新增的 offered-set 检查不影响它先触发）。
- `test_step_budget_caps_tool_calls_per_turn`：50 次重复 `list_alarms` 调用，`list_alarms` 本身 RBAC 开放，第 3 步（`tool_catalog_search` 绕过）非必须，但为了确定性建议统一用四点 helper，不依赖真实 BM25 排序结果。
- `test_no_proposed_tool_means_no_execution`：**建议简化**，只 mock `_call_skill_router` 返回 `{"decision":"direct_answer",...}`——新的短路路径能达到完全相同的可观察结果（`not result["tool_calls"]`、`not result["approval"]`），mock 更少，而且实际上更贴合重构后的意图（走预期的快速路径，而不是硬塞一个空列表走完三个规划阶段）。

**新增测试**（证明"RBAC 检查提前"是刻意的行为变化，不只是"别弄坏旧测试"）：`test_unauthorized_tool_never_becomes_a_search_candidate`（readonly 角色 + 真实 `tool_catalog_search` + 查询指向 `scale_cluster` → 候选为空 → 在 LLM3 被调用之前就短路，用 spy 监视 `_call_tool_call_planner` 验证）。

**本 PR 顺带的文档清理**：`README.md` 和 `.codebuddy/memory/MEMORY.md` 里"`context_loader → memory_retriever → llm_router → ...`"这句流水线描述已经过时，一并更新，避免误导后续改动者。

**顺序要求**：依赖 PR1+PR2。是体量最大的一个 PR，后续所有节点/边形状都建立在它之上。

**验收**：`pytest tests/ -x` 全绿（含迁移后的测试）。手动冒烟：`run_copilot("你好", ["readonly"])` 应该在 `skill_router` 就短路；`run_copilot("cluster-002 还能撑多久", ["readonly"])` 应该走完三个规划阶段（未配置 LLM key 时按现有方式优雅降级）。

---

## 四、PR4 —— 安全加固：offered-set 强制检查 + `authorization_epoch` 检查

**`backend/guardrails/policy.py`**
- `validate_tool_calls(tool_calls, roles, message="", task_id=None, allowed_tool_names: set[str] | None = None)`——新增尾部可选参数，默认 `None` 保持所有现有调用方行为不变（`test_write_tool_is_blocked_by_tool_metadata_without_write_keywords`、`test_guardrail_validates_resource_and_blast_radius` 两个直接单测都不传这个参数，不受影响）。新检查插在逐条校验循环里**现有 RBAC 检查之后**（确认不影响 `test_llm_tool_plan_still_passes_rbac` 的断言顺序）：`if allowed_tool_names is not None and tool_name not in allowed_tool_names: violations.append(f"该工具未在本轮检索候选中提供：{tool_name}"); continue`。

**`backend/agent/copilot.py`**
- `guardrail(state)`：调用 `validate_tool_calls(...)` 时传入 `allowed_tool_names={schema["function"]["name"] for schema in state.get("selected_tool_schemas", [])}`。**`tool_executor` 里的执行前复检调用刻意不改**——到执行阶段时 `guardrail` 已经把 `tool_calls_proposed` 收窄到"已提供+RBAC 通过"的集合了，复检的职责是"世界是否变了"（资源是否还存在、限流），不是"是否被提供过"。
- `tool_executor(state)`：在现有写操作复检代码块之前新增：若存在写调用，比较 `authorization_epoch(state["user_roles"], state["tenant_id"])` 和 `state.get("authorization_epoch")`，不一致则直接返回"执行前复检失败：技能或工具目录版本已变化，请重新发起请求"，不执行。
- `context_loader(state)`：返回值里加上 `authorization_epoch(state["user_roles"], state["tenant_id"])`（图里最早能拿到角色/租户的地方），在轮次开始时就把 epoch 固定下来，供可能很久之后的 HITL 恢复时比对。

**顺序要求**：依赖 PR3（`selected_tool_schemas`、`authorization_epoch` 字段/辅助函数在 PR1 就有了，这里才真正使用）。

**验收**：现有测试全绿（两个新参数默认不生效）。新增测试：`test_tool_call_outside_offered_candidates_is_rejected_by_guardrail`；`test_authorization_epoch_invalidates_stale_write_after_catalog_change`（模拟 `test_high_risk_change_pauses_then_resumes_once` 的暂停场景，在 `resume_copilot` 之前修改 `TOOL_CATALOG_VERSION` 或调用 `reload_skills()`，断言恢复后不执行、返回"目录版本已变化"提示）。

---

## 五、PR5 —— Responder payload 增强 + 前端/API 兼容

**`backend/agent/copilot.py`**
- `_responder_payload`：新增（走现有 `_cap()`/`RESPONDER_PAYLOAD_CAP` 机制，不重新设计）：`skill_decision`（只留 decision/skill_ids/confidence/reason_summary）、`loaded_skills`（只留 id/version/summary，**不带** tier-3 详细步骤，对齐提案第三节第 6 条"完整 Skill 正文只在确实需要时保留"）、`tool_search_request`、`tool_search_candidates`（只留名字+分数）、`route_decisions`，以及**首次真正启用**的 `retrieved_cases`。
- `RESPONDER_PROMPT`：补充说明可能会看到这些新的辅助上下文字段，但 `answer`/`resource_claims` 输出契约不变，`verify_resource_claims` 不需要改。
- `_format_result`：从 `loaded_skills` 合成对外的 `"retrieved_docs"`（`{id, title, content: summary, score: 1.0, retrieval_mode: "skill_router"}`，保证前端"Skill 命中"面板不用改）；从 `retrieved_cases` 合成对外的 `"retrieved_history"`（key 名保持不变，尽管内部 state 字段改名了）；`"plan"` 继续从各阶段的 reason/reason_summary 拼起来，保证前端 chip 列表不用改；新增 `"route_decisions"`、`"skill_decision"` 两个输出 key（纯增量，不影响 `eval/evaluator.py` 只读的那几个字段）。

**顺序要求**：依赖 PR3。和 PR4 相互独立，可并行开发。

**验收**：现有测试全绿；`eval/evaluator.py` 不用改。手动检查：有 `DEEPSEEK_API_KEY` 时跑 `python -m eval.evaluator`，确认 `report.json` 形状不变。新增测试：`test_responder_payload_includes_skill_and_tool_search_summaries_and_stays_under_cap`。

---

## 六、PR6 —— 可观测性：分阶段指标 + 总调用预算

**`backend/agent/llm.py`**
- 新增 `_last_usage` ContextVar（和现有 `_request_status` 同构），在 `_invoke()` 成功解析响应时从 `body.get("usage")` 写入；新增 `get_last_llm_usage()` 访问器。

**`backend/agent/copilot.py`**
- `skill_router`、`tool_search_planner`、`tool_call_planner` 以及 `llm_responder` 重试循环的每次尝试：计时、`agent_step_count` 自增、往 `llm_stage_metrics` 追加一条 `{stage, latency_ms, success, model, prompt_tokens, completion_tokens, summary}`（拿不到 usage 就填 `None`，不能因此报错）。
- 四个 LLM 调用点前都检查 `agent_step_count >= MAX_LLM_CALLS_PER_TURN`（PR1 里定义为 8），超了就跳过调用直接降级（规划阶段按 `direct_answer` 短路处理，`fallback_reason="llm_call_budget_exhausted"`；Responder 的重试循环直接跳出用现有固定道歉语）。当前正常路径最多 1+1+1+3=6 次，触发不到，这是面向未来的硬预算（对齐提案第十节第 8 条）。

**顺序要求**：依赖 PR3。和 PR4/PR5 相互独立，可并行。

**验收**：现有测试全绿。新增测试：`test_llm_stage_metrics_records_one_entry_per_stage_on_the_tool_execution_path`、`test_llm_call_budget_is_enforced_before_invoking_the_llm`（spy 方式）、`test_direct_answer_short_circuit_never_calls_tool_search_or_tool_call_planner`（用 `pytest.fail` 断言 LLM2/LLM3 不会被调用）、`test_llm1_then_llm2_then_llm3_call_order_on_the_full_tool_path`（记录调用顺序，走真实的、RBAC 开放的查询如 `"list_alarms"`，顺带覆盖 PR2 的宿主函数）。

---

## 七、PR7（可选，完全独立并行）—— MCP Server 注册漂移修复

**`backend/mcp/mcp_server.py`**
- 补上缺失的 `list_clusters`、`get_vm_detail` 两个 `@mcp.tool()` 包装函数，照抄现有其余 16 个的写法。
- **不是四阶段图能否工作的必要条件**（进程内 Agent 从不经过这个 server），建议做只是因为便宜（2 个函数，和其余 16 个一样的模板代码）且消除一处已确认的真实漂移。把 MCP 注册从 `TOOL_REGISTRY` 自动派生（彻底消除手工维护列表）明确留给第三批（真正 MCP Client 化），不在这批范围内。

**顺序要求**：和 PR1–PR6 零依赖，随时可做。

**验收**：现有 MCP 相关测试（`test_mcp_call_injects_configured_identity`、`test_mcp_write_uses_caller_supplied_approved_task_id`）保持绿。可选新增 `test_mcp_server_exposes_every_registered_tool`（断言 MCP server 工具名集合 == `TOOL_REGISTRY.keys()`），作为防止再次漂移的回归护栏。

---

## 依赖关系总览

```text
PR1（Schema/状态基础）──┬──▶ PR2（工具BM25 + 未接线宿主函数）──▶ PR3（图重排 + 测试迁移）─┬─▶ PR4（护栏/epoch加固）
                        │                                                              ├─▶ PR5（Responder payload + 兼容）
                        │                                                              └─▶ PR6（可观测性）
PR7（MCP Server漂移修复）── 与以上全部无依赖，随时可合并
```

PR3 落地后，PR4、PR5、PR6 相互独立，可以并行开发评审。

## 涉及的关键文件

- `backend/agent/copilot.py`（状态、Prompt、图节点/边、payload）
- `backend/agent/llm.py`（新增/删除 LLM 调用辅助函数）
- `backend/skills/loader.py`（按 ID 加载、角色过滤、目录版本）
- `backend/memory/retriever.py`（工具 BM25 索引）
- `backend/mcp/tools.py`（`ToolSpec` 分类字段、目录版本常量）
- `backend/mcp/schemas.py`（`ToolSearchRequest`）
- `backend/guardrails/policy.py`（offered-set 检查）
- `backend/mcp/mcp_server.py`（PR7，可选）
- `tests/test_hitl.py`、`tests/test_security_regressions.py`（测试迁移，PR3 核心工作量之一）
