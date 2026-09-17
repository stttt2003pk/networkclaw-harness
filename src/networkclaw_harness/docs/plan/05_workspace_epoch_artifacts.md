# 05 工作区、Execution Epoch 与 Artifact 基础

目标：建立会话可迁移、可审计且无法路径逃逸的持久工作区，并用 execution epoch 防止旧执行者继续写入。

模块状态：🟨 已有目录与路径 guard 基础

前置依赖：04 稳定的 1:1 进程生命周期。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 05.1 工作区绑定

- ✅ 要求 host 显式传入绝对 workspace root，不从 cwd 或用户输入推断。
- ✅ 创建 `session-state`、`artifacts/*`、`summaries`、`indexes` 和 `tmp` 基础布局。
- ✅ 拒绝绝对子路径、`..` 和符号链接逃逸。
- ⬜ 加入 tenant/session 所有权元数据和目录格式版本。

## 05.2 Execution epoch

- ⬜ 定义 epoch 的签发方、存储方、续期、失效和比较规则。
- ⬜ 所有状态写入和外部副作用前验证当前 epoch。
- ⬜ 防止旧进程、迟到 callback 和子线程绕过 epoch 检查。
- ⬜ 设计 A→B→A 节点切换和旧 owner 拒写测试。

## 05.3 Artifact 服务

- ⬜ 定义 raw、normalized、evidence、generated artifact 元数据和内容寻址策略。
- ⬜ 支持原子写、校验 hash、大小/MIME、来源、引用和生命周期。
- ⬜ 大型工具输出只保存 artifact，模型接收摘要、索引和必要片段。
- ⬜ `tmp/` 不得保存恢复所需的唯一事实。

## 05.4 索引和摘要

- ⬜ 建立 artifact、字段、引用、来源和检索索引。
- ⬜ 定义摘要生成、失效、重建和来源追溯。
- ⬜ 支持按需局部读取，不把整个资料集塞入模型上下文。

## 05.5 配额和清理

- ⬜ 按 tenant/session/profile 定义磁盘、文件数、单文件和 tmp 配额。
- ⬜ 清理只能删除可重建或明确过期内容，不得破坏恢复事实。
- ⬜ 配额不足时产生结构化错误和可操作诊断。

## 05.6 模块出口

- ⬜ 路径逃逸、符号链接、并发写、epoch 失效和配额测试通过。
- ⬜ 大资料写入、索引、局部查询和 artifact 引用 E2E 通过后，将状态更新为 ✅。

