# 06 Session Runtime 与语义持久化

目标：让一个 Harness 安全承载多个 session，并保存足够的语义事实供新进程恢复而不制造第二套状态机。

模块状态：⬜ 未开始

前置依赖：05 工作区、epoch 和 artifact 基础。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 06.1 Session registry

- ⬜ 实现 session.open、resume、close 的状态机和容量控制。
- ⬜ 所有内存状态按 session_id 分区，禁止模块全局“当前 session”。
- ⬜ 并发打开同一 session 时结合 epoch 和幂等键收敛为唯一 owner。

## 06.2 历史与消息不变量

- ⬜ 复用 Hermes session persistence，定义 NetworkClaw adapter 边界。
- ⬜ 保持严格角色交替和合法 tool-call/tool-result 配对。
- ⬜ 区分用户交互、model step、tool round 和最终交付。
- ⬜ 不通过修改历史消息实现中途控制或恢复。

## 06.3 语义状态模型

- ⬜ 持久化 goal、plan、run、interaction、invocation、outcome、artifact refs 和 unresolved items。
- ⬜ 定义 running、waiting、completed、failed、cancelled、interrupted、blocked、expired 状态边界。
- ⬜ 取消请求与取消确认、执行成功与业务成功、连接断开与结果未知必须分开。

## 06.4 检查点与写入顺序

- ⬜ 定义计划、工具 intent、授权、工具结果和检查点的可判定顺序。
- ⬜ 使用原子文件/事务边界，避免半写状态被当成完成。
- ⬜ 关键事实写失败时明确停止或降级，不静默继续高副作用动作。

## 06.5 多 session 隔离测试

- ⬜ 同一 Harness 中并发执行 A/B/A，验证上下文、事件、预算、审批、工作区和 cache 不串线。
- ⬜ session close 不影响同 Harness 其他 session。
- ⬜ session 重新打开只加载自身事实和 artifact。

## 06.6 模块出口

- ⬜ 多 session 生命周期、语义持久化和检查点合同通过行为测试。
- ⬜ 新进程可读取事实但不会自动重放旧动作后，将模块状态更新为 ✅。

