# 01 架构合同与实施基线

目标：把产品意图、系统边界和不可违反的不变量冻结为后续所有模块共同依赖的工程合同。

模块状态：🟨 待冻结

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 01.1 设计演进关系

- ✅ 明确 `Coordinator 语义运行时` 是前序设计，`Hermes Headless Harness` 是其内核化演进。
- ✅ 明确 Harness 是唯一 coordinator/agent loop，chatsvc 最终只保留 host adapter、传输和生命周期职责。
- ✅ 明确迁移完成后禁止 chatsvc 与 Harness 长期保留双 coordinator。

## 01.2 进程与会话拓扑

- ✅ 冻结 `chatsvc : Harness = 1 : 1`。
- ✅ 冻结 `Harness : session = 1 : N`。
- ✅ 禁止多个 chatsvc 共享节点级 Harness。
- ✅ 明确 chatrtmgr 继续持有用户亲和、节点容量和 execution lease 权威。

## 01.3 统一术语与身份模型

- 🟨 统一 tenant、user、session、interaction、turn、run、model step、item、invocation、artifact 的定义。
- ⬜ 为每类身份规定生成方、作用域、持久化期限和关联关系。
- ⬜ 规定 `request_id`、`turn_id`、`run_id`、`invocation_id`、`event_id` 的幂等与去重边界。

## 01.4 能力范围和非目标

- 🟨 冻结首期必须保留的 Hermes 能力矩阵和约 95% 运行能力口径。
- ✅ 冻结 UI、网站、演示资源不进入运行 snapshot。
- ✅ 冻结运行时自进化关闭，skill/tool 变更只能通过 Git 和发布链进入生产。
- ⬜ 建立“保留、适配、替换、禁用、后置”五类能力清单。

## 01.5 架构决策记录

- ⬜ 建立 ADR 索引，至少覆盖进程形态、协议版本、工作区所有权、execution epoch、恢复和副作用策略。
- ⬜ 为后续重大变更规定 ADR 更新、兼容评估和回滚要求。
- ⬜ 建立文档链接检查，确保交付仓库文档不依赖缺失的外部相对路径。

## 01.6 模块出口

- ⬜ 所有术语、身份、职责和非目标无冲突。
- ⬜ 后续模块可仅引用本合同，不重新定义 chatsvc、chatrtmgr 和 Harness 的所有权。
- ⬜ 架构评审明确批准后，将模块状态更新为 ✅。

## 01.7 总体执行路线

下表同时是模块索引和依赖顺序。除修正文档或已完成脚手架外，模块 N 只有在模块 N-1 达到
“模块出口”后才能进入主体实现；不得以更大编号模块反向补齐较小编号模块的必要合同。

| 顺序 | 模块 | 当前状态 |
| --- | --- | --- |
| 01 | [架构合同与实施基线](./01_architecture_contracts.md) | 🟨 待冻结 |
| 02 | [Hermes 上游基线与 Vendor 供应链](./02_hermes_vendor_baseline.md) | 🟨 脚手架完成，H0 未验收 |
| 03 | [Headless Host 协议 v1](./03_host_protocol_v1.md) | 🟨 最小协议完成，v1 未冻结 |
| 04 | [chatsvc 与 Harness 1:1 进程生命周期](./04_process_lifecycle_1to1.md) | ⬜ 未开始 |
| 05 | [工作区、Execution Epoch 与 Artifact 基础](./05_workspace_epoch_artifacts.md) | 🟨 路径 guard 基础完成 |
| 06 | [Session Runtime 与语义持久化](./06_session_runtime_persistence.md) | ⬜ 未开始 |
| 07 | [Provider、上下文、Compaction 与 Memory](./07_provider_context_memory.md) | ⬜ 未开始 |
| 08 | [Tools、Skills、MCP、Browser 与策略运行时](./08_tools_skills_policy_runtime.md) | ⬜ 未开始 |
| 09 | [Agent Loop、计划、验证与 Subagent](./09_agent_loop_planning_subagents.md) | ⬜ 未开始 |
| 10 | [用户交互、Steer、取消与审批](./10_interaction_control_approval.md) | ⬜ 未开始 |
| 11 | [事件投影、Artifact 展示与审计](./11_event_projection_audit.md) | 🟨 脱敏基础完成 |
| 12 | [恢复、故障接管与副作用对账](./12_recovery_failover_reconciliation.md) | ⬜ 未开始 |
| 13 | [安全、可观测性与性能容量](./13_security_observability_performance.md) | ⬜ 未开始 |
| 14 | [离线发布、部署与供应链验收](./14_offline_release_deployment.md) | 🟨 构建脚手架完成 |
| 15 | [chatsvc 接线、Coordinator 迁移与最终验收](./15_chatsvc_migration_acceptance.md) | ⬜ 未开始 |
