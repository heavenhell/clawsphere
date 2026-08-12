# ClawSphere 架构优化分析报告

> 涉及三个已知问题及关联排查项，供研发团队决策和实现参考。
>
> 后续的落地方案（四阶段 LLM 调用重排 LangGraph）见 [four-stage-agent-architecture-proposal.md](./four-stage-agent-architecture-proposal.md)，其中也修正了本报告的部分事实性错误（已在下文标注 `[已修正]`）。

---

## 问题一：技能加载不完整 —— 策略层与检索层割裂

### 1.1 现象描述

当前技能知识存在两条独立的传递通道：

- **通道 A（Router Prompt）**：`ROUTER_PROMPT` 在 `copilot.py:82` 通过 `startup_skill_summaries()` 一次性嵌入全部 Skill 的 tier-1（一句话摘要）。LLM Router 能看到完整的菜单。
- **通道 B（BM25 检索）**：`memory_retriever` 在 `copilot.py:178-179` 用当前用户消息做 BM25 分词匹配，只取 `top_k=3` 个 skill 的 tier-2（摘要层），结果注入 `retrieved_docs`，最终进入 `_responder_payload` 给 Responder 用。

两条通道之间**没有联系机制**：Router 看到技能 A 存在并决定"这个场景需要用 A"，但如果 BM25 没命中 A 的 tier-2，Responder 就只能拿到前 3 个命中的别的技能，缺少执行 A 所需的具体摘要。

### 1.2 根因分析

(1) **BM25 分词对中文语义的覆盖率有限**

`tokenize()` 函数（`retriever.py:18-24`）使用 bigram（双字滑动窗口）对中文分词，同时抽取 ASCII token。这种纯统计方法**完全忽略语义**——它只管"这两个字有没有连续出现在文档里"，不管这两个字代表什么含义。

具体而言，如果用户说"vm-1001 CPU 高负载"，其中文部分被切成 bigram：`["负载"]`（"高负载"只有 3 个字，逐 2 字滑动得 "高负" 和 "负载"）。如果技能文档中使用的是"CPU 使用率过高"，bigram 为 `["使用", "用率", "率过", "过高"]`，与查询 token `["负载"]` 没有任何重叠，BM25 得分为 0。

(2) **tier-2 的命中完全依赖词形重叠**

技能文档是否被命中，不取决于该技能是否"相关"，而取决于文档内容中**是否存在和查询中相同的连续双字**。一个命名规范、用词专业的技能文档，反而可能因为用词和运维人员的口语化表达不一致而失分。

(3) **`retrieve_skill_detail()` 未被流水线接入**

`retriever.py:102` 定义了 `retrieve_skill_detail()` 函数，支持通过 `skill_id` 直接按 ID 查找 tier-3 或 tier-2。但整个 `copilot.py` 的 9 个图节点没有任何一个调用它。它是一个**被设计但未被实施的兜底机制**。

(4) **Responder 看不到 Router 的选择**

`_responder_payload`（`copilot.py:356`）只包含 `retrieved_docs`（BM25 命中的 tier-2），不包含 `tool_calls_proposed`（Router 决定要调用的工具）。因此 Router 的决策（"这个场景需要 X 技能"）无法向后传递，Responder 无法得知"Router 认为需要但 BM25 没用命中的那个技能"。

### 1.3 修复建议

**短期方案（低风险，建议优先实施）：**

- 在 `memory_retriever` 节点之后、`llm_router` 节点之前，新增一个 `skill_align` 节点：将 Router 输出的 `tool_calls_proposed` 中的工具名映射回技能 ID，对已输出但在 `retrieved_docs` 中缺失的技能调用 `retrieve_skill_detail()` 补全 tier-2。这是最小侵入的补丁。

**中期方案（推荐）：**

- 将技能检索从纯粹的 BM25 升级为**混合检索**：`score = 0.7 × BM25 + 0.3 × (tool-name → skill 映射权重)`。即如果 Router 决定调用 `get_vm_metrics`，对应的 `diagnose_vm_performance` 技能获得一个固定加分，确保它进入 top_3 或至少作为第 4 个文档追加进去。

**长期方案：**

- 当项目接入 Embedding 服务时，将 BM25 替换为向量语义检索（`retriever.py:27-30` 已有占位的 `_empty_embedding()` 函数），从根源上解决语义覆盖问题。

### 1.4 关联排查项

| 排查点 | 说明 |
|---|---|
| `_responder_payload` 是否传入了 Router 的决策上下文 | 当前 `retrieved_docs` 和 `tool_calls_proposed` 互相不知对方存在。Responder 不知道 Router 选了哪些技能。建议在 payload 中增加 `router_decision` 字段。 |
| `tier=2` 硬编码 | `copilot.py:179` 硬编码 `tier=2`。如果未来技能文档膨胀到需要先查 tier-1 再按需加载 tier-2/tier-3，当前架构需要重构。 |
| 种子案例（alert_case）的 cold start | `retriever.py:49-58` 只预置了 2 个历史案例。随着真实案例积累，BM25 分词不准确的问题会进一步放大。 |
| `replace_all_skills` 未实现 | `loader.py` 的 `DESCRIPTION` 注释提到计划支持热加载/替换，但当前该功能未实现。启动后无法动态新增技能。 |

---

## 问题二：MCP 工具全量加载导致上下文膨胀

### 2.1 现象描述

`_rbac_tool_catalog()`（`copilot.py:114-126`）在每一次 Router 调用时，将 `TOOL_REGISTRY` 中所有符合当前角色权限的工具**全量序列化**为 function calling 的 `tools` 数组，包含每个工具的 `description` 和完整的 `input_model.model_json_schema()`。

> **[已修正]** 当前 `TOOL_REGISTRY`（`tools.py`）实际注册 **18 个工具**（含只读和写操作），而 `mcp_server.py` 手工用 `@mcp.tool()` 暴露的只有 **16 个**（缺 `list_clusters`、`get_vm_detail`），两者已经出现定义漂移，见问题四。以 `ScaleClusterParams` 为例（`schemas.py:87`），其字段是 `cluster_id`（继承自 `ClusterCapacityParams`）、`target_hosts`、`reason`，**不存在**嵌套的 `scaling_group_params`；单个工具的 schema 体量比原估算更小。

**粗略估算**（基于 DeepSeek tokenizer，中英文混排 1 token ≈ 2 字符）：
- 18 个工具 × 平均 80 tokens/tool = **~1,440 tokens**
- 加上 `ROUTER_PROMPT`（~400 tokens）和用户消息（~100 tokens），单次 Router 调用约 **1,900+ tokens**

当前在 128K 窗口下完全可控。但考虑以下增长路径：
- 接入 FusionCompute 全部 40+ 北向接口 → 60 个工具
- 接入 eDME 运维面完整 API → +30 个工具
- 每个工具的 schema 因业务复杂度增加而膨胀
- **最终**：90 个工具 × 150 tokens = **13,500 tokens**，这还没算多轮对话的历史消息。

### 2.2 根因分析

(1) **工具筛选只有 RBAC 维度，缺乏语义维度**

当前 `_rbac_tool_catalog` 的唯一筛选条件是 `user_roles` 是否在 `spec.auth_roles` 中。一个具有 `admin` 角色的用户，无论他问的是"告警列表"还是"春节祝福"，都会被传入全部 20 个工具定义。

(2) **`get_resource_overview` 类聚合工具的隐式深度**

`tools.py:81-91` 的 `get_resource_overview` 返回 7 种资源数据（overview、sites、clusters、hosts、vms、datastores、alarms），等价于一次性拉取 7 个独立接口。Router 一旦调用它，Responder 收到的 `tool_results` 可能轻松过 10,000 字符，这在当前 `_responder_payload` 的截断逻辑（`_cap_text`, 2,000 字符上限）中会被粗暴剪裁，导致数据不完整。

(3) **工具分类缺失，无法做分层加载**

当前 `TOOL_REGISTRY` 中所有工具平铺存储，缺乏分类标签：
- 告警类：`list_alarms`、`get_alarm_detail`、`query_edme_current_alarms`
- 资源管理类：`get_resource_overview`、`list_vms`、`get_vm_detail`
- 性能诊断类：`get_vm_metrics`、`run_capacity_forecast`
- 写操作类：`restart_vm`、`scale_cluster`、`modify_ha_policy`
- 审批管理类：`create_approval_request`

如果引入分类标签，Router 调用前可以根据用户意图（从 prompt 第一句话中的关键词判断）只传入相关分类的工具子集。

(4) **`tool_choice: "auto"` 模式下模型会尝试理解全部工具**

DeepSeek API 的 `tool_choice: "auto"` 策略是让模型自行决策。模型需要遍历所有 tool descriptions 来决定调用与否。工具越多，Router 的决策延迟越高、注意力越分散、误调用概率越大。

### 2.3 修复建议

**短期方案（低成本）：**

- 在 `ToolSpec` 中增加 `category` 字段（如 `"alert"`, `"resource"`, `"performance"`, `"admin"`, `"edme"`），修改 `_rbac_tool_catalog` 接受可选的 `categories` 参数。在 `llm_router` 调用前，先对 `state["message"]` 做一次轻量关键词匹配（30 行正则），确定可能相关的分类，优先传入对应子集。匹配不到时降级为全量。

**中期方案（推荐，需重构）：**

- **两阶段工具加载**：
  1. 第一阶段（快速路由）：只传入工具名称 + description 的简化列表（不含完整 JSON Schema），约 20 token/tool，让 Router 筛选出 1-3 个工具。
  2. 第二阶段（精确调用）：重新调用 Router 或直接按 Router 的 selection 将选中工具的完整 Schema 传给 Responder。
  
- 或者用一个更简单的方法：将 Router 的 `tools` 参数只保留 20 个工具的 `name + description`，去掉 `parameters` 字段（即不传 schema）。DeepSeek 的 function calling 在 `name` 和 `description` 匹配时仍然有效，只在后续需要详细的参数合法性校验才用到 schema——而参数校验在 `guardrail` 节点（`validate_tool_calls`）已经做了。

**长期方案：**

- 将工具定义向量化（用 Embedding 服务），在 Router 调用前先做一次**语义工具过滤**：把用户消息做 embedding，与所有工具的 `description` 做余弦相似度计算，取 top 15 传入。

### 2.4 关联排查项

| 排查点 | 说明 |
|---|---|
| `_responder_payload` 对 tool_results 的截断 | `_cap_text(data_text, 2000)` 在返回数据大时直接硬截断，可能丢失关键字段（如最后一个 resource ID）。建议改为智能截断（保留结构化字段的完整性）。 |
| `_llm_history` 无 token 预算控制 | 上下文窗口管理只在 `context_loader` 节点做了一次，但 `_llm_history` 拼装时不做二次裁剪。如果工具返回的数据 + BM25 文档 + 历史消息之和超出窗口，API 会直接报错或产生静默截断。 |
| `TOOL_REGISTRY` 的 `mcp_tool` 装饰器注册顺序影响加载性能 | 当前 18 个工具在模块 `import` 时即全部执行装饰器并注册。每增加一个工具都需要修改 `tools.py` 的 import 部分（新增 schema 类），耦合度高。 |
| 写操作工具的 `risk` 标记 | `restart_vm` 标记为 `high`，`scale_cluster` 标记为 `high`，但缺少 `medium` 级别的工具示范。如果未来出现"创建测试快照"类中等风险操作，风险分类的粒度需要重新校准。 |

---

## 问题三：系统提示词前缀缓存优化 —— 独立 Prompt 导致 KV Cache 碎片化

### 3.1 现象描述

`copilot.py` 在模块加载时构建了三个 Prompt：

| Prompt | 位置 | 使用者 | API 调用 |
|---|---|---|---|
| `SYSTEM_PROMPT` | `copilot.py:80` | `build_system_prompt(...)` | **未被任何 copilot 节点用于 API 调用**（仅导出给 `app.py` 等外部引用） |
| `ROUTER_PROMPT` | `copilot.py:82-93` | `llm_router` | `call_deepseek_agent_plan(ROUTER_PROMPT, ...)` |
| `RESPONDER_PROMPT` | `copilot.py:95-106` | `llm_responder` | `call_deepseek_json(RESPONDER_PROMPT, ...)` |

每次对话会触发两次 LLM API 调用（Router + Responder），两次调用的 `system` 消息**完全不同**。这使得 DeepSeek API 层面的 **Prefix KV Cache（自动前缀缓存）** 在两个调用之间**零命中**。

DeepSeek 的前缀缓存工作原理：当两次请求的 `messages` 数组开头部分完全一致时，服务端可以复用第一次计算中已缓存的 key-value 张量，跳过该前缀的重计算。当前因为 `ROUTER_PROMPT` ≠ `RESPONDER_PROMPT`，Router 调用完成后其 KV Cache 对 Responder 调用没有任何复用价值。

### 3.2 根因分析

(1) **三个 Prompt 各自从零开始定义身份和规则**

对比三个 Prompt 的开头内容：

```
SYSTEM_PROMPT → "你是「ClawSphere」，产品名称为「DCS Copilot」。
                 你的第一身份是企业 DCS/FusionCompute/eDME 运维 Agent..."

ROUTER_PROMPT → "你是 DCS/FusionCompute/eDME 运维 Copilot 的路由器。
                 根据完整对话判断用户意图..."

RESPONDER_PROMPT → "你是 DCS/FusionCompute/eDME 运维 Copilot,负责生成最终回答。
                    必须严格输出 JSON..."
```

三个 Prompt **各自独立声明身份**（"你是...运维..."），但 `ROUTER_PROMPT` 和 `RESPONDER_PROMPT` 中**没有嵌入 `SYSTEM_PROMPT`**，也互不引用。这意味着：如果未来需要修改 Agent 的身份描述（如改名为"DCS 智能运维助手"），需要同时修改 3 个字符串，容易遗漏。

(2) **`SYSTEM_PROMPT` 被构建但未参与核心流水线**

`copilot.py:80` 的 `SYSTEM_PROMPT = build_system_prompt(...)` 使用了 `startup_skill_summaries()` 的完整输出，理论上包含了最丰富的上下文信息。但在 `copilot.py` 内部，没有任何图节点引用它。它的唯一作用是被 `__init__.py` 导出，供 `app.py` 等**外部调用方**使用。

这导致一个矛盾：`copilot.py` 内部的核心流水线无法享受 `SYSTEM_PROMPT` 的完整上下文（如身份设定中的"回答必须先给结论再给证据"这条规则在 ROUTER_PROMPT 中不存在），而外部调用方通过 `SYSTEM_PROMPT` 做独立对话时又缺少 Router/Responder 的流水线能力。

(3) **`_llm_history` 拼接中缺少身份声明**

`_llm_history`（`copilot.py:129-144`）构建的 `history` 数组以 system summary 或 user messages 开头，然后直接拼到 `call_deepseek_agent_plan` 的 `messages` 参数里。模型看到的第一个 system 消息是 `ROUTER_PROMPT`（只含路由规则，不含身份声明），这意味着 Router 阶段的模型不知道自己是"ClawSphere"——只知道自己是"路由器"。

当一个用户问"你是谁"时，Router 会倾向于"直接回答"（不调工具），然后将这一定位传递到 Responder。Responder 的 `RESPONDER_PROMPT` 也不含身份声明（只含输出格式要求），因此模型只能凭对话历史理解身份。如果历史中没有身份信息，回答可能不准确。

### 3.3 修复建议

**短期方案（影响最小）：**

- 将 `SYSTEM_PROMPT` 的内容作为公共前缀分别拼入 `ROUTER_PROMPT` 和 `RESPONDER_PROMPT` 的开头。修改方式如下：

```python
# copilot.py
BASE_PROMPT = SYSTEM_PROMPT  # 公共前缀

ROUTER_PROMPT = BASE_PROMPT + "\n\n" + """当前阶段：路由决策。
根据完整对话判断用户意图,自行决定调用哪些只读工具获取真实数据..."""

RESPONDER_PROMPT = BASE_PROMPT + "\n\n" + """当前阶段：回答生成。
必须严格输出 JSON,字段:..."""
```

这样，Router 和 Responder 共享同一个前缀 `BASE_PROMPT`，DeepSeek 的 Prefix KV Cache 在 responder 调用时可以命中 router 调用的缓存。

**中期方案（推荐）：**

- **单一 Prompt 模板 + 阶段切换**：定义一个 `PROMPT_TEMPLATE`，Router 和 Responder 各自注入当前阶段的指令块。例如：

```python
PROMPT_TEMPLATE = """{identity}
{skill_catalog}

当前阶段：{stage_name}
{stage_instructions}"""

ROUTER_PROMPT = PROMPT_TEMPLATE.format(
    identity=IDENTITY_TEXT,
    skill_catalog=startup_skill_summaries(),
    stage_name="路由决策",
    stage_instructions=ROUTER_RULES,
)

RESPONDER_PROMPT = PROMPT_TEMPLATE.format(
    identity=IDENTITY_TEXT,
    skill_catalog=startup_skill_summaries(),
    stage_name="回答生成",
    stage_instructions=RESPONDER_RULES,
)
```

这样不仅实现了前缀缓存命中，也确保了身份声明的单点维护。

- **可选优化**：Router 阶段如果不想让路由规则干扰 Responder（反之亦然），可以将 `stage_instructions` 放在 Prompt 的**末尾**而非开头。DeepSeek 的 prefix cache 检查的是从数组起始位置的连续匹配，末尾的不同不影响缓存命中。

**长期方案：**

- 将 Prompt 管理从硬编码字符串升级为**Prompt Registry**：所有 Prompt 模板存储在 YAML/JSON 配置文件中，支持 A/B 测试、版本管理和运行时热更新。类似于 `TOOL_REGISTRY` 的 `mcp_tool` 装饰器模式。

### 3.4 关联排查项

| 排查点 | 说明 |
|---|---|
| `app.py` 是否直接用 `SYSTEM_PROMPT` 做独立对话 | 搜索结果显示 `app.py` 中没有对 `SYSTEM_PROMPT` 的引用。需要确认外部调用方是否存在、是否需要同步适配。 |
| `call_deepseek_json` 的 `system_prompt` 与 `_responder_payload` 的 token 比例 | 当前 `RESPONDER_PROMPT` 约 150 tokens，`_responder_payload` 通常 2000+ tokens。system 部分占比小，前缀缓存的绝对收益有限，但相对收益（对 router→responder 调用链）仍然可观。 |
| `call_deepseek_agent_plan` 的 `history` 参数是否也会被缓存 | `_llm_history` 构建的 history 是动态的（包含 BM25 结果和近期消息），不会在 router→responder 间共享。只有 system 前缀是可缓存的。 |
| `llm_available=False` 降级链路中的 Prompt 使用 | 当 LLM 不可用时（`LLM_UNAVAILABLE_MESSAGE`），降级链路不使用任何 system prompt，直接返回固定消息。前缀缓存对此路径无影响。 |
| 多轮对话中前缀缓存是否持续有效 | 同一会话的多轮 router 调用之间，`ROUTER_PROMPT` 不变，会自动命中缓存。优化主要收益在**同一次 user message 内**的 router→responder 两次调用。 |

---

## 总结与优先级建议

| 优先级 | 问题 | 改动量 | 风险 | 收益 |
|---|---|---|---|---|
| **P0** | 技能加载不完整（tier-2 缺失的兜底） | 中（新增 1 个图节点） | 低 | 解决冷启动下的技能缺失问题 |
| **P0** | MCP 工具全量加载（token 膨胀） | 中（增加 category 字段 + 轻量路由） | 中 | 随工具数量增长线性降低 token 消耗 |
| **P1** | prompt 前缀缓存优化 | 小（修改 3 个字符串） | 低 | 每次对话节省 ~20% Router 阶段 token |
| **P2** | tool_results 智能截断 | 小（改进 `_cap_text`） | 低 | 避免数据丢失导致回答不准确 |
| **P2** | 向量语义检索（替换 BM25） | 大（接入 Embedding 服务） | 中 | 从根本上解决分词不匹配问题 |

建议按 P0 → P1 → P2 的顺序实施，P0 的两项可以并行进行。
