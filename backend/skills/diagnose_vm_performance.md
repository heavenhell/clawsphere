# SKILL: diagnose_vm_performance
version: 1.2
doc_type: skill
tags: [VM, 性能诊断, CPU, 内存, 存储, 网络]
permission: public
applicable_roles: [readonly, ops, admin]

## 一句话（第一层）
诊断 VM 性能下降的标准流程：CPU、内存、存储、网络逐层排查。

## 摘要（第二层）
先确认 VM 身份和宿主机，再查询 cpu.ready、cpu.usage、mem.balloon、disk.latency 和 net.drop。CPU Ready 超过 5% 或存储延迟超过 20ms 应作为主要证据。

## 详细步骤（第三层）
1. 调用 get_vm_detail，确认 VM 存在、运行状态和宿主机。
2. 调用 get_vm_metrics，CPU Ready > 5% 判断为 CPU 争用；CPU usage > 80% 判断为高负载。
3. mem.balloon > 0 表示宿主机存在内存压力。
4. disk.latency > 20ms 表示存储瓶颈，关联 Dorado 存储池。
5. net.drop 持续升高时检查 vSwitch、MTU 和物理链路。
