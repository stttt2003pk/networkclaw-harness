# 12 恢复、故障接管与副作用对账

目标：让 chatsvc/Harness 进程中断或节点迁移后安全恢复已提交事实，并对未知副作用先核实再决定下一步。

模块状态：⬜ 未开始

前置依赖：11 已持久化且可投影的完整运行事实。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 12.1 恢复分类

- ⬜ 将记录分类为 completed、cancelled、waiting、safe-to-retry、unknown 和 corrupted。
- ⬜ 旧 running 不投影为新实例正在执行。
- ⬜ 旧 PID、管道、browser、MCP 和 subagent 句柄永不复活。

## 12.2 Recovery reconciler

- ⬜ 加载 session 状态、计划、交互、工具 intent/result、artifact 和 epoch。
- ⬜ 检查 schema/version 兼容并执行可审计迁移。
- ⬜ 对不完整写入、缺失 artifact 和 hash 不匹配产生明确诊断。

## 12.3 工具副作用恢复

- ⬜ 只读且显式声明幂等/可重试的工具可按策略重试。
- ⬜ 有副作用且结果 unknown 的工具必须查询实际状态或请求用户决策。
- ⬜ 幂等键、事件去重键和业务状态查询分别建模。

## 12.4 等待态恢复

- ⬜ clarification/approval 等待可在新 Harness 中恢复。
- ⬜ 旧审批按版本、范围和有效期重新校验，不能无限继承。
- ⬜ 用户新的输入可以选择继续、取消或开始关联新 run。

## 12.5 故障接管 E2E

- ⬜ 在 tool intent 前、外部副作用后、result 持久化前等故障点注入崩溃。
- ⬜ 使用同一 workspace 在新 1:1 进程对中恢复。
- ⬜ 执行 A→B→A 节点切换、epoch fencing 和双写拒绝测试。

## 12.6 模块出口

- ⬜ H4 恢复闭环通过，所有未知结果均不会盲目重放。
- ⬜ 故障注入和节点迁移测试可重复通过后，将状态更新为 ✅。

