# DCS Copilot Demo

华为 DCS/FusionCompute 运维 Agent demo，包含：

- React/Vite 运维 Copilot 前端
- 独立查询工作台与管理员审批台
- FastAPI tool gateway
- LangGraph 风格 Agent 编排
- DeepSeek LLM 接入
- FusionCompute mock 数据
- Dorado mock 存储池数据
- 告警解释、容量预测、VM 性能诊断三个演示场景

## 本地运行

后端：

```powershell
cd E:\tmp\code\dcs-copilot-demo
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8010 --reload
```

前端：

```powershell
cd E:\tmp\code\dcs-copilot-demo\frontend
pnpm dev --host 127.0.0.1 --port 5174
```

页面地址：

- 查询工作台：http://127.0.0.1:5174/
- 管理员审批台：http://127.0.0.1:5174/approval

DeepSeek API key 从环境变量读取：

```powershell
$env:DEEPSEEK_API_KEY="..."
```

如果未配置 key，后端会使用 deterministic fallback，方便离线演示。

一键启动：

```powershell
cd E:\tmp\code\dcs-copilot-demo
.\start-demo.ps1
```

## Architecture coverage

Implemented from the design docs:

- LangGraph nodes: `intent_classifier -> context_loader -> memory_retriever -> planner -> guardrail -> tool_executor / hitl_interrupt -> response_generator -> memory_writer`.
- Context management: recent 6 full turns plus a rolling summary field for older messages.
- Skill/RAG: local skill retrieval with `retrieved_docs`.
- Tool gateway: FastAPI tools with role and risk metadata.
- Guardrail: role checks, write-intent blocking, risk routing, and frequency-limit placeholder.
- HITL: high-risk actions enter an approval queue and are not executed directly.
- Audit/memory: tool audit log plus memory summary records.
- MCP wrapper: `backend/mcp/mcp_server.py` exposes stdio-style `tools/list` and `tools/call`.
- Eval: run `python -m eval.evaluator`.

Current storage modes:

- 默认 `DCS_MEMORY_BACKEND=sqlite`，用于无需外部服务的本地演示。
- 设置 `DCS_MEMORY_BACKEND=postgres` 和 `POSTGRES_DSN` 后，Skill 知识进入 pgvector；
  `docker compose up -d postgres` 可启动本项目的 pgvector 环境。
- 检索使用 BM25、128 维哈希向量和 RRF 融合，先做租户与密级过滤。
- 对话保留最近 6 轮，旧内容压缩为滚动摘要，并持久化到本地数据库。
- LangGraph checkpoint 默认使用 SQLite；设置 `DCS_CHECKPOINT_BACKEND=postgres`
  后使用 PostgresSaver，支持 HITL 跨进程恢复。
- Langfuse and Prometheus are not connected to external services yet.

## MCP Server

官方 MCP Python SDK Server 默认使用 stdio：

```powershell
python -m backend.mcp.mcp_server
```

设置 `MCP_TRANSPORT=streamable-http` 可切换为 Streamable HTTP。FastAPI 网关的
`/api/tools/call` 同时支持 JWT Bearer 身份，开发环境可通过 `/api/auth/demo-token`
获取演示令牌。

## HITL approval

ops/admin 发起重启、扩容或 HA 修改后，LangGraph 在工具执行前 `interrupt`。
审批项可从 `GET /api/approvals` 查询，并通过
`POST /api/approvals/{approval_id}/decision` 批准或拒绝；高风险操作必须由
admin 令牌审批。批准后使用原 conversation id 从 checkpoint 恢复，拒绝时不会调用写工具。

## Quality and observability

```powershell
python -m pytest -q
python -m eval.evaluator
```

评测报告包含意图、工具、事实和安全四个维度，默认质量门禁为 90%，安全维度必须
达到 100%。Prometheus 指标位于 `http://127.0.0.1:8010/metrics/`。配置
`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 与 `LANGFUSE_BASE_URL` 后，Agent
运行会写入 Langfuse v4 Trace；未配置时不会影响本地运行。
