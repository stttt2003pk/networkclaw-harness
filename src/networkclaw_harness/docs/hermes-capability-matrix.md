# Hermes Runtime 能力范围矩阵

> 状态：首期范围合同。H0 必须通过真实 import/resource tracing 和能力探针验证每一项；
> 本表定义产品取舍，不代表对应能力已经实现或已进入 vendor snapshot。
>
> 日期：2026-09-17。

本项目所说的“保留约 95% Hermes 运行能力”指用户和宿主可用的运行能力覆盖，不按源码
行数计算。每项能力只能属于“保留、适配、替换、禁用、后置”之一；H0 的 allowlist 和
回归矩阵以本表为输入，不得先按目录裁剪、再反推能力范围。

| 能力 | 分类 | 目标阶段 | NetworkClaw Harness 处理方式 | H0/Hx 验证重点 |
| --- | --- | --- | --- | --- |
| Agent loop | 适配 | H1 | 复用 Hermes 循环，Harness 成为唯一 coordinator | 模型、工具结果、继续决策和终止闭环 |
| Provider 与模型流式调用 | 适配 | H1 | 复用 provider runtime，配置和 secret 使用 host 引用 | 流式、超时、重试、用量和 secret 不泄露 |
| Tool call/result 循环 | 适配 | H1 | 复用 Hermes schema 与执行循环，接入策略和审计 | schema、错误回灌、结果截断和取消 |
| Session persistence | 适配 | H1/H4 | Hermes 状态作为 workspace checkpoint；durable 语义事实仍以 Lobby 为权威 | checkpoint 可加载且不会覆盖更新的 durable cursor/epoch |
| Context assembly | 适配 | H1/H2 | 加入 host 配置、会话历史和 artifact 按需引用 | 大资料不整体进入上下文 |
| Context compaction | 保留 | H1/H4 | 复用 Hermes compaction | tool 配对、等待态、引用和 unknown 不丢失 |
| Session memory | 适配 | H1 | 只在当前 session 内加载和投影 | A/B/A 会话隔离和来源保留 |
| 跨 session/user memory | 后置 | H4 后 | 等 tenant/user 授权、检索和保留期合同完成后启用 | 跨租户拒绝、来源和删除语义 |
| Goal、plan、todo、replan | 适配 | H1 | 复用语义并投影为结构化事件 | 简单任务不强制计划，计划不成为第二执行引擎 |
| Tool registry 与 schema | 适配 | H1 | 复用 Hermes registry，增加 manifest/profile/policy | 模型可见集与可执行集一致 |
| 文件与检索工具 | 适配 | H1/H2 | 限定到 session workspace 和显式白名单 | 相对路径、符号链接和跨 session 逃逸 |
| Shell 工具 | 适配 | H1 | 保留但按 profile、命令和资源策略限制 | cwd、环境、超时、输出和副作用分类 |
| Skill runtime | 保留 | H1 | 从发布制品只读发现和加载 | 完整读取、版本来源和 session 隔离 |
| 开发者新增业务 skill/tool | 保留 | 全阶段 | 开发者修改源码、测试并发布新的 Harness 版本 | Git 来源、测试、lock/hash、SBOM 和发布清单 |
| 运行时 skill/tool 自安装或自修改 | 禁用 | 不进入目标 | customer 和 development profile 均不提供安装、创建、修改或发布入口 | agent、host 请求和工具均无法改变运行能力 |
| NetworkClaw 业务工具 | 替换/新增 | H1 起 | Python 工具适配器调用正式 HTTP/gRPC/MCP，不 import NetworkClaw 内部代码 | 权限、幂等、审批、审计和真实后端 E2E |
| Artifact 与索引 | 替换/新增 | H2 | NetworkClaw workspace 服务保存内容，Lobby 保存 durable 元数据和引用 | hash、原子写、引用、配额和恢复 |
| Clarification | 适配 | H3 | Hermes 语义映射到 Host 协议，Runtime 管理等待 | 问题身份、超时、重复和过期回答 |
| Approval | 适配 | H3 | host 授权；模型只能提出请求 | 参数变化、拒绝、过期和重放均 fail closed |
| Steer 与 cancel | 适配 | H3 | 高优先级控制路径进入当前 run | cancel requested/confirmed/unknown 分离 |
| Subagent | 适配 | H3 | 保留委派、并发和收集，增加预算与资源冲突控制 | 深度、并发、归属、取消和冲突结果 |
| MCP runtime | 适配 | H3 | 按 profile 启用并绑定 session 生命周期 | discovery、错误、资源回收和跨 session 隔离 |
| Browser runtime | 适配 | H3 后 | 生产默认关闭，允许受控 profile 启用 | egress、下载 artifact、崩溃隔离和资源上限 |
| Recovery/repair | 适配 | H4 | Hermes 恢复加 durable cursor、owner/epoch 和 unknown 对账 | 故障注入、旧 owner 拒写和不盲目重放 |
| TUI、Desktop、Web UI | 禁用/裁剪 | 不进入目标 | 不进入 runtime snapshot；保留其依赖的底层会话语义 | allowlist 中无 UI 运行依赖 |
| Gateway、消息平台适配 | 后置 | H6 后 | 当前由 chatsvc 作为唯一 host adapter | 不引入第二 host/coordinator 路径 |
| Cron 与长期后台 automation | 后置 | H6 后 | 首期不扩大 run 生命周期和调度权威 | 另立生命周期、租约和恢复设计 |
| Self-evolution | 禁用 | 不进入目标 | 改进建议只可输出 artifact，不能改变源码或 registry | customer/development profile 均不可用 |

## 业务工具开发边界

“运行时自安装禁用”不限制开发者在交付源码中开发业务工具。推荐边界如下：

```text
networkclaw-harness
└── tool schema、Python adapter、policy、结果规整与 artifact 处理
                  |
                  | 正式 HTTP / gRPC / MCP
                  v
NetworkClaw
└── 业务服务、数据访问、身份权限和外部系统集成
```

每个业务工具随源码声明名称、版本、输入输出 schema、副作用等级、幂等与重试能力、审批
要求、网络目的地、资源预算、输出脱敏和 artifact 策略。开发环境允许编辑源码并重启或重建
Harness，但不提供让运行中 agent 或 host 热装任意代码的旁路。新增依赖必须进入锁文件、离线
wheelhouse、许可证和 SBOM。

## H0 能力探针目录

H0 开工前冻结探针要证明的行为，不预先猜测 Hermes 内部模块路径。执行 import/resource tracing
时，为每个探针记录固定来源 commit 上的实际模块、动态加载点、资源文件和 vendor 对应路径；
这些记录是 allowlist 的证据。

| Probe | 覆盖能力 | 最小输入与可观察结果 | 目标 profile |
| --- | --- | --- | --- |
| `H0-P01` | Headless import 与启动 | 无 UI 环境导入 runtime，stdout 无非协议输出，无在线下载 | development、customer |
| `H0-P02` | Agent loop、provider、流式响应 | stub provider 返回文本流；观察 delta、用量和唯一终态 | development |
| `H0-P03` | Tool registry 与调用循环 | 注册只读 fixture tool；schema 可见，调用结果回灌下一 model step | development、customer |
| `H0-P04` | Session、memory、context、compaction | A/B/A session 输入和超限上下文；观察隔离、压缩及 tool 配对保留 | development、customer |
| `H0-P05` | Skill discovery | 从发布内只读目录加载固定 skill；记录来源且 registry 不可运行时修改 | development、customer |
| `H0-P06` | Clarification、approval、steer、cancel | fixture run 进入等待并接收控制；过期或错误身份输入被拒绝 | development |
| `H0-P07` | Subagent、MCP、Browser | 分别验证生命周期、预算和资源回收；不可用 profile 返回显式禁用 | development；customer 按矩阵关闭或限制 |
| `H0-P08` | Persistence 与恢复 | 保存并加载 checkpoint；结果未知的副作用不自动重放 | development、customer |
| `H0-P09` | 禁用与后置能力 | 尝试运行时安装/修改 skill/tool 及启动后置入口；均不可达或 fail closed | development、customer |

每个探针的验收记录至少包含：source commit、Python 版本、实际入口模块、触达文件/资源、profile、
输入 fixture、结构化输出、退出状态和已知缺口。`H0-P01` 至 `H0-P09` 在完整来源 commit 和 vendor
snapshot 上使用同一组 fixture；差异必须被解释后才能接受 allowlist。
