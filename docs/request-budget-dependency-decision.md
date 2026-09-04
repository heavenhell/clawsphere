# 90 KiB 请求预算：依赖决策

## 决策

不新增 Python 生产依赖。外部 DeepSeek 请求继续使用项目已有的 `httpx`；语义压缩通过
同一个 HTTP 客户端调用可选的本机 Ollama `POST /api/chat`，默认模型为
`qwen3.5:4b-q4_K_M`。Ollama 是部署时的本机运行时，不加入 Python requirements。

## 需求与边界

- 最终发往 DeepSeek 的紧凑 UTF-8 JSON body 必须小于等于 92,160 字节。
- 测量的 bytes 必须与 HTTP 客户端实际发送的 bytes 是同一份数据。
- 固定 system/developer 指令、当前用户请求、工具 schema 与模型参数不可压缩。
- 只允许把已脱敏的可压缩历史发送到 loopback 压缩器。
- 压缩器未安装、不可用、输出无效或压缩后仍超限时失败关闭。

## 选项比较

| 选项 | 依赖/运行成本 | 结论 |
|---|---|---|
| 项目已有 `httpx` + Ollama HTTP API | 无新增 Python 包；Ollama 为可选本机运行时 | 采用。集成面最小，已有超时与测试模式可复用 |
| 新增 Ollama Python SDK | 新增生产依赖，能力与简单 HTTP 调用重复 | 不采用 |
| llama.cpp server | 可行且有 OpenAI 兼容服务，但 Windows 安装与模型管理需要额外运维 | 保留为未来适配器，不在本 PR 引入 |
| 仅确定性截断 | 无依赖，但会丢失资源 ID、错误码、决策和待办语义 | 只用于裁剪模型已生成的摘要以满足最终字节硬门禁，不作为主压缩器 |

## 当前官方依据

- Ollama 的本地 API 默认位于 `http://localhost:11434/api`，Chat 接口为
  `POST /api/chat`：<https://docs.ollama.com/api/introduction>、
  <https://docs.ollama.com/api/chat>
- Ollama 为 MIT 许可：<https://github.com/ollama/ollama/blob/main/LICENSE>
- `qwen3.5:4b-q4_K_M` 当前标签约 3.4 GB、标注 256K context：
  <https://ollama.com/library/qwen3.5/tags>

## 适配边界

`backend.agent.request_budget.enforce_request_budget()` 只接受已脱敏 payload，并返回
`BudgetedRequest.body`。调用方必须发送该 body，不能再次 JSON 序列化。压缩器通过
`SemanticCompressor` callable 注入，后续切换 llama.cpp 或其他本机模型时无需改动
DeepSeek 客户端和硬门禁。
