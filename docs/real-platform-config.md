# 真实平台一文件配置

运行时只读取 `config/platforms.json`。该文件已加入 `.gitignore`，不会提交平台密码。

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

## eDME 客户端委托方式（推荐）

### 凭证来源

委托链与租户数量无关，区别只在这一个 session 从哪来：

- **单租户引导（当前）**：`single_tenant_bootstrap: true` 配合 `username` / `password`。首次请求时 Agent 用它换取 `accessSession` 注册进内存 broker，之后与按人登录完全同路——同样按身份隔离、同样签名下发、MCP Server 同样不保存凭证。这个开关必须显式打开：只写 `auth_mode: "client"` 却填了凭证会直接拒绝启动，避免在以为按人隔离的前提下悄悄共用一个账号。
- **按人登录（接入用户登录之后）**：配置里不放任何凭证，只留 `ip`，由每个用户各自调用 `POST /api/platform-sessions/edme`。同一身份一旦这样登录过就以它为准，不再使用引导凭证，因此两种来源可以并存过渡。

引导凭证只接受账号密码，不接受预先取好的 `session`：broker 需要能在过期后自行重新登录，写死的 session 失效后没有恢复路径。

单租户引导阶段，eDME 侧的审计和 RBAC 看到的仍然是同一个账号，追不到具体的人；这一层要等按人登录接入后才成立。`DELETE /api/platform-sessions/edme` 在引导模式下只清掉当前 session，下一次请求会重新引导。

### 按人登录流程

用户登录 Agent 后调用 `POST /api/platform-sessions/edme`，请求体仅包含 `username` 和 `password`。Agent 立即通过配置中的固定 eDME 地址交换 `accessSession`，随后丢弃密码；响应只返回连接状态、session 标识和过期时间，不返回 `accessSession`。

Agent 在内存中按 `user_id + tenant_id + platform_id` 隔离 session，并为每个身份建立独立 MCP 连接。session 被封装为签名委托 JWT 放入 `X-ClawSphere-Platform-Credential` Header；MCP Server 校验签名及用户、租户绑定后，才把其中的 `accessSession` 用作下游 `X-Auth-Token`。模型、工具参数、审计和日志都不会看到账号、密码或 session。

可用接口：

- `POST /api/platform-sessions/edme`：登录并替换当前用户的 eDME session。
- `GET /api/platform-sessions/edme`：查询当前用户连接状态；配置了引导凭证时在这里完成首次换票，登录失败不抛异常，而是在 `error` 字段里说明原因。
- `DELETE /api/platform-sessions/edme`：从 Agent 内存注销当前用户 session。

生产环境必须在 Agent 和 MCP Server 同时配置相同且独立的 `DCS_MCP_DELEGATION_SECRET`（至少 32 字符）。委托 JWT 是签名而非消息级加密，因此远程 MCP 地址和 eDME 登录地址都强制使用 HTTPS；只有 `127.0.0.1`、`localhost` 和 `::1` 允许 HTTP。反向代理和网关访问日志必须对 `X-ClawSphere-Platform-Credential` Header 做删除或脱敏。

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
- 同时填写 session 和账号密码时，系统先使用 session；收到 401/403 后自动用账号密码重新登录。
- 只填写 session 时不会主动登录；session 失效后会明确报错，需要更新配置并重启服务。
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

可通过 `GET /api/platform-status` 检查每个平台当前是 `real`、`client-delegated` 还是 `mock`。

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
