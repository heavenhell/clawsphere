# SKILL: capacity_forecast_standard
version: 1.0
doc_type: skill
tags: [容量, 预测, 集群, 数据存储]
permission: public
applicable_roles: [readonly, ops, admin]

## 一句话（第一层）
基于集群当前剩余容量和历史日增长率估算耗尽时间与风险等级。

## 摘要（第二层）
容量预测必须限定到指定集群，先查当前容量，再运行预测。14 天内耗尽为 critical，14 至 30 天为 high，超过 30 天按增长和剩余比例评估。

## 详细步骤（第三层）
1. 调用 get_cluster_capacity 获取集群关联数据存储容量。
2. 调用 run_capacity_forecast，默认预测 30 天，允许 1 至 365 天。
3. 输出日增长、预计耗尽天数、风险等级和建议时间窗。
4. 扩容属于写操作；用户明确要求执行扩容时必须进入审批，不得仅返回预测。
