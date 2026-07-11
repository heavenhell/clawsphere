# SKILL: alert_query
version: 1.0
doc_type: skill
tags: [告警, FusionCompute, 根因, Runbook]
permission: public
applicable_roles: [readonly, ops, admin]

## 一句话（第一层）
区分告警列表查询与单条告警解释，并用关联资源证据给出处置建议。

## 摘要（第二层）
“有哪些告警”先调用 list_alarms；“为什么告警”再调用 get_alarm_detail。回答必须包含告警级别、对象、证据、建议和风险，不得把已清除告警说成活动告警。

## 详细步骤（第三层）
1. 列表问题调用 list_alarms，可按 severity 过滤。
2. 解释问题解析 alarm_id 或结合最近一次列表中的序号。
3. 调用 get_alarm_detail 获取主机、VM 或数据存储上下文。
4. 容量告警检查剩余比例；CPU 告警检查主机负载与 VM CPU Ready；内存告警检查 balloon/swap。
5. 只给可验证结论，写操作必须进入审批。
