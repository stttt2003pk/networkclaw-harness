# 08 Tools、Skills、MCP、Browser 与策略运行时

目标：恢复 Hermes 的主要执行能力，并让每次工具调用都经过明确的权限、路径、网络、预算和副作用策略。

模块状态：⬜ 未开始

前置依赖：07 Provider、上下文、Compaction 与 Memory。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 08.1 Tool registry 和 schema

- ⬜ 接入 vendor Hermes tool registry、schema 校验、超时、取消和结果截断。
- ⬜ 区分核心、profile、服务可达和 session surface 工具。
- ⬜ 禁止为终端/文件已能完成的能力无条件增加核心工具。

## 08.2 Policy engine

- ⬜ 为每个工具声明只读/副作用、可重试、审批、网络、文件和资源策略。
- ⬜ 工具执行前绑定 session、tenant、workspace、epoch 和授权快照。
- ⬜ 未知策略、过期审批、越权路径和不允许目的地全部 fail closed。

## 08.3 Shell 和文件工具

- ⬜ 将 cwd 固定到 session workspace，实施命令、路径和资源限制。
- ⬜ 保存命令摘要、退出状态和大型输出 artifact，不透传密钥或完整敏感输出。
- ⬜ 只读工具明确可重试，写工具结果未知时禁止盲目重放。

## 08.4 Skills

- ⬜ 发现发布制品内置 skills，完整读取并按 session 注入。
- ⬜ skill/tool 源码只读，生产 profile 禁止运行时创建、修改、安装或发布。
- ⬜ skill 版本和来源进入会话语义资产及发布清单。
- ⬜ customer/development profile 均不暴露运行时安装、创建、修改或发布 tool/skill 的入口。
- ⬜ 验证开发者可在 Harness 源码内新增业务 tool/skill，并经测试、lock/hash、SBOM 和新版本发布进入制品。

## 08.5 MCP 和 Browser

- ⬜ 实现 MCP discovery、生命周期、调用错误和资源回收。
- ⬜ browser 按 profile 启用，下载进入 artifact，网络目的地经过策略。
- ⬜ MCP/browser 崩溃不污染其他 session，关闭 session 时释放专属句柄。

## 08.6 NetworkClaw tools

- ⬜ 定义内部 API/RPC 工具边界，不直接 import lobby/chatrtmgr 业务代码。
- ⬜ 业务工具拆分为 Harness 内 schema/adapter/policy 与 NetworkClaw 内正式业务 API，禁止直接读业务数据库。
- ⬜ 工具凭据由 host 或 secret provider 提供，不进入模型上下文。
- ⬜ 为业务工具提供权限、幂等、审计和真实后端 E2E。

## 08.7 模块出口

- ⬜ 工具能力矩阵、策略拒绝、审批要求、取消和大型结果路径通过测试。
- ⬜ 生产 profile 确认自进化关闭且无运行时下载后，将状态更新为 ✅。
