# LLM1 增加工具可见性 + 无技能兜底 —— 变更PR实施计划

> **状态：已实施。** PR1（代码）、PR2（测试）、PR3（文档）均已完成，实施结果并入 [four-stage-agent-design.md](./four-stage-agent-design.md)。本文档保留作为这次修订的设计记录。
>
> 背景：当前 `skill_router`（LLM1）只能看到 Skill 一句话目录，看不到任何工具信息（见对话记录，已用代码核实：`_skill_router_payload` 只有 `message/conversation_summary/recent_messages/skill_catalog` 四个字段，`call_deepseek_json` 是纯 JSON 模式，请求体里没有 `tools` 字段）。这导致一个真实的架构约束：**能不能调用工具，完全由"有没有命中某个 Skill"决定**——`route_after_skill_router`（`copilot.py:451`）只有 `decision=="use_skill"` 才会往下走，没有"跳过 Skill 直接查工具"的路径。当前 4 个 Skill 的一句话摘要都不明确覆盖写操作（重启/扩容/HA策略）和 eDME，如果 LLM1 判断不出该用哪个 Skill，整轮请求就再也碰不到任何工具。
>
> 本次变更：给 LLM1 加一层工具目录可见性（分层——只给 tier1 的名字+描述，不给完整 参数 schema，延续现有"每层只看最小必要信息"的原则），并且在 `skill_router` 的决策契约里加一个新分支，让"没有 Skill 匹配，但能看出需要某类工具数据"这种情况也能走到工具检索，不再必须先命中 Skill。
>
> 这是在已实现的四阶段架构（[four-stage-agent-design.md](./four-stage-agent-design.md)）之上做的一次**结构性修订**，不是重新设计——护栏、offered-set、RBAC、审批、authorization_epoch 这些安全层完全不动，改动只发生在 LLM1 的输入和 `skill_router` 之后多一条路由分支。

## 一、设计要点（先对齐，再动代码）

1. **LLM1 新增第四种决策**：`decision` 枚举从 `use_skill | direct_answer | clarification_required` 扩展为 `use_skill | use_tool_directly | direct_answer | clarification_required`。
   - `use_tool_directly`：没有 Skill 能覆盖这个请求，但工具目录里能看出需要哪类数据，直接跳过 `skill_loader`，进入 `tool_search_planner`（LLM2）。
   - 其余三种语义不变。
2. **LLM1 的工具可见性只到 tier1**：新增 `tool_catalog_tier1(roles)`，格式类似现有 `skill_catalog_tier1(roles)`——按角色过滤后的 `工具名 (category): 一句话描述` 列表，**不带参数 schema**。参数 schema 仍然只在 LLM3 阶段、经过 `tool_catalog_search` 的 RBAC+BM25 过滤之后才出现。这条线不能松：一旦 LLM1 也能看到完整 schema，就退回到"全量工具糊给模型"的老问题，四阶段拆分的意义就没了。
3. **不新增安全层，也不绕开任何现有安全层**：`use_tool_directly` 路径最终还是要经过 `tool_search_planner → tool_catalog_search`（RBAC 过滤 + BM25）→ `tool_call_planner` → `guardrail`（RBAC + offered-set）→ `tool_executor`（authorization_epoch + 执行前复检），跟现有 `use_skill` 路径共用同一套后半程，一个安全检查点都不少。这条路径本质上是"跳过 Skill 这一步"，不是"跳过安全检查"。
4. **不新增 CopilotState 字段**：`decision` 存在既有的 `skill_decision` 字段里，路由函数直接读 `skill_decision.decision`；`loaded_skills` 在这条路径下保持初始值 `[]`，`_loaded_skills_text(state)` 已经能正确处理空技能上下文（返回空字符串）——这个分支复用的是"Skill 选中但加载失败"那个已有的容错逻辑，只是把它从一个意外的边缘情况，变成一个有意为之的正式路径。

## 二、PR1 —— 工具 tier1 目录 + Prompt/契约扩展 + 路由

### `backend/mcp/tools.py`

新增（紧挨着 `TOOL_REGISTRY`，跟 `skill_catalog_tier1` 在 `loader.py` 里的位置对应）：

```python
def tool_catalog_tier1(roles: list[str]) -> str:
    return "\n".join(
        f"- {spec.name} ({spec.category}): {spec.description}"
        for spec in TOOL_REGISTRY.values()
        if any(role in spec.auth_roles for role in roles)
    )
```

直接复用已有的 `spec.category`（PR1 里给全部 18 个工具打的标签）和 `spec.auth_roles`，不需要新字段。

### `backend/agent/copilot.py`

- `_skill_router_payload`（`copilot.py:230`）新增一个字段：
  ```python
  "tool_catalog": tool_catalog_tier1(state["user_roles"]),
  ```
  需要 `from backend.mcp.tools import tool_catalog_tier1`（新增导入）。
- `SKILL_ROUTER_PROMPT`（`copilot.py:110`）：
  - 输出契约的 `decision` 枚举加上 `use_tool_directly`。
  - 补一条规则：*"如果没有任何 Skill 覆盖这个请求，但下面的工具目录里能看出需要哪类数据，输出 use_tool_directly，不要因为没有对应 Skill 就放弃或要求澄清"*。
  - Prompt 正文里加入 `{tool_catalog_tier1(...)}` 对应的占位说明（实际内容通过 payload 传，不是塞进静态 Prompt 常量——这里只需要更新规则文字，工具目录本身走 `_skill_router_payload`，跟 Skill 目录当前的处理方式一致）。
- `TOOL_SEARCH_PROMPT`（`copilot.py:130`）：开头"你已经拿到下面这个 Skill 的完整内容"这句话现在不总是成立（`use_tool_directly` 路径下没有 Skill）。改成："你可能已经拿到一个 Skill 的完整内容；如果没有，说明这个请求不对应任何已知 Skill，你需要直接基于用户消息和历史判断需要什么数据。"
- `skill_router`（`copilot.py:403`）：`choice` 的判断链里加一支：
  ```python
  if choice == "use_tool_directly":
      return {**base, "plan_source": "skill_router_use_tool_directly", "intent": "tool_execution"}
  ```
  放在 `use_skill` 判断之后、`clarification_required` 判断之前，顺序不影响正确性，只是保持和现有分支風格一致。
- `route_after_skill_router`（`copilot.py:451`）：返回类型从 `Literal["skill_loader", "llm_responder"]` 扩成 `Literal["skill_loader", "tool_search_planner", "llm_responder"]`，新增分支：
  ```python
  if decision.get("decision") == "use_tool_directly":
      return "tool_search_planner"
  ```
- `build_graph()`（`copilot.py:899`）：`add_conditional_edges("skill_router", route_after_skill_router)` 不需要改调用方式（LangGraph 会从函数的返回值集合里自动识别所有可能的目标节点，`tool_search_planner` 已经是图里注册过的节点），只要路由函数按上面改好即可。

**顺序要求**：这是唯一一个 PR，因为改动面本身不大（一个新函数 + 一个 Prompt 规则 + 两个分支判断 + 一条路由），不需要像四阶段大改造那样拆分批次。

**验证**：
- 手动 smoke test：`role=["readonly"]`，构造一个 mock 让 `_call_skill_router` 返回 `decision="use_tool_directly"`，确认 `route_after_skill_router` 输出 `"tool_search_planner"`，并且 `run_copilot()` 全链路跑下来能到 `tool_catalog_search`/`tool_call_planner`。
- 现有 96 个测试应该全部不受影响（新增分支不改变任何已有分支的行为）。

## 三、PR2 —— 测试覆盖

新增到 `tests/test_memory.py`（工具目录相关）和 `tests/test_hitl.py`（路由行为相关）：

1. `test_tool_catalog_tier1_excludes_unauthorized_tools`：readonly 角色的输出里不含 `scale_cluster`/`restart_vm`/`modify_ha_policy`，admin 角色的输出包含全部 18 个。
2. `test_use_tool_directly_skips_skill_loader_but_reaches_tool_search`：仿照已有的 `test_llm1_then_llm2_then_llm3_call_order_on_the_full_tool_path`，mock `_call_skill_router` 返回 `use_tool_directly`，spy `_call_tool_search_planner`/`_call_tool_call_planner`，断言 `skill_loader` 没被调用过（可以直接断言 `result["selected_skill_ids"] == []` 且 `result["loaded_skills"] == []`，不需要额外 spy）、且工具检索/调用两阶段正常跑完。
3. `test_use_tool_directly_still_enforces_full_safety_spine`：mock 到 `tool_call_planner` 提议一个未授权工具（复用 `test_tool_call_outside_offered_candidates_is_rejected_by_guardrail` 的思路），确认 `use_tool_directly` 路径下 offered-set/RBAC 检查照样生效——这是证明"跳过 Skill 不等于跳过安全检查"的关键测试。
4. 复查现有 `test_direct_answer_short_circuit_never_calls_tool_search_or_tool_call_planner`：这个测试 mock 的是 `decision="direct_answer"`，新分支不影响它，应该原样通过，不需要改。

## 四、PR3 —— 文档更新

- `docs/four-stage-agent-design.md`：
  - 时序图加一条新路径：`skill_router` 判定 `use_tool_directly` 时，跳过 `skill_loader`，直接进 `tool_search_planner`。
  - 第五节"LLM1 输入契约"更新：新增工具 tier1 目录输入，新增 `use_tool_directly` 决策值。
  - 第八节"已知限制"里关于"Skill 目录覆盖面不够会导致部分请求碰不到工具"这条（如果记录过的话）标记为已解决，改成指向这次变更。
- 本文档（`skill-router-tool-fallback-plan.md`）执行完之后，在文件顶部加一行"已实施"状态说明，或者在 `four-stage-agent-design.md` 里加一句引用，保持"提案/计划/设计文档"三层结构的一致性。

## 五、不在这次变更范围内

- 不改 `tool_search_planner`/`tool_catalog_search`/`tool_call_planner`/`guardrail`/`hitl_interrupt`/`tool_executor` 的任何逻辑——它们对"这次是从 `skill_loader` 来的还是从 `skill_router` 直接来的"没有感知，也不需要有。
- 不给 LLM1 完整工具参数 schema——只给 tier1 名字+描述，这是本次变更的一条硬约束，不是留待以后优化的项。
- 不改 `MAX_LLM_CALLS_PER_TURN`/预算逻辑——`use_tool_directly` 路径消耗的 LLM 调用次数和 `use_skill` 路径一样（LLM1 + LLM2 + LLM3 + LLM4，只是少了 `skill_loader` 这一步宿主逻辑，不消耗 LLM 调用）。

## 六、影响文件一览

```text
backend/mcp/tools.py       +tool_catalog_tier1()
backend/agent/copilot.py   _skill_router_payload、SKILL_ROUTER_PROMPT、TOOL_SEARCH_PROMPT、
                            skill_router、route_after_skill_router
tests/test_memory.py       +tool_catalog_tier1 的 RBAC 过滤测试
tests/test_hitl.py         +use_tool_directly 路由测试、+安全护栏仍生效测试
docs/four-stage-agent-design.md   时序图、LLM1契约、已知限制章节更新
```

预计改动量：1 个新函数、2 处 Prompt 文案、2 个分支判断、1 条路由边、3-4 个新测试。比原来四阶段大改造小一个数量级，不需要拆成多批实施。
