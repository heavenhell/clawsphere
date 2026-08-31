# 真实平台一文件配置

运行时只读取 `config/platforms.json`。该文件已加入 `.gitignore`，不会提交平台密码。

默认内容如下。可填写 `ip + username + password`，也可填写 `ip + session`；留空的平台继续使用 Mock：

```json
{
  "fusioncompute": {
    "ip": "192.0.2.20",
    "username": "northbound-user",
    "password": "replace-me",
    "session": ""
  },
  "edme": {
    "ip": "192.0.2.30",
    "username": "northbound-user",
    "password": "replace-me",
    "session": ""
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

Agent 会在每次 `tools/list`/`tools/call` 请求的 MCP `_meta` 中注入短期签名调用者令牌，包含 `user_id`、`roles`、`tenant_id` 和任务绑定；这些控制字段不出现在模型可见的工具参数 Schema 中。FusionCompute/eDME 等设备凭证仍由 MCP Server 根据其本地平台配置读取，不会传入模型或 Agent 的工具参数。

当前 HITL 审批记录存储在 SQLite。同主机的 Agent 与 MCP Server 可通过相同 `DCS_DATA_DIR` 使用同一数据库；不要把 SQLite 文件放到跨主机网络文件系统。真正跨主机且需要执行写工具时，必须先接入统一审批服务。否则 Server 查不到与 task_id、租户、工具和参数完全匹配的已批准记录，会返回 `APPROVAL_REQUIRED`，不会降级或绕过审批。只读工具不依赖共享审批存储。

可通过 `GET /api/platform-status` 检查每个平台当前是 `real` 还是 `mock`。

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
