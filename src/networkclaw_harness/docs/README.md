# NetworkClaw Harness 文档

本目录随 `networkclaw_harness` 源码包、wheel 和离线交付制品一起发布，是当前仓库的
架构事实来源。

文档关系是演进而非并列：

```text
Coordinator 语义运行时（前序语义设计、原设想内置于 chatsvc）
    -> Hermes Headless Harness（当前目标、从 chatsvc 分离出的 coordinator/agent 内核）
```

- [Hermes Headless Harness 内核设计](./hermes-headless-harness.md)：当前架构事实来源。
- [Harness 进程形态：chatsvc 与 Harness 1:1](./process-topology.md)：已确认的进程归属、
  多 session 复用、故障域和生命周期决策。
- [Coordinator 语义运行时](./coordinator-semantic-runtime.md)：保留的前序语义设计和演进背景；其中 coordinator 留在 chatsvc 的部署结论已失效。

开发实施计划位于 [`plan/`](./plan/)，按编号严格遵循依赖顺序执行；后置模块不得成为前置
模块的隐含依赖。计划状态使用 ✅ 已完成、🟨 待确认或已有基础但未验收、⬜ 未开始。

修改 Harness 的协议、工作区、恢复、Hermes vendor、离线交付或 chatsvc 边界时，应在
同一变更中更新这里的对应设计说明。
