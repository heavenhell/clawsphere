# 真实平台与客户端委托鉴权配置

Agent 与 MCP Server 使用独立配置路径：

- Agent：`DCS_AGENT_PLATFORM_CONFIG`，默认 `config/platforms.json`。
- MCP Server：`DCS_MCP_SERVER_PLATFORM_CONFIG`；客户端委托模式下建议复制
  `config/platforms.mcp-server.example.json` 为 `config/platforms.mcp-server.json`。
- `DCS_PLATFORM_CONFIG` 仅作为兼容回退。组件专用变量优先级更高。

实际配置文件已加入 `.gitignore`，示例文件可以提交。客户端委托模式下，Agent
配置保存单租户引导账号密码；MCP Server 配置只保存同一个 eDME endpoint，不能
保存账号、密码、session，也不能打开 `single_tenant_bootstrap`。MCP Server 若读到
此类配置会直接拒绝启动。

默认推荐 eDME 使用客户端委托鉴权：业务凭证由 Agent 在运行时换成 session，签名后随每个请求下发给 MCP Server，MCP Server 自身不保存任何平台凭证。

Agent 目前还没有用户登录，只有一个租户，所以把这个唯一租户的账号密码写在配置里并打开 `single_tenant_bootstrap`。委托链本身不随租户数量变化，将来接入登录后改变的只是凭证来源。FusionCompute 仍使用服务端凭证方式；留空的平台继续使用 Mock：

```json
{
  "fusioncompute": {
    "ip": "192.0.2.20",
    "username": "northbound-user",
    "password": "replace-me",
    "session": ""
  },
  "edme": {
    "auth_mode": "client",
    "single_tenant_bootstrap": true,
    "ip": "192.0.2.30",
    "username": "northbound-user",
    "password": "replace-me"
  },
  "mcp": {
    "enabled": true,
    "agent_mode": "mcp",
    "transport": "streamable-http",
    "host": "127.0.0.1",
    "port": 8020,
    "url": "",
    "connect_timeout_seconds": 5,
    "call_timeout_seconds": 30
  }
}
```

对应的 MCP Server 配置必须使用相同 endpoint，但不包含任何 eDME 凭证：

```json
{
  "edme": {
    "auth_mode": "client",
    "single_tenant_bootstrap": false,
    "ip": "192.0.2.30",
    "expired_session_error_codes": []
  },
  "mcp": {
    "enabled": true,
    "agent_mode": "mcp",
    "host": "127.0.0.1",
    "port": 8020
  }
}
```

如果 Agent 的引导凭证缺失，Agent 仍会启动并把 eDME 显示为 unavailable，等待按人
登录或配置修复。`start-demo.ps1` 在客户端委托模式找不到无凭证 Server 配置时仍会
启动 Agent 和前端，但不会启动本地 MCP Server。

## eDME 客户端委托方式（推荐）

### 凭证来源

委托链与租户数量无关，区别只在这一个 session 从哪来：

- **单租户引导（当前）**：`single_tenant_bootstrap: true` 配合 `username` / `password`。首次请求时 Agent 用它换取 `accessSession` 注册进内存 broker，之后与按人登录完全同路——同样按身份隔离、同样签名下发、MCP Server 同样不保存凭证。这个开关必须显式打开：只写 `auth_mode: "client"` 却填了凭证会直接拒绝启动，避免在以为按人隔离的前提下悄悄共用一个账号。
- **按人登录（接入用户登录之后）**：配置里不放任何凭证，只留 `ip`，由每个用户各自调用 `POST /api/platform-sessions/edme`。同一身份一旦这样登录过就以它为准，不再使用引导凭证，因此两种来源可以并存过渡。

引导凭证只接受账号密码，不接受预先取好的 `session`：broker 需要能在过期后自行重新登录，写死的 session 失效后没有恢复路径。

单租户引导阶段，eDME 侧的审计和 RBAC 看到的仍然是同一个账号，追不到具体的人；这一层要等按人登录接入后才成立。`DELETE /api/platform-sessions/edme` 定义为“重置连接/强制重新换票”：在引导模式下清掉当前 session，下一次请求重新登录换票。

### 按人登录流程

用户登录 Agent 后调用 `POST /api/platform-sessions/edme`，请求体仅包含 `username` 和 `password`。Agent 立即通过配置中的固定 eDME 地址交换 `accessSession`，随后丢弃密码；响应只返回连接状态、session 标识和过期时间，不返回 `accessSession`。

Agent 在内存中按 `user_id + tenant_id + platform_id` 隔离 session，并为每个身份建立独立 MCP 连接。session 被封装为签名委托 JWT 放入 `X-ClawSphere-Platform-Credential` Header；MCP Server 校验签名及用户、租户绑定后，才把其中的 `accessSession` 用作下游 `X-Auth-Token`。模型、工具参数、审计和日志都不会看到账号、密码或 session。

委托 JWT 与对应 eDME `accessSession` 同时过期，eDME 返回的生命周期上限按 24 小时处理。JWT 还携带规范化 eDME origin 的 SHA-256 标识；MCP Server 用本地配置重新计算并严格匹配。客户端决定目标 endpoint，但 Server 只有在本地明确配置了同一目标时才会转发，防止委托 token 被拿到另一套 eDME 使用。

可用接口：

- `POST /api/platform-sessions/edme`：登录并替换当前用户的 eDME session。
- `GET /api/platform-sessions/edme`：查询当前用户连接状态；配置了引导凭证时在这里完成首次换票，登录失败不抛异常，而是在 `error` 字段里说明原因。
- `DELETE /api/platform-sessions/edme`：从 Agent 内存注销当前用户 session。

生产环境必须在 Agent 和 MCP Server 同时配置相同且独立的 `DCS_MCP_DELEGATION_SECRET`（至少 32 字符）。委托 JWT 是签名而非消息级加密，因此远程 MCP 地址和 eDME 登录地址都强制使用 HTTPS；只有 `127.0.0.1`、`localhost` 和 `::1` 允许 HTTP。反向代理和网关访问日志必须对 `X-ClawSphere-Platform-Credential` Header 做删除或脱敏。

### 过期、刷新与重放规则

- HTTP 401 一律视为 session 失效。普通 403 视为权限不足，不触发换票。
- 现场确认某个 eDME 错误码明确表示 session/token 失效后，可把精确代码加入
  `expired_session_error_codes`；只匹配结构化响应中的 `error_code`、`errorCode`
  或 `code`，不按错误文本猜测。
- 同一用户和租户并发遇到失效时采用 single-flight：只有一个请求登录，其他请求
  等待并复用结果。失败结果冷却 2 秒，避免同时打爆登录接口。
- 只有显式标记 `retry_on_auth_expiry=true` 的纯查询工具在换票后自动重放一次。
  默认值为 false。
- 写工具只换票、不重放，返回 `PLATFORM_AUTH_REFRESHED_RETRY_REQUIRED`，提示
  “认证已刷新，请重新发起操作”；关联的已批准 HITL 记录会失效，重新发起时必须
  重新审批。换票失败返回 `PLATFORM_AUTH_REFRESH_FAILED`。
- 刷新尝试、成功、失败、single-flight 等待、只读重试和写操作未重放都记录在
  `clawsphere_mcp_client_events_total`，结构化日志只记录 endpoint、工具名、哈希身份
  和异常类型，不记录账号、密码、session 或委托 JWT。

## Session 方式

已有有效会话时，可以不保存平台账号密码：

```json
{
  "fusioncompute": {
    "ip": "192.0.2.20",
    "username": "",
    "password": "",
    "session": "FusionCompute-X-Auth-Token"
  },
  "edme": {
    "ip": "192.0.2.30",
    "username": "",
    "password": "",
    "session": "eDME-accessSession"
  }
}
```

- FusionCompute 的 `session` 会作为 `X-Auth-Token` 发送。
- eDME 的 `session` 会作为 `X-Auth-Token` 发送，值为登录返回的 `accessSession`。
- 同时填写 session 和账号密码时，系统先使用 session；收到 401（或配置的精确失效错误码）后换取新 session，但不会在适配器层重放原请求。
- 只填写 session 时不会主动登录；session 失效后会明确报错，需要更新配置并重启服务。普通 403 会原样作为权限不足返回。
- Session 与密码一样属于敏感凭证，不会出现在 `/api/platform-status` 或工具审计参数中。

## 自动行为

- FusionCompute 默认连接 `https://IP:7443`，调用 `/service/session` 登录，缓存 `X-Auth-Token`，并从 `/service/sites` 自动发现站点。
- eDME 默认连接 `https://IP:26335`，调用 `/rest/plat/smapp/v1/sessions` 登录并缓存 `accessSession`。
- eDME 配置完成后，存储池和数据存储查询优先使用 eDME；未配置时使用 FusionCompute 或 Mock。
- 任一真实平台启用后，`/mock/*` 路由默认关闭；全部平台留空时自动开启 Mock。
- `mcp.enabled=true` 只控制 `start-demo.ps1` 是否启动本机 MCP Server；Agent 连接模式由 `agent_mode` 独立控制。
- `agent_mode=mcp` 时 Agent 通过持久 MCP Client 动态调用 `tools/list` 和 `tools/call`。`url` 留空时连接 `http://host:port/mcp`。
- MCP Server 发送 `notifications/tools/list_changed` 后，Agent 会立即在后台刷新已知调用者的目录；执行前若发现目录版本变化，会丢弃旧计划并重新规划一次。重连后也会重新拉取目录。
- MCP 不可用或刷新失败时不会回退到本地工具，会返回明确错误，并记录结构化错误日志与 `clawsphere_mcp_client_events_total` 指标。告警通知接口已预留，外部通知渠道后续接入。
- `agent_mode=local` 只允许全部 Provider 都是 Mock 的调试环境；配置任何真实平台后默认切换为 `mcp`，显式配置 `local` 会拒绝启动。

## 远程 MCP Server

Agent 与 MCP Server 可以独立部署。连接远程 Server 时不需要在 Agent 进程启动本地服务：

```json
{
  "mcp": {
    "enabled": false,
    "agent_mode": "mcp",
    "transport": "streamable-http",
    "url": "https://mcp.example.internal/operations/mcp",
    "connect_timeout_seconds": 5,
    "call_timeout_seconds": 30
  }
}
```

`url` 必须是绝对 HTTP(S) 地址，不能包含用户名、密码、query 或 fragment。生产环境应使用 HTTPS 和受信任的内部网络/反向代理。

Agent 会在每次 `tools/list`/`tools/call` 请求的 MCP `_meta` 中注入短期签名调用者令牌，包含 `user_id`、`roles`、`tenant_id` 和任务绑定；这些控制字段不出现在模型可见的工具参数 Schema 中。使用 eDME `auth_mode=client` 时，MCP Server 不保存用户凭证列表，只接收与该调用者绑定的短期 Header 委托令牌。

当前 HITL 审批记录存储在 SQLite。同主机的 Agent 与 MCP Server 可通过相同 `DCS_DATA_DIR` 使用同一数据库；不要把 SQLite 文件放到跨主机网络文件系统。真正跨主机且需要执行写工具时，必须先接入统一审批服务。否则 Server 查不到与 task_id、租户、工具和参数完全匹配的已批准记录，会返回 `APPROVAL_REQUIRED`，不会降级或绕过审批。只读工具不依赖共享审批存储。

可通过 `GET /api/platform-status` 检查每个平台当前是 `real`、`client-delegated`、`unavailable` 还是 `mock`。

## 可选项

三项基础配置之外，仅在现场环境需要时添加：

```json
{
  "fusioncompute": {
    "ip": "192.0.2.20",
    "username": "northbound-user",
    "password": "replace-me",
    "port": 7443,
    "site_id": "3C0207D5",
    "api_version": "v6.3",
    "ca_cert": "C:\\certs\\fusioncompute-ca.pem"
  },
  "edme": {
    "ip": "192.0.2.30",
    "username": "northbound-user",
    "password": "replace-me",
    "port": 26335,
    "ca_cert": "C:\\certs\\edme-trust.pem"
  }
}
```

TLS 校验始终开启。若设备使用私有 CA 或自签名证书，需要填写 `ca_cert`，系统不会通过关闭证书校验来绕过错误。

## 生产限制

- 当前真实适配器首先覆盖只读资源、告警和性能查询。
- FusionCompute 容量预测需要平台返回 `dailyGrowthGB`，否则会明确提示需要 eDME 历史容量数据，不会使用 Mock 增长率。
- 写工具仍受现有审批和 RBAC 保护，目前不会直接向真实平台执行变更。
- MCP HTTP 服务当前适合内网或反向代理之后使用；对公网开放前仍需增加逐请求 OAuth/JWT Token Verifier。
