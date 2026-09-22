你的最新理解是对的，关键修正是：

```text
Session = 持久身份、历史、workspace、session metadata
AIAgent = 绑定某个 session 的可缓存执行对象
Turn = 对该 AIAgent 执行一次 run_conversation()
```

所以不能简单写成：

```text
turn = new AIAgent()
session = AIAgent
```

更准确的是：

```text
session -> cached AIAgent
turn -> cached_agent.run_conversation(...)
```

Agent 可以被淘汰并从 SessionDB 重建，但 Session 本身不能因此消失。

## 重新调整后的任务列表

以下顺序按前置依赖排列；同一优先级内，编号越小越应先执行。

### 1. [P0] 冻结三者关系和边界

更新：

- `architecture.md`
- `host-contract.md`
- `project-memory.md`

明确：

- 一个 session 可以连续承载多个顺序 turn。
- 同一 session 同时只能有一个 active turn。
- 每个 turn 调用 Hermes 的 `run_conversation()`。
- Harness 不实现第二套 model/tool/plan/iterate/delegate loop。
- session 是持久事实来源，AIAgent 只是可重建缓存。
- parent/child agent 必须通过独立 child session 关联。

这一步是后续实现的架构基线。

#### 完成记录（2026-09-21）

状态：已完成。

- `architecture.md` 已定义 durable `SessionBinding`、可淘汰 `AIAgent` 缓存、每个 turn
  恰好一次 `run_conversation()`、单 session 单 active turn，以及不实现第二套 agent loop。
- `host-contract.md` 已明确 turn 控制、epoch/lease fencing，以及 child 必须获得独立 session
  identity、workspace、SessionDB 和 `SessionBinding`。
- `project-memory.md` 已固化 Session/AIAgent/Turn 定义、缓存重建语义、持久事实来源和
  parent/child lineage 边界。
- 验证：文档链接检查通过；上述六项架构约束均可在三份基线文档中检索到。

### 2. [P0] 明确 Go `chatsvc` / Go adapter 的职责

Go 侧只负责：

```text
session_id
  -> session owner
  -> execution epoch
  -> workspace
  -> assigned Harness process
  -> transport connection
```

职责包括：

- open/resume session
- 路由输入、取消、steer 和事件帧
- process ownership
- disconnect、drain、replacement
- execution epoch fencing
- 保证同一 session 不被两个 owner 同时执行
- 向 Python Host Adapter 转发 turn 参数和 host grant

Go 侧不得负责：

- 创建或缓存 Python AIAgent
- 实现 Hermes conversation loop
- model/tool retry
- plan/todo/iteration
- delegate 调度逻辑
- session transcript 事实管理

#### 完成记录（2026-09-21）

状态：职责边界已完成；外部 Go 服务中的实现与部署验收仍属于
`implementation-plan.md` 的 Host Adapter integration 工作项。

- `integration.md` 已定义完整的 session 路由链、open/resume、输入与控制帧转发、process
  ownership、disconnect/drain/replacement、epoch CAS fencing、child PID 和 host grant 转发。
- `architecture.md`、`host-contract.md` 和 `project-memory.md` 已同步 Host Adapter 的权威
  所有权与禁止项。
- 明确禁止 Go adapter 创建/缓存 Python `AIAgent`，或接管 conversation loop、model/tool
  retry、plan/todo/iteration、delegation scheduling 和 transcript 事实管理。
- 验证：文档链接检查通过；任务列出的职责和禁止项均可在权威集成文档中检索到。

### 3. [P0] Python Host Adapter 实现 `SessionBinding + AgentCache`

Python 侧需要把当前 session 状态拆开：

```text
SessionBinding
  - session_id
  - workspace
  - SessionDB identity
  - execution epoch
  - active turn state
  - fencing state

AgentCacheEntry
  - AIAgent
  - route/provider/tool signature
  - last-used time
  - generation
```

核心行为：

1. 首个 turn：
   - 验证 host 提供的 session/workspace/grant
   - 创建 SessionBinding
   - 创建 Hermes AIAgent
   - 调用 `run_conversation()`

2. 后续顺序 turn：
   - 找到同一 SessionBinding
   - 复用缓存的 AIAgent
   - 再次调用 `run_conversation()`

3. Agent 被淘汰：
   - 只释放 AIAgent
   - 不删除 session、transcript、workspace 或 todo 状态
   - 下次 turn 从同一 SessionDB/workspace 重建

4. 配置变化：
   - provider、route、tool signature 或 profile 变化时重建 Agent
   - session 历史仍然保持连续

5. session close：
   - 才释放 SessionBinding 的持久资源。

特别需要修正：

- lock 不能覆盖整个 agent execution。
- callback/event handler 必须绑定当前 turn，而不是永久绑定 session。
- 每次 turn 要重置 turn-specific bridge 状态。
- 不依赖 cwd 或全局 Hermes home 推断 workspace。

#### 完成记录（2026-09-21）

状态：已完成。

- `HermesHostAdapter` 已将 durable `SessionBinding` 与可淘汰的 `AIAgent` cache entry 分离；
  binding 保留 workspace、SessionDB/runtime、lease/epoch、turn 状态、fencing 和 child
  resources，淘汰只关闭 Agent，不释放 session 持久资源。
- 首个 turn 创建原生 Hermes Agent，顺序 turn 复用同一 cache entry；`evict_agent()` 后下个
  turn 从同一 SessionDB/workspace 重建，`session.close` 才释放 binding/runtime。
- cache key 已包含 provider route、profile、allowed-tools signature 和 resource-profile
  signature；配置变化会重建 Agent，同时保留 session 历史。
- turn-specific event bridge 在每次 turn 重置并绑定当前 emit/generation；session admission
  使用独立 gate，未覆盖 Hermes model/tool execution。
- 相关行为测试覆盖顺序复用、淘汰重建、route/profile 变化、host grant 签名和 session close。
- 验证：`scripts/run_tests.sh` 通过，`221 passed`；vendor verification、Hermes runtime
  closure 和文档链接检查均通过；`git diff --check` 通过。

### 4. [P0] 把 turn execution 收敛到 Hermes 原生入口

真实执行路径应是：

```text
Go input
  -> resolve SessionBinding
  -> acquire session turn admission
  -> get_or_create AIAgent
  -> AIAgent.run_conversation(...)
  -> relay Hermes events
  -> persist/reconcile
  -> release turn admission
```

Harness 不再新增：

- 自己的 agent while loop
- 自己的 tool loop
- 自己的 iteration/retry loop
- 自己的 delegate loop
- 以 session 为粒度的伪 agent 执行模型。

#### 完成记录（2026-09-21）

状态：已完成。

- `_run_turn()` 的执行链已固定为：解析 session binding、获取 admission、get/create native
  `AIAgent`、调用一次 `run_conversation()`、转发 Hermes 事件、返回唯一 terminal outcome，
  最后释放 admission。
- Harness 没有新增 agent while loop、tool loop、iteration/retry loop 或 delegate loop；模型、
  工具、todo、规划和子代理执行均留给 Hermes 原生 Agent。
- callback bridge 只负责当前 turn 的事件投影与持久化回调，不解释 assistant 文本，也不驱动
  下一轮模型执行。
- 新增行为测试证明同一缓存 Agent 的两个顺序 turn 总共调用两次原生入口，即每个 turn 恰好一次。
- 验证：`scripts/run_tests.sh` 通过，`222 passed`；vendor verification、Hermes runtime
  closure、文档链接检查和 `git diff --check` 均通过。

### 5. [P1] 补齐 session turn admission，但不重复 Hermes agent 能力

需要防止：

- 同一 session 两个 turn 并发执行；
- 旧 epoch 在新 epoch 接管后继续写入；
- cancel/steer 影响错误的 turn；
- stale process 继续发送事件。

这里的 admission 只解决 host/process ownership，不负责 model 或 agent 调度。

推荐使用：

```text
session_id + execution_epoch + turn_id + generation
```

所有输入、事件、完成结果都必须经过 generation/epoch 校验。

#### 完成记录（2026-09-21）

状态：已完成。

- `HermesHostAdapter` 使用 session 级 admission gate，拒绝同一 session 的并发 active turn；
  不同 session 仍可在同一进程中并行。
- active turn 记录 `session_id`（由 binding 固定）、`execution_epoch`、`turn_id`、`run_id`
  和递增 `turn_generation`；cancel/steer 会校验当前 turn/run，stale control 被拒绝。
- epoch takeover/fence 会中断当前 Agent、阻止 stale binding 继续产出，并由当前 turn 只生成
  一个 terminal outcome；旧 epoch 的 late event 会被丢弃。
- native callback sink 现在绑定具体 generation；前一 turn 的迟到 delta/tool/subagent callback
  不能写入后一 turn 的 event bridge。
- admission 只处理 host/process ownership，不实现 model、tool、iteration 或 delegation
  调度。
- 新增行为测试覆盖 native turn generation 的 late callback 丢弃；并发、stale control、epoch
  fencing 和多 session 行为已有测试覆盖。
- 验证：`scripts/run_tests.sh` 通过，`223 passed`；vendor verification、Hermes runtime
  closure、文档链接检查和 `git diff --check` 均通过。

### 6. [P1] 桥接 Hermes 原生 lease 和 interrupt

平台 lease 丢失时：

```text
platform lease lost
  -> fence SessionBinding
  -> interrupt 当前 Hermes AIAgent
  -> run_conversation() 返回 interrupted
  -> 生成唯一 terminal outcome
  -> 禁止旧 epoch 后续写入
```

这里不应再设计一套独立的 Agent lease。

需要确认并测试：

- provider 请求中断；
- tool 执行中断；
- native hard interrupt；
- lease loss 后不会产生两个 terminal event；
- 新 epoch 可以重新绑定并从 SessionDB 恢复；
- 旧 epoch 的迟到事件被丢弃。

#### 完成记录（2026-09-21）

状态：已完成。

- `fence(SessionBinding)` 复用 Hermes 原生 interrupt/hard interrupt；provider 请求、tool 执行
  和 native hard interrupt 均不引入第二套 Agent lease。
- epoch fencing 会阻止 stale generation 继续发布事件，当前 turn 只生成一个 terminal outcome；
  新增测试明确断言只有一个 `turn.cancelled`。
- 新 epoch takeover 由 host 重新 open/resume 同一显式 workspace 和 SessionDB，旧 binding
  先 fence 再 close；旧 epoch 的迟到 callback/event 会被丢弃。
- 验证：`scripts/run_tests.sh` 通过，`224 passed`；vendor verification、Hermes runtime
  closure、文档链接检查和 `git diff --check` 均通过。

### 7. [P1] 重新定义 CapacityLedger/Profile/FairSessionScheduler

这三项不应整体塞进 Harness 的 `JsonlHost`。

推荐职责归属：

| 能力 | 所属 |
|---|---|
| 用户/tenant quota | lobby/platform |
| session placement | chatrtmgr |
| session 路由 | chatsvc/Go adapter |
| process CPU/memory/PID 限制 | deployment/runtime |
| 同一 session turn 串行化 | Go ownership + Hermes turn lease |
| agent token/iteration/delegation budget | Hermes |
| Python 最大并发、最大 payload、超时保护 | Harness hard safety guard |

Harness 只需要接收并验证一个 host grant，例如：

```text
session_id
execution_epoch
workspace
turn_budget
resource_profile
allowed_tools
lease
```

它不应该再次决定用户公平调度或 session placement。

#### 完成记录（2026-09-21）

状态：已完成。

- quota、placement、session routing、process limits 和用户公平性已明确归属外部平台；Hermes
  继续拥有 token/iteration/delegation budget，Harness 只执行本地 hard safety guard。
- `JsonlHost` 只验证并转发 platform `host_grant`，不根据 tenant/user 做 placement、quota
  arbitration 或公平调度。
- `host/scheduler.py` 已明确降级为 bounded protocol admission guard：仅限制本进程输入队列并
  优先处理 control，不拥有用户 quota、tenant fairness、session placement 或 process capacity。
- `runtime/scheduler.py` 已标明仅供注入式行为测试使用，生产 launcher 不选择它。
- 验证：`scripts/run_tests.sh` 通过，`224 passed`；vendor verification、Hermes runtime
  closure、文档链接检查和 `git diff --check` 均通过。

### 8. [P1] 实现 Hermes-native parent/child delegation 接口

parent delegate child 时，Host Adapter 负责承接平台资源：

```text
parent AIAgent
  -> request child host grant
  -> child session
  -> child workspace
  -> child SessionDB
  -> child AIAgent
  -> child run_conversation()
```

要求：

- child 使用独立 session；
- child 使用独立 workspace 和 Agent；
- child 保存 parent lineage；
- parent interrupt 可以传播给 child；
- child 可以在自身 session 内进行 Hermes 支持的多轮 conversation；
- child 完成或失败后释放 host grant；
- parent 不直接操作 child 的 transcript；
- child 的事件必须带 lineage 和 bounded metadata。

#### 完成记录（2026-09-21）

状态：已完成。

- `HostDelegationBroker` 在 child 创建前等待 host grant；grant 必须绑定独立 child session、
  绝对 workspace、child lease 和 bounded budget。
- `_take_delegation_grant()` 为 child 创建独立 `SessionWorkspace`、Hermes SessionDB/runtime、
  `HermesHostToolSession` 和 native child `AIAgent` 所需 mapping；parent 与 child 不共享
  session identity、workspace 或 transcript。
- child mapping 保存 parent session/turn/generation lineage；`_LineageBridge` 为 child 事件添加
  bounded lineage metadata 后再投影到 parent event stream。
- Hermes vendored delegation 负责 parent interrupt 向 active child 的原生传播；Harness 只提供
  grant、lease、workspace 和 resource lifecycle，不新增 delegation loop。
- child 完成、失败、parent close 或 grant cleanup 时释放 child tool/session runtime；child grant
  失败不会修改 parent transcript。
- 新增行为测试验证 child 独立资源、lineage 和 grant release；broker identity/workspace/budget
  校验及 host resolve 流程已有测试覆盖。
- 验证：`scripts/run_tests.sh` 通过，`225 passed`；vendor verification、Hermes runtime
  closure、文档链接检查和 `git diff --check` 均通过。

### 9. [P1] 建立真实集成测试

优先测试行为，而不是实现结构：

1. 首 turn 创建 Agent；
2. 第二个顺序 turn 复用 Agent；
3. Agent eviction 后从 SessionDB 重建；
4. session transcript、todo、workspace 连续；
5. 两个不同 session 并行；
6. 同一 session turn 串行；
7. Go 不会双路由同一 session；
8. Harness 重启后 session 恢复；
9. route/profile 变化触发 Agent 重建；
10. cancel/steer 只影响对应 turn generation；
11. provider interruption 返回唯一 terminal outcome；
12. parent/child session lineage 正确；
13. child failure 不污染 parent session。

#### 完成记录（2026-09-21）

状态：已完成。

行为覆盖矩阵：

| 项目 | 证据 |
|---|---|
| 首 turn 创建 Agent | `test_native_adapter_streams_one_vertical_turn` |
| 顺序 turn 复用 Agent | `test_session_reuses_agent_then_rebuilds_after_eviction`、`test_each_turn_calls_only_the_native_conversation_entry_once` |
| eviction 后重建 | `test_session_reuses_agent_then_rebuilds_after_eviction` |
| transcript/todo/workspace 连续 | `test_todo_completion_is_committed_then_projected_as_plan`、`test_reopening_same_workspace_after_adapter_restart_reuses_session_identity` |
| 两个 session 并行 | `test_different_sessions_can_execute_in_parallel` |
| 同一 session 串行 | `test_same_session_rejects_a_concurrent_turn_before_native_execution` |
| Go/Host 不双路由 session | `test_open_rejects_second_owner_for_active_session`、`test_newer_epoch_takes_over_after_fencing_old_binding` |
| Harness 重启恢复 | `test_reopening_same_workspace_after_adapter_restart_reuses_session_identity`、lifecycle subprocess tests |
| route/profile 重建 | `test_route_change_rebuilds_agent_without_closing_session`、`test_profile_change_rebuilds_agent_cache_entry` |
| cancel/steer generation | `test_controls_reject_stale_run_generation`、`test_late_callback_from_previous_generation_is_dropped` |
| provider interruption 唯一 terminal | `test_epoch_fence_emits_exactly_one_terminal_outcome`、真实 provider cancel E2E |
| parent/child lineage | `test_child_grant_carries_bounded_parent_lineage`、`test_native_delegation_binds_independent_child_resources_and_releases_them` |
| child failure 不污染 parent | `test_failed_child_allocation_does_not_modify_parent_durable_state` |

- 新增四类集成行为测试：跨 session 并行、同 session admission、重启后 workspace/session
  identity 恢复、child failure 隔离 parent durable state。
- 测试保持行为级断言，不读取源码结构作为成功条件；真实 subprocess/provider E2E 继续覆盖
  JSONL vertical flow、workspace tool、cancel 和 steer。
- 验证：`scripts/run_tests.sh` 通过，`229 passed`；vendor verification、Hermes runtime
  closure、文档链接检查和 `git diff --check` 均通过。

### 10. [P2] 补 recovery 和故障语义

使用真实 subprocess 测试：

- provider 请求期间 SIGKILL；
- tool 执行期间 SIGKILL；
- side effect 已发出但 commit 前 SIGKILL；
- stale epoch 被新 epoch takeover；
- agent cache miss；
- active turn marker 清理；
- parent/child 进程死亡；
- event replay/idempotency；
- unknown side effect fail-closed，不自动重放。

这些测试的目的不是证明自建 scheduler，而是证明 adapter 没有破坏 Hermes 原有的恢复、session 和 delegation 语义。

#### 完成记录（2026-09-21）

状态：已完成（本仓库可验证范围）；真实部署中的多进程/平台故障演练仍需在外部平台执行。

故障覆盖矩阵：

| 故障语义 | 证据 |
|---|---|
| provider 请求期间 SIGKILL | `test_sigkill_during_provider_request_clears_active_run_marker` |
| tool 执行期间 SIGKILL | `test_sigkill_after_tool_effect_before_commit_is_unknown_and_not_replayed` |
| side effect 已发出但 commit 前 SIGKILL | 同上；effect marker 存在但 durable result 不存在，恢复为 `UNKNOWN` |
| stale epoch takeover | `test_takeover_a_to_b_to_a_fences_old_recovery_and_reconciliation_writes`、Host takeover tests |
| agent cache miss / restart | `test_reopening_same_workspace_after_adapter_restart_reuses_session_identity` |
| active turn marker 清理 | subprocess recovery tests + `SemanticPersistenceService.recover` tests |
| parent/child 进程死亡 | lifecycle orphan/parent SIGKILL tests、delegation cleanup tests |
| event replay/idempotency | durable event dedupe/conflict tests、duplicate request replay tests |
| unknown side effect fail-closed | `test_unknown_side_effect_queries_actual_state_or_requires_explicit_safe_decision`、
  `test_failure_injection_never_blindly_replays_unknown_effect` |

- 新增真实 subprocess fixture：进程在 provider/run marker 或 tool intent/effect marker 落盘后
  被 `SIGKILL`，replacement 从同一 workspace/SQLite durable store 恢复，不自动重放未知 side effect。
- recovery 只重建事实和安全分类，不从死进程重新调度模型、工具或 delegation；旧 ephemeral handles
  不会进入恢复事实。
- 验证：`scripts/run_tests.sh` 通过，`231 passed`；vendor verification、Hermes runtime
  closure、文档链接检查和 `git diff --check` 均通过。

### 11. [P2] 完成 release/vendor verification

这部分与 Hermes agent 语义无关，仍然是 Harness 的交付门禁：

- vendor source commit 可追溯；
- allowlist、patch series、file hash 一致；
- `__pycache__` 和生成物处理稳定；
- 所有验证统一 CPython 3.12 环境；
- clean checkout 可构建；
- wheelhouse、manifest、SBOM、signature 可复验；
- image digest 和外部签名可独立验证；
- `scripts/run_tests.sh` 能在 clean 环境通过。

#### 完成记录（2026-09-21）

状态：已完成。发布/供应链门禁已完成；目标平台镜像 rebuild 需要在带 Docker daemon 的
Linux/amd64 构建节点执行，当前已完成等价的镜像构建、digest/OCI 校验和目标容器验收。

- vendor provenance 已保留 source commit、allowlist、patch series 和逐文件 hash；
  `scripts/verify-hermes-vendor.py` 通过（`verified 770 vendored files`）。
- validation release 使用 CPython 3.12、临时签名密钥和 dirty-tree 明确标记构建成功；
  manifest、签名、artifact hash、source archive、wheel、SBOM、wheelhouse 和 vendor
  provenance 均可由 `scripts/verify-offline-release.py` 复验。
- 带镜像构建成功，目标为 `linux/amd64`，镜像 digest 为
  `sha256:456ddc07336ea578ff9b2079fadf21e3896510b23caa00beeea7b5480c7bacb1`；OCI archive
  也已纳入 manifest 并通过 hash 校验。
- 修正 release image context：构建 Docker image 时将离线 wheelhouse 注入临时
  `source/offline/wheels`，但不污染签名 source archive；image rebuild 路径同样补入
  wheelhouse。
- `scripts/run_tests.sh` 通过，`232 passed`；vendor verification、Hermes runtime
  closure 和文档链接检查均通过。
- `scripts/test-offline-release.py` 现在会在宿主平台不匹配时提前报告明确诊断，而不会把
  Linux wheelhouse 误报为缺少依赖。本机为 macOS arm64；目标平台 Python acceptance 已通过
  Docker 的 `linux/amd64` CPython 3.12 容器执行。
- 目标平台验收已在 Docker 的 `linux/amd64` CPython 3.12 环境执行：离线依赖安装、wheel/sdist
  可复现性、签名与 manifest 复核、headless owner/child demo，以及完整回归套件均通过；结果为
  `228 passed, 4 skipped`，`verified 770 vendored files`，Hermes runtime closure 通过。
  目标容器内没有 Docker daemon，因此镜像 rebuild 子步骤使用 `--skip-image`；镜像本身已在
  本机 Docker 的 network-isolated build 中构建并由 digest/OCI archive hash 独立验证。

另在临时独立 Git clean checkout 中使用外部 RSA-3072 签名密钥完成 publishable 构建；
`source_dirty=false`、`publishable=true`，manifest/artifact 验证通过，镜像 digest 为
`sha256:d881cbe80e5ebb8b240578263497f98f7f738f4f2e15d42bb37f77234aa7d2f4`。

## 对原来四项的最终结论

1. `CapacityLedger/Profile/FairSessionScheduler`  
   不是 Harness 核心缺失，应下沉到平台侧；Harness 只做 host grant 验证和硬安全限制。

2. active-turn lease loss  
   是必要能力，但实现方式应是桥接 Hermes 原生 lease/interrupt，而不是重新设计一套 Agent 生命周期。

3. subprocess、多 session、SIGKILL、epoch takeover、provider interruption  
   仍然必须补齐，重点是验证 session-bound Agent cache 和 Hermes 原生 turn 语义没有被 adapter 破坏。

4. release/vendor verification  
   仍然必须完成，是独立的供应链与交付问题。

最重要的实现顺序应是：

```text
1. Session/Agent/Turn contract
2. Go session routing
3. Python AgentCache
4. Hermes run_conversation bridge
5. lease/interrupt fencing
6. delegation
7. subprocess recovery tests
8. release verification
```

其中真正的核心不是“让 Harness 自己变成一个更强的 gateway”，而是让它成为一个能够正确承载 Hermes `AIAgent` 的 dedicated execution server。
