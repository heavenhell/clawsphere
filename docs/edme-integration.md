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

## 已接入的业务域接口

除上面的运维面只读能力外，适配器还接入了以下业务域接口（`backend/adapters/edme.py`）：

| 能力 | 接口 | 归一化产物 |
|---|---|---|
| 站点/集群/主机 | `POST /rest/vmmgmt/v1/{sites,clusters,hosts}/query` | `virtual_sites/clusters/hosts()`，百分数转比值，主机多 IP 取管理 IP |
| 虚拟机 | `POST /rest/vmmgmt/v1/vms/query` | `virtual_vms()`，`cpu`/`memory` 嵌套对象拍平 |
| 当前告警 | `POST /rest/alarmmgmt/v1/alarms/current-alarm/query` | `virtual_alarms()`，数值 severity 转标签，MOI 解析出对象类型和 ID，毫秒时间戳转 ISO |
| 存储池 | `POST /rest/storagemgmt/v1/storagepools/query` | `edme_storage_pools()` / `datastores()`，容量 MB 转 GB |

配置 eDME 后，`repo.sites/clusters/hosts/vms/alarms` 会路由到上述接口（`backend/providers.py`）。

### 两个必须知道的限制

1. **`vms/query` 不支持分页**：传 limit/offset 无效，只返回首页（约 20 条）；但 `site_id`/`cluster_id`/`name`/`status` 过滤是生效的。因此 `virtual_vms()` 在无过滤条件时**按集群逐个查询再合并去重**，并在某集群返回数量少于其自报 `vm_num` 时打 WARNING 日志。`overview()` 的 `vm_count`/`host_count` 取集群自报总数而非列表长度——列表可能被截断，平台自报的总数不会。

2. **性能指标不由 eDME 提供**：`metrics()` / `vm_metrics()` / `cluster_daily_growth_gb()` 来自 FusionCompute。只配了 eDME 时这些调用会**抛出明确错误**而不是回落到 Mock 数据——把演示数据当作真实平台状态返回，正是 grounding 护栏要防的那类幻觉。

回归测试见 `tests/test_edme_adapter.py`（15 条，按真实响应形状构造）。

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
