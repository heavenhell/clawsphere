# eDME 24.1.0 接入说明

## 接入范围

当前按照《eDME 24.1.0 运维面 API 接口参考》和《运维面北向 API 应用开发指南》接入以下只读能力：

| 能力 | 官方接口 | Demo Mock 接口 | MCP 工具 |
|---|---|---|---|
| 登录认证 | `PUT /rest/plat/smapp/v1/sessions` | `PUT /mock/edme/rest/plat/smapp/v1/sessions` | 内部 Mock 不需要登录 |
| 当前告警 | `POST /rest/alarmmgmt/v1/alarms/current-alarm/query` | 同路径增加 `/mock/edme` 前缀 | `query_edme_current_alarms` |
| 资源实例 | `GET /rest/resourcedb/v1/instances/{className}` | 同路径增加 `/mock/edme` 前缀 | `query_edme_resources` |
| 监控对象类型 | `GET /rest/metrics/v1/mgr-svc/obj-types` | 同路径增加 `/mock/edme` 前缀 | `get_edme_metric_catalog` |
| 指标目录 | `GET /rest/metrics/v1/mgr-svc/obj-types/{obj-type-id}/indicators` | 同路径增加 `/mock/edme` 前缀 | `get_edme_metric_catalog` |
| 历史性能 | `POST /rest/metrics/v1/data-svc/history-data/action/query` | 同路径增加 `/mock/edme` 前缀 | `query_edme_performance_history` |

资源列表接口在官方文档中已标记为过期。Mock 保留它是为了演示分页和系统资源模型；真实环境适配时应优先根据现场版本选择对应存储、虚拟化等业务域的列表接口。

## 调用链路

```text
用户问题
  -> LangGraph 识别 edme_operations
  -> MCP Tool Gateway 做 Schema、RBAC 和审计
  -> EDMEInterface 平台契约
  -> MockRepository 读取 eDME 场景 JSON
```

HTTP Mock 面向需要验证 eDME 原始协议的调用方，MCP 工具面向 Agent。两者共享同一个 Repository，因此不会维护两套互相矛盾的场景数据。

## HTTP Mock 调用示例

先登录获取 `accessSession`：

```http
PUT /mock/edme/rest/plat/smapp/v1/sessions
Content-Type: application/json

{
  "grantType": "password",
  "userName": "northbound",
  "value": "demo-secret"
}
```

后续请求携带：

```http
X-Auth-Token: edme-mock-access-session
```

## 替换为真实 eDME

真实 eDME 默认使用 `https://<管理IP>:26335`，需要 TLS 1.2 以上和“三方系统接入”类型的北向用户。生产适配器应实现 `EDMEInterface`，完成以下工作：

1. 调用 sessions 接口并缓存 `accessSession`，在过期或收到 403 后重新认证。
2. 所有请求发送 `Accept: application/json`、`Content-Type: application/json`、`Accept-Charset: utf8` 和 `X-Auth-Token`。
3. 校验服务端证书，使用现场导出的 `trust.cer`，不要关闭 TLS 校验。
4. 将 eDME 原始响应转换为当前领域模型，保持 MCP 工具参数和 Agent 逻辑不变。
5. 对当前告警处理 `iterator`，直到返回空 `hits`；同时遵守文档中的接口频控。

Mock 数据位于 `backend/mock/data/edme-*.json`，平台契约位于 `backend/interfaces/edme.py`。
