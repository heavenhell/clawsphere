# ClawSphere 项目长期记忆

## 项目概述
- 面向 DCS/FusionCompute/eDME 运维场景的 AI Copilot 系统
- 基于 LangGraph 构建的状态图，DeepSeek 作为 LLM，四阶段渐进式 Skill/工具加载（见 docs/four-stage-agent-architecture-proposal.md、docs/four-stage-implementation-plan.md）
- 核心流程：context_loader → history_retriever → skill_router(LLM1) → skill_loader → tool_search_planner(LLM2) → tool_catalog_search → tool_call_planner(LLM3) → guardrail → hitl_interrupt → tool_executor → llm_responder(LLM4) → memory_writer
  （skill_router 的 direct_answer/clarification_required、tool_search_planner 无候选两处可短路直接进 llm_responder）

## 关键技术决策

### 依赖关系
- memory/database.py ↔ guardrails/permission.py、guardrails/policy.py ↔ mcp/tools.py 存在预存循环依赖
- 各模块的 __init__.py 必须使用 PEP 562 __getattr__ 延迟导入模式

### 上下文管理
- 首次对话时无对话历史，依赖当前消息 + 预置技能文档 + 种子案例
- SYSTEM_PROMPT 在 copilot.py 中构建但未被流水线直接使用，Router 和 Responder 使用独立 Prompt
- BM25 检索技能文档（tier-2），top_k=3；历史案例 top_k=2

### 已知架构问题（详见 docs/architecture-issues-analysis.md）
- P0: 技能加载不完整（BM25 未命中时无兜底机制）
- P0: MCP 工具全量加载（缺少语义层面的工具筛选）
- P1: 三个 System Prompt 独立存在，无前缀缓存共享
