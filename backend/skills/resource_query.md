# SKILL: resource_query
version: 1.0
doc_type: skill
tags: [资源, 虚拟机, 主机, 集群, 存储]
permission: public
applicable_roles: [readonly, ops, admin]

## 一句话（第一层）
按用户指定的资源类型返回数量、状态和关键标识，未指定类型时返回资源总览。

## 摘要（第二层）
资源查询应识别 VM、主机、集群、存储或总览。数量问题同时返回简短列表；列表问题不得被误判为告警解释或容量预测。

## 详细步骤（第三层）
1. 总览调用 get_resource_overview。
2. VM 查询调用 list_vms，可按运行状态过滤。
3. 集群、主机和存储按资源类型输出名称、状态和关键容量。
4. 对不存在的资源明确说明未找到，不使用默认对象代替。
