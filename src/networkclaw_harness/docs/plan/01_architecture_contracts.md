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
- ✅ 明确 Lobby/session durable 边界持有跨节点 execution lease 权威，chatrtmgr 持有用户亲和、节点容量和本地进程存活真值。

## 01.3 统一术语与身份模型

- ✅ 冻结传输 `sequence` 按 `request_id` 排序；durable history 使用存储 cursor，不混用。
- ✅ 冻结进程级命令不伪造 `session_id`，session 类命令绑定 tenant/user/session。
- ✅ 冻结 H0 fixture 的最小身份集和基本关系：

| 身份 | H0 生成方 | 最小作用域与关系 | H0 用途 |
| --- | --- | --- | --- |
| `tenant_id` | NetworkClaw host/durable 边界 | 跨进程稳定的租户边界 | 隔离 fixture、workspace 和策略 |
| `user_id` | NetworkClaw host/durable 边界 | 在 tenant 内标识授权主体 | 验证用户亲和不替代 session 隔离 |
| `session_id` | Lobby/session durable 边界 | 在 tenant 内稳定；迁移和恢复不改变 | workspace、历史和单 active run 归属 |
| `request_id` | 发起请求的 host | 在 session 协议流内唯一；关联 accepted/end/error | 传输幂等、分片排序和重复输入测试 |
| `run_id` | Harness | 每次 coordinator 执行新建；关联触发它的 request | 验证恢复不冒用旧 run、同 session 不并发执行 |

H0 可以使用固定测试值，但不得从 cwd、workspace 路径或用户文本反推这些身份。完整保留期、
全局唯一性和交互对象关系仍由本模块后续字段级合同冻结。

- 🟨 统一 tenant、user、session、interaction、turn、run、model step、item、invocation、artifact 的定义。
- ⬜ 为每类身份规定生成方、作用域、持久化期限和关联关系。
- ⬜ 规定 `request_id`、`turn_id`、`run_id`、`invocation_id`、`event_id` 的幂等与去重边界。

## 01.4 权威、Workspace 与 Execution Lease

- ✅ 冻结 Lobby/session durable 边界为 session 语义资产、owner、epoch 和 lease 的跨节点权威。
- ✅ 冻结 chatrtmgr 为本地 chatsvc 进程存活与复用裁决的权威；用户亲和只是软路由提示。
- ✅ 冻结 workspace 保存 Hermes checkpoint 和 artifact 内容，不建立第二个业务语义权威。
- ✅ 冻结 checkpoint 必须绑定 durable cursor 与 execution epoch，失配时不得直接恢复执行。
- ⬜ 冻结 owner identity、epoch CAS、lease TTL/续租/宽限期和失联 fail-closed 的字段级合同。
- ⬜ 冻结 artifact 内容、Lobby durable metadata 和关键事件 ACK 的提交顺序。

## 01.5 能力范围和非目标

- ✅ 建立“保留、适配、替换、禁用、后置”能力矩阵，作为 H0 allowlist 和能力探针输入。
- ✅ 冻结 UI、网站、演示资源不进入运行 snapshot。
- ✅ 冻结运行时自进化关闭，skill/tool 变更只能通过 Git 和发布链进入生产。
- ✅ 冻结开发者可在 Harness 源码中开发业务 tool/skill；customer/development profile 均禁止运行时自安装。
- ⬜ 对能力矩阵逐项补齐探针、来源模块和目标 profile。

## 01.6 架构决策记录

- ⬜ 建立 ADR 索引，至少覆盖进程形态、协议版本、工作区所有权、execution epoch、恢复和副作用策略。
- ⬜ 为后续重大变更规定 ADR 更新、兼容评估和回滚要求。
- ⬜ 建立文档链接检查，确保交付仓库文档不依赖缺失的外部相对路径。

## 01.7 开工门槛

开工分为两道门，避免把供应链元数据、H0 探索和主体实现混为一件事。

### H0 Ready

- ✅ 当前 `networkclaw-harness/main` 已接受为 Hermes fork 开发基线，无需新建或确认第二个 fork。
- ✅ 已固定可追溯的 Hermes 来源提交，且确认它是当前 HEAD 的 Git 祖先。
- ✅ 能力矩阵已给出 H0 探针族、可观察结果和目标 profile；具体来源模块在 import/resource tracing 中发现并写入证据，不在开工前猜测。
- ✅ H0 fixture 使用本文件定义的最小身份集。

满足以上条件即可开始 H0。仓库 URL 已由当前 `origin` 和 `hermes-source.json` 记录；它是供应链
元数据，不是重新评估 fork 或阻塞 H0 的产品决策。

### Implementation Ready

03–06 的主体实现开始前必须完成：

- ⬜ 冻结完整身份、父子关系和幂等/去重边界。
- ⬜ 冻结 owner/epoch CAS、lease TTL、续租、宽限期和各状态下的禁止操作。
- ⬜ 冻结 durable ACK、tool intent、外部副作用、result、artifact 和 checkpoint 的写入与恢复顺序。
- ⬜ 建立 Harness ADR 索引，并明确与 NetworkClaw ADR/HLD 的归属和互引方式。

前端断连后的默认行为、provider fallback、具体保留期以及 Browser/MCP 生产开放范围，可在对应
模块入口冻结，不阻塞 H0。

## 01.8 模块出口

- ⬜ 所有术语、身份、职责和非目标无冲突。
- ⬜ 后续模块可仅引用本合同，不重新定义 chatsvc、chatrtmgr 和 Harness 的所有权。
- ⬜ 架构评审明确批准后，将模块状态更新为 ✅。

## 01.9 总体执行路线

下表是模块索引和主要交付顺序，但实施以每个模块声明的显式前置依赖为准，不再使用
“模块 N 必须等待 N-1 全部完成”的隐式规则。合同不得由后置实现反向补齐；在 01 冻结且
03 建立协议骨架后，04 进程生命周期与 05 workspace/epoch 可以并行实现，最终在 12 的
故障接管验收汇合。H0-H6 是跨模块的纵向验收门，不等同于模块编号。

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
