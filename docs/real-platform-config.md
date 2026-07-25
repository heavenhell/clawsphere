# 真实平台一文件配置

运行时只读取 `config/platforms.json`。该文件已加入 `.gitignore`，不会提交平台密码。

默认内容如下。可填写 `ip + username + password`，也可填写 `ip + session`；留空的平台继续使用 Mock：

```json
{
  "fusioncompute": {
    "ip": "192.168.10.20",
    "username": "northbound-user",
    "password": "replace-me",
    "session": ""
  },
  "edme": {
    "ip": "192.168.10.30",
    "username": "northbound-user",
    "password": "replace-me",
    "session": ""
  },
  "mcp": {
    "enabled": true,
    "transport": "streamable-http",
    "host": "127.0.0.1",
    "port": 8020
  }
}
```

## Session 方式

已有有效会话时，可以不保存平台账号密码：

```json
{
  "fusioncompute": {
    "ip": "192.168.10.20",
    "username": "",
    "password": "",
    "session": "FusionCompute-X-Auth-Token"
  },
  "edme": {
    "ip": "192.168.10.30",
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
- MCP 默认监听 `http://127.0.0.1:8020/mcp`，由 `start-demo.ps1` 一起启动。

可通过 `GET /api/platform-status` 检查每个平台当前是 `real` 还是 `mock`。

## 可选项

三项基础配置之外，仅在现场环境需要时添加：

```json
{
  "fusioncompute": {
    "ip": "192.168.10.20",
    "username": "northbound-user",
    "password": "replace-me",
    "port": 7443,
    "site_id": "3C0207D5",
    "api_version": "v6.3",
    "ca_cert": "C:\\certs\\fusioncompute-ca.pem"
  },
  "edme": {
    "ip": "192.168.10.30",
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
