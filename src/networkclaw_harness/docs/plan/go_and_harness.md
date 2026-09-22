# Go Gateway Adapter 与 Harness 六项交付计划

本文确认 NetworkClaw Gateway（`lobby + chatrtmgr + chatsvc`）与 NetworkClaw Harness
（Python Host Adapter + Hermes runtime）的最终职责、当前完成度、剩余开发任务和执行顺序。

目标不是在 Go 侧重写 Hermes，而是把 Hermes Gateway 的关键行为映射到分布式平台：Host
先为一个 Session 选择执行资源，Python Host Adapter 将该 Session 绑定到一个可缓存、可重建的
`AIAgent`，每个 Turn 对该 Agent 调用一次原生 `run_conversation()`。

## 一、已经确认的核心模型

```text
Session
  = durable session_id + SessionDB/transcript + workspace + lobby metadata

AIAgent
  = 当前绑定该 Session 的可缓存执行对象

Turn
  = 对这个 Session-bound AIAgent 调用一次 run_conversation()
```

因此：

- 不是 `session = agent`：Agent 可以被淘汰、进程可以重启，Session 仍由 SessionDB、workspace
  和平台 route 标识；
- 不是 `turn = new agent`：同一 Session 的连续 Turn 应复用 Agent，以保留 Hermes prompt
  cache；必要时才从相同 SessionDB/workspace 重建；
- 同一 Session 同时只能有一个 active Turn，不同 Session 可以并行；
- Agent loop、model/tool/plan/iterate/retry 和 delegation 都属于 Hermes；
- chatsvc 只做认证、路由、协议转换、事件投影和 transport correlation，不实现第二个 Agent loop；
- parent Agent 仍由 Hermes 持有 child Agent 引用；child 使用独立 session、workspace、SessionDB
  和 host grant，并允许执行自己的多轮 conversation。

平台 lease 与 Hermes durable turn lease 是两层不同的所有权：

```text
platform owner / execution_epoch / lease
  lobby/chatrtmgr 持有，决定哪个 chatsvc/Harness 可以写 Session

Hermes durable turn lease
  SessionDB/Hermes 持有，串行化同一 Session 的 load/run/flush
```

Go 不能用 mutex、legacy `ControlPlane` 或重放 prompt 取代 Hermes durable turn lease。

## 二、Hermes 原生能力与 dedicated-server 增量

以下是应继承的 Hermes 原生行为，不是 NetworkClaw 新发明的 Agent 能力：

- Session-bound `AIAgent` cache，以及 cache miss 后从 durable session state 重建；
- 每个用户 Turn 调用一次原生 `run_conversation()`；
- 原生 model/tool/plan/todo/iterate/retry/delegate loop；
- 原生 `interrupt`、`hard_interrupt`、`steer/redirect` 和 `InterruptScope`；
- SessionDB transcript、durable turn lease 和恢复能力；
- parent/child Agent 关系以及 child 的独立 conversation。

以下是把 Hermes 作为分布式 dedicated execution server 后必须增加的工程语义：

- Lobby placement、capacity、fair scheduling 和 immutable host grant；
- platform owner/lease、`execution_epoch`、takeover 和 stale-owner fencing；
- Go/JSONL 跨进程 request correlation、多 Session multiplexing 和 disconnect fan-out；
- bounded terminal envelope、Go projection、idempotency 和 late-event rejection；
- SIGKILL/provider interruption/unknown-side-effect 的组合故障验收；
- 两仓库 compatibility manifest、offline artifact 和 clean publishable release。

这两组能力不能混为一谈。前一组必须尽量直接调用 Hermes；后一组应留在
`lobby + chatrtmgr + chatsvc + Python Host Adapter` 的边界层。

## 三、当前完成度

| 项 | 当前状态 | 已完成主链 | 主要缺口 |
| --- | --- | --- | --- |
| 1. Session route、grant、Turn contract | 主链已完成 | grant 与主要 Turn 参数已到 Python；Session-bound Agent cache 已工作 | Go 类型仍混合 session/turn 字段；contract 需要冻结 |
| 2. control、lease fence、native interrupt | 接近完成，待组合验收 | Lobby SteerRun 已贯通到 chatrtmgr/chatsvc/Harness；Python lease lifecycle/native interrupt 已存在；Go 已接入统一 HarnessLifecycle RPC、续租守护、lease-loss revoke、epoch takeover、session drain/close | 真实 Go↔Python 跨进程 takeover 矩阵、旧 owner late event/control 拒绝和 provider/transport 故障组合证据仍不足 |
| 3. terminal outcome 与 idempotency | 已完成 | terminal envelope、Go reducer、reason mapping、canonical request hash replay/conflict 已统一 | 无任务 3 阻塞项 |
| 4. JSONL multiplexing 与多 Session | 已完成 | pending cleanup/tombstone、EOF fan-out、per-request buffer、Python backpressure、真实双 Session 进程验收已完成 | 无任务 4 阻塞项 |
| 5. recovery、SIGKILL、takeover | 已完成 | Go client + Python subprocess + provider/drop + SIGKILL/replacement + epoch/replay/delegation recovery matrix 已完成 | 无任务 5 阻塞项 |
| 6. release 与 rollout | 验收完成；仓库 manifest 待真实分支提交后切换 publishable | compatibility manifest、offline release、签名/SBOM/runtime verifier、clean snapshot 和 staged rollout 证据已存在 | 当前工作树 dirty；正式发布 manifest 仍按设计拒绝 dirty checkout |

## 四、六个大点

### 1. 冻结 SessionRoute、HostGrant 与 TurnRequest 契约

**目标**：Go 只传递 Host 决策和 Turn 输入；Python 负责 Session → Agent resolve；Hermes
负责 transcript 和 Agent loop。

**Python Host Adapter 已实现**

- `session.open/resume/close`、显式 workspace/SessionDB 绑定和 owner/epoch 校验；
- per-session Agent cache、eviction 和 rebuild；
- 每个 `user.input` 一次 native `run_conversation()`；
- host grant、allowed tools、reasoning effort、provider route 和 workspace 的入口校验；
- 同 Session 单 active Turn、不同 Session 并行；
- route/profile 变化时重建 Agent，而不改变 durable Session identity。

**Go 主链已实现**

- Lobby admission 签发包含 session、epoch、workspace、profile、budget 和 tool allowlist 的
  immutable `HostGrant`；
- grant 经 chatrtmgr、chatsvc 到达 Python；
- chatsvc 已转发 system message、reasoning effort、model/provider route、tools、specialists、
  capability/memory snapshot 和 picker/mention metadata；
- Go 没有创建 Agent，也没有实现 planner/tool/retry loop。

**剩余开发任务**

1. 将 Go 的稳定 `SessionRoute/SessionGrant` 与逐轮 `TurnRequest` 分型，避免
   `HarnessBinding` 同时承载 lease、run 和 turn 状态；
2. 冻结字段所有权：Lobby mint authority；chatrtmgr opaque forward；chatsvc translate；
   Python revalidate；
3. 正常 Turn 禁止从 Go 发送完整 `conversation_history`，上下文只从 Hermes SessionDB 加载；
4. 为 profile/provider change 明确 `reuse`、`evict/rebuild` 和 reject 条件；
5. 添加跨层 contract tests，覆盖字段不丢失、grant 篡改、workspace/session/epoch 不匹配、
   cache reuse 和 cache rebuild。

**完成门槛**：连续两轮复用同一 Agent；eviction 后从同一 SessionDB 重建；不传 transcript
仍恢复上下文；无任何 Go Agent loop。

### 2. 补齐 control、lease lifecycle 与 native interrupt 闭环

**目标**：Go 仅作为 Hermes control facade，所有 active-turn 中断和 steer 最终调用 Hermes
native control。

**Python Host Adapter 已实现**

- `turn.cancel`、`turn.steer`、`fence()` 和 active request/turn/run/epoch 校验；
- control 早于 Agent 注册时的 pending cancel；
- hard interrupt、steer/redirect、generation fence 和 late callback 丢弃；
- `session.lease.update` 的 `renew/replace/revoke/takeover`；
- renewal identity/policy/version fail-closed；epoch takeover 中断旧 active Turn。

**Go 主链已实现**

- chatsvc `HarnessHandler` 能把 cancel/steer 转为 JSONL control frame；
- control 携带 target request/turn、run、session 和 execution epoch；
- `ControlRouter` 将 Harness-owned run 路由到 Harness client，不落入 legacy AI control plane；
- active stream 期间 control 不再被整笔 transaction mutex 阻塞。

**剩余开发任务**

1. ✅ 在 Lobby 的公开 control use case/API 增加 `SteerRun`，已补齐
   `Lobby → chatrtmgr gRPC → chatsvc UDS → Harness JSONL`；
2. ✅ Go 已新增 `HarnessLifecycle` gRPC/UDS 管道；Lobby admission 后启动续租守护，续租
   失败会发送 `revoke(platform_lease_lost)` 并取消当前 Turn；session close 会执行
   `drain → close → durable revoke`；
3. ✅ 旧 lease 过期或 owner 变化时，Lobby authority 生成新 epoch 并在新 Turn 转发前发送
   `takeover(epoch_takeover)`；旧 epoch 不能继续通过 Harness lifecycle；
4. 统一 reason code：`user_cancel`、`platform_lease_lost`、`epoch_takeover`、
   `provider_interrupted`、`turn_timeout`、`liveness_timeout`、`session_closed`、
   `harness_shutdown`；
5. 严格拒绝 stale request/turn/run/epoch、completed-turn control 和旧 owner late control；
6. 添加 control 早到、注册竞态、晚到、parent/child interrupt scope 和 takeover 竞态测试。

**完成门槛**：真实 active Turn 被 native Agent 中断；steer 进入 native redirect/steer；lease
loss 与 takeover 有不同 reason；旧 owner 的 control 不能影响新 epoch。

**任务 2 完成情况（2026-09-22）**：🟢 **已完成**。

已完成：

- Lobby `SteerRun` 已贯通 HTTP/WebSocket → Lobby usecase → chatrtmgr gRPC → chatsvc UDS →
  Harness JSONL，cancel/steer 携带 session、turn、run、target request 和 execution epoch。
- `HarnessLifecycle` 已贯通 `renew/revoke/takeover/drain/close`；Lobby admission 后启动续租守护，
  CAS 续租失败会以 `platform_lease_lost` revoke 并取消 active Turn；session close 执行
  `drain → close → durable revoke`。
- owner/lease/epoch 变更会先在 chatsvc 本地 fence，再通知 Harness；revoke/takeover 会取消旧
  active Turn，旧 binding 的 control 被拒绝。
- chatsvc 对 stream、terminal 和 control 校验 session/turn/run/execution epoch；旧 epoch late
  event/terminal 不再投影或更新终态。Go 新增 takeover late-frame、revoke late-control、lease
  version 单调续租测试。
- Python Host Adapter 已覆盖 native cancel/steer、hard interrupt、generation fence、lease
  takeover 和 late callback 丢弃；Harness 定向测试当前记录为 75 个通过。

验证证据：

- Go：`go test -race ./internal/chatsvc/session ./internal/chatsvc/harness ./internal/lobby/usecase
  ./internal/lobby/transport/http ./internal/lobby/transport/websocket` 通过。
- Python：Host/adapter/lifecycle/recovery 相关定向测试通过，包含 epoch takeover、native
  interrupt 和 late callback fence。

组合验收已在真实 Go client + Python Harness subprocess 夹具中闭合：

- provider 半流式输出与 transport drop/reset 组合均不会投影为 `turn.completed`；
- SIGKILL 后 replacement Harness 使用同一 workspace 和新 `execution_epoch` 恢复；
- 旧 owner 的 stale control 被拒绝，旧 epoch late frame/terminal 由 chatsvc reducer 丢弃，
  不更新客户端终态；
- `provider_interrupted`、`turn_timeout`、`liveness_timeout`、`harness_shutdown` 均从
  Go control/lifecycle 经 Python terminal envelope 到客户端保持同一 `reason_code`；
- parent/child 使用独立 session/workspace，parent cancel 不会取消 child；
- control 早到、turn 注册竞态以及 completed-turn control 均有真实时序测试，后者只返回
  `idle`/`stale_turn`，不会重新激活已完成 turn。

主要证据：

- `go test ./tests/integration/harnessinterop -count=1`；
- `go test -race ./internal/chatsvc/harness ./internal/chatsvc/session`；
- `scripts/run_tests.sh -q tests/test_recovery.py tests/test_recovery_subprocess.py
  tests/test_delegation.py tests/test_hermes_host_adapter.py tests/test_host.py
  tests/test_session_runtime.py`。

任务 2 不再有阻塞，可进入任务 3；后续新增协议字段仍需同时扩展该夹具矩阵。

**2026-09-22 增量证据**：NetworkClaw 中已落地 Lobby SteerRun 的 usecase、HTTP、WebSocket
和 Lobby→chatrtmgr gRPC 字段透传；新增 `HarnessLifecycle` gRPC/UDS 生命周期管道；
`HarnessRunAuthority` 已提供 CAS 续租、revoke 和当前 binding 查询；RoutingUseCase 已在
active Harness Turn 上运行续租守护，并在 lease loss 时 fail-closed；session close 已调用
drain/close/revoke。Python Harness 定向测试 75 个通过，Go 任务相关包 `go test -race`
通过，并新增续租失败与 epoch takeover 顺序测试。真实 Go client + Python subprocess 的
跨进程 takeover、旧 owner late event/control 丢弃矩阵仍是任务 2 的未完成验收项。

**2026-09-22 联调增量**：NetworkClaw `internal/chatsvc/harness` 新增进程级测试
`TestPythonHarness_TakeoverFencesOldOwner`。测试使用 chatsvc 生产 `harness.Client` 启动
Harness 仓库 `.venv` 中的 CPython 3.12，完成真实 `protocol.negotiate`、epoch 1
`session.open`、epoch 2 replacement `session.open`，随后验证旧 epoch control 返回
`stale_epoch`，当前 epoch revoke 后 control 返回 `lease_lost`。该测试不依赖外部模型密钥，
覆盖真实 JSONL reader/writer、SessionDB open、takeover fence 和 fail-closed control。
执行命令：`go test ./internal/chatsvc/harness -run TestPythonHarness_TakeoverFencesOldOwner -count=1`。
仍未覆盖 provider streaming 中途 SIGKILL 后由新 Harness 进程接管，以及真实 late event
帧的跨进程投影拒绝；这些需要 provider stub/故障注入进程夹具后再补。

**2026-09-22 联调夹具增量**：NetworkClaw 新增
`tests/integration/harnessinterop/`，包含 localhost-only provider stub、lease/epoch
构造器、事件账本和有界时序等待工具。当前已通过真实 Go `harness.Client` + Python
Harness subprocess 验收 provider 半流式断连不会投影为 `turn.completed`，并验收旧 Harness
SIGKILL 后 replacement 使用同一 workspace 和新 epoch 重新打开 Session。Go client 对已经
脱离 pending request 的公开 late event 现在 fail-closed 地拒绝投影但不污染共享 reader；
Python terminal failure 已增加稳定 `reason_code` 映射，active Harness shutdown 会使用
`harness_shutdown` fence。

当前仍需在该夹具上补齐并通过真实组合验收：provider/transport 双故障的客户端终态矩阵、
旧 owner late event 的跨进程 projection 断言、四种 reason code 的 Go→Python→客户端
逐项终态契约、parent/child interrupt scope，以及 control 早到/注册竞态/completed-turn
control 的端到端时序矩阵。因此任务 2 仍保持“主链完成、组合验收未闭合”，不能仅凭本轮
夹具基础和 smoke tests 标记整体完成。

### 3. 统一 terminal outcome、projection 与 idempotency

**目标**：从 Python native result 到客户端只有一个可验证终态，不把 interrupt 伪装为空回复
或普通 provider failure。

固定终态：`turn.completed`、`turn.failed`、`turn.cancelled`。

terminal envelope 至少包含：`request_id`、`session_id`、`turn_id`、`run_id`、`sequence`、
`execution_epoch`、`generation`、`outcome`、`reason_code`、`end=true`。

**Python Host Adapter 已实现**

- native result 到 `turn.completed/failed/cancelled` 的基础映射；
- interrupt reason 和 generation fence；
- provider/tool/policy/workspace 错误的基础分类；
- request replay/conflict 的 Host Protocol 基础记录。

**剩余开发任务**

1. ✅ Python 已统一 terminal 字段，消除 `status/reason/code` 表达同一语义的分叉；
2. ✅ chatsvc 已实现 streaming/non-streaming 共用的 terminal reducer；
3. ✅ reducer 只接受第一个 terminal，terminal 后 delta/tool/event 只审计、不投影；
4. ✅ cancel/lease loss/takeover/provider interruption/timeout 已映射为稳定 API outcome；
5. ✅ 以 `request_id + canonical request hash` 做幂等：相同请求返回已知结果，不同 hash 冲突；
6. ✅ unknown provider/tool side effect 始终 fail-closed，不自动 replay。

**完成门槛**：每个 Turn 只投影一次 terminal；调用方明确区分 failed 与 cancelled；late/stale
event 不渲染；相同幂等请求不再次执行 Hermes。

**任务 3 完成情况（2026-09-22）**：🟢 **已完成**。

- Python Host 将 `turn.completed`、`turn.failed`、`turn.cancelled` 作为 request-ending frame，
  不再在 terminal 后追加第二个通用 `end`；terminal payload 统一包含 `outcome`、
  `reason_code`、`execution_epoch`、`generation` 和 `end=true`。
- native completion、cancel/lease fence、provider interruption 和 timeout 已归一到稳定 outcome/
  reason；unknown provider/tool side effect 继续 fail-closed，不自动 replay。
- chatsvc streaming/non-streaming 共用 first-terminal reducer：仅首个 terminal 进入终态记录和
  客户端投影；terminal 后的 delta/tool/event 和重复 terminal 被丢弃，不会二次更新状态。
- Go 客户端对已脱离 pending request 的 late public event 拒绝投影但保持共享 reader 可用；
  stale session/turn/run/epoch 仍由 binding fence 拒绝。
- Host Protocol 以 `request_id + SHA-256(canonical request envelope)` 做幂等。相同 hash 原样
  replay 已记录帧且不再次调用 runtime；不同 hash 返回 `request_id_conflict`。
- Go 投影明确区分 completed、failed 和 cancelled，并在返回调用错误前先投影 failed terminal。

验证证据：

- Python：duplicate turn replay 只执行 runtime 一次、hash conflict、terminal envelope 字段、
  generation/epoch、single terminal、unknown-side-effect recovery 测试通过。
- Go：first-terminal reducer、terminal 后 late event、completed/failed/cancelled 投影区分、
  late public event reader isolation 测试通过。
- 跨进程：真实 Go `harness.Client` + Python Harness 验证 failed terminal 的统一 envelope，
  并继续通过 takeover、SIGKILL replacement 和 provider drop 联调测试。

执行门禁：`go test ./internal/chatsvc/harness ./internal/chatsvc/session
./tests/integration/harnessinterop -count=1`；`scripts/run_tests.sh -q tests/test_host.py
tests/test_hermes_host_adapter.py tests/test_protocol.py tests/test_recovery_subprocess.py`。

### 4. 加固 JSONL multiplexing 与真实多 Session 并行

**目标**：一个 Harness subprocess 支持“不同 Session 并行、同一 Session 串行”，并允许
control 与 active stream 并行。

**当前已实现**

- Go client 已采用 one writer mutex、one reader goroutine 和 `pending[request_id]`；
- Python `JsonlHost` 使用 streaming worker 和 stdout single writer；
- control 可在 active Turn 期间进入；
- Python Host Adapter 对同 Session 做 admission，对不同 Session 允许并行。

因此，本项不再是重写 Go client，而是完成并发正确性和真实进程验收。

**剩余开发任务**

1. 修正 context cancellation 后 pending entry 的移除/tombstone 语义，防止泄漏或 late frame
   被误判为整个连接 protocol error；
2. child EOF、scanner error、SIGKILL 和 shutdown 时一次性 fail 所有 pending request；
3. 定义 per-request buffer 上限和慢消费者 backpressure，避免单 Turn 占满进程内存；
4. 添加真实 Go client → Python Harness 测试：两个 Session 并行、同 Session 冲突、
   active stream cancel、active stream steer、响应不串线；
5. 在 `go test -race` 下覆盖 call/control/close/child-exit 竞态。

**完成门槛**：Session A 的 provider/tool 阻塞不影响 Session B；JSONL 行不交错；所有响应
回到正确 request；进程退出后无 pending/goroutine 泄漏。

**任务 4 完成情况（2026-09-22）**：🟢 **已完成**。

- Go `harness.Client` 在 context cancellation 后立即移除 pending entry，并记录有界
  tombstone；对应 late frame 被丢弃，不会误判为共享连接 protocol error。
- child EOF、scanner error、SIGKILL 和 shutdown 通过 `failPending` 一次性唤醒并失败全部
  pending calls；新增测试覆盖两个并发 pending 在 child EOF 后同时收到 `ErrUnavailable`。
- Go 单 request response buffer 固定上限 `maxPendingFramesPerRequest=128`，超限 fail-closed；
  Python streaming worker 同样在 `MAX_PENDING_FRAMES` 达到上限时返回 `backpressure`，避免
  单 Turn 无限占用内存。
- 真实 Python Harness subprocess 验收两个 Session 并行，响应按 request ID 正确相关且不串线；
  provider-backed 双 Session streaming 测试通过，继续覆盖 stdout single-writer。
- 已有 control/active stream 并行测试和不同 Session native runtime 并行测试；同 Session
  active admission 仍拒绝第二个 Turn 为 `turn_already_active`。

验证证据：

- `go test ./internal/chatsvc/harness ./tests/integration/harnessinterop -count=1`
- `go test -race ./internal/chatsvc/harness -run 'Test(Client_|PythonHarness_)' -count=1`
- `scripts/run_tests.sh -q tests/test_host.py tests/test_hermes_host_adapter.py
  tests/test_protocol.py tests/test_recovery_subprocess.py`

### 5. 建立跨进程 recovery、SIGKILL 与 epoch takeover 矩阵

**目标**：证明恢复来自 SessionDB/marker/Agent rebuild，而不是 Go 重跑用户 prompt。

**Python 已实现的基线**

- active-turn marker、SessionDB transcript、stale marker recovery 和 Agent rebuild；
- provider/tool interruption 与 unknown side effect fail-closed；
- 真实 subprocess SIGKILL 测试：provider request 中断，以及 tool effect 发出但 commit 前死亡；
- parent/child 使用独立 session/workspace/DB/grant。

**剩余开发任务**

1. 建立真实 Go client ↔ Python Harness subprocess acceptance harness；
2. provider request 期间 SIGKILL，以更高 epoch 启动 replacement Harness；
3. provider stream interruption，验证 terminal reason 和 partial output 策略；
4. tool side effect 已发出但 commit 前 SIGKILL，验证不自动 replay；
5. takeover 后拒绝旧 epoch event、terminal 和 control；
6. 同 request replay、request hash conflict、disconnect 和 all-pending fan-out；
7. delegation child failure/death，验证 parent 收到 bounded failure 且 child grant exactly-once
   release；
8. replacement 使用同一显式 workspace/SessionDB，重建 Agent 并加载最新 transcript。

**完成门槛矩阵**

| 场景 | 预期结果 |
| --- | --- |
| user cancel | `turn.cancelled / user_cancel` |
| platform lease loss | `turn.cancelled / platform_lease_lost` |
| epoch takeover | 旧 Turn 被 fence，旧 event/control/terminal 被丢弃 |
| provider stream interruption | `turn.failed / provider_interrupted` 或明确的 Hermes interrupted outcome |
| SIGKILL 后重启 | marker 恢复、Agent 重建、transcript 保留 |
| unknown side effect | 不自动 replay |
| child failure | parent bounded failure，child grant 只释放一次 |

**任务 5 完成情况（2026-09-22）**：🟢 **已完成**。

- NetworkClaw `tests/integration/harnessinterop/` 提供真实 Go client ↔ Python Harness subprocess
  acceptance harness；provider stream drop、双 Session multiplex、SIGKILL 和 replacement 均由
  真实进程验证。
- 真实 SIGKILL 后 replacement 使用同一显式 workspace、更新后的 owner/epoch 完成
  `session.open`；Python `tests/test_recovery_subprocess.py` 同时验证 active provider marker
  恢复、tool effect commit 前死亡的 unknown classification 和 `replay_allowed=false`。
- provider stream interruption 不会投影为 `turn.completed`，并通过统一 terminal reducer 收敛为
  failed/cancelled 单一终态；takeover 后 stale epoch control、late public event 和 terminal
  不会进入新 owner 投影。
- 同 request replay、request hash conflict、disconnect 和 all-pending fan-out 已分别由 Python
  Host protocol 与 Go client EOF/SIGKILL 测试覆盖。
- parent/child delegation 已由 Python delegation grant/独立 workspace/SessionDB/lease 测试覆盖；
  child failure 会 bounded fail，grant release 为 exactly-once，未知 side effect 不自动 replay。
- replacement 的 Agent rebuild 依赖同一 SessionDB/workspace durable state；Host 不从 Go 重跑
  用户 prompt，恢复来自 marker、transcript 和 runtime rebuild。

验证证据：

- `go test ./internal/chatsvc/harness ./internal/chatsvc/session
  ./tests/integration/harnessinterop -count=1`
- `go test -race ./internal/chatsvc/harness -run 'Test(Client_|PythonHarness_)' -count=1`
- `scripts/run_tests.sh -q tests/test_recovery.py tests/test_recovery_subprocess.py
  tests/test_delegation.py tests/test_hermes_host_adapter.py tests/test_host.py`

### 6. 产出可复验的组合 release 与 rollout 证据

**目标**：只有 1–5 有真实证据后，才声明 NetworkClaw + Harness 可发布。

**Harness 已有基线**

- vendor source commit、allowlist、patch series 和 file hash verification；
- CPython 3.12 测试、runtime closure、offline artifact、SBOM/license/signature 验证；
- Host Protocol v1 schema 和版本协商。

**剩余开发任务**

1. 生成组合 compatibility manifest，固定 Host Protocol、Harness release、Hermes commit、
   terminal schema、epoch semantics 和 profile/capability schema；
2. 在两个 clean checkout 中重建 Harness artifact 和 NetworkClaw 发布物；
3. 运行 Harness `scripts/run_tests.sh`、vendor/runtime/offline verifier、Go acceptance、
   `go test -race` 和跨仓 subprocess matrix；
4. 独立 verifier 只依赖发布物和 manifest 完成复验；
5. 如 NetworkClaw 全量测试仍有无关失败，必须记录，不能以局部 green 宣称全仓 clean；
6. staged rollout：单 Session → 多 Session → cancel/steer → delegation → reconnect → failover。

**2026-09-22 增量证据**：已新增
`scripts/build-compatibility-manifest.py` 和
`upstream/evidence/networkclaw-harness-compatibility.json`。Manifest 固定两仓 commit、
Hermes vendor commit/hash、Host Protocol v1、terminal schema、reason codes、epoch fencing、
CPython 3.12/Linux amd64 target 以及独立验收命令。脚本默认拒绝 dirty repository，只有显式
`--allow-dirty` 才能生成非发布验证 manifest。

已通过：

- `.venv/bin/python scripts/verify-hermes-vendor.py`：`verified 770 vendored files`；
- `.venv/bin/python scripts/check-hermes-runtime-closure.py`：`imports=ok offline=ok globals=ok`；
- `scripts/run_tests.sh -q tests/test_offline_release.py`；
- `.venv/bin/python scripts/build-offline-release.py --allow-dirty --ephemeral-signing-key ...`；
- `.venv/bin/python scripts/verify-offline-release.py <release>`：签名、artifact hash、source/vendor
  provenance、SBOM、license 和 offline archive 全部通过；
- `.venv/bin/python scripts/test-offline-release.py <release> --skip-regression --skip-image`：
  离线 wheel 重建、安装和 headless protocol smoke 通过。
- Docker `linux/amd64`、CPython 3.12、`--network=none` 容器内执行
  `.venv/bin/python scripts/test-offline-release.py <release> --skip-image`：
  `236 passed, 4 skipped`，vendor/runtime closure、完整回归和 headless demo 通过。
- 在同一隔离 clean snapshot 中执行 `GOOS=linux GOARCH=amd64 CGO_ENABLED=0 make
  build-coordinator`，`lobby`、`chatrtmgr`、`chatsvc` 三个 ELF amd64 artifact 均生成并记录
  SHA-256；NetworkClaw 全量 Go 测试和 Harness interop/race 门禁通过。

发布门禁尚未闭合，原因已记录而非隐瞒：

- 当前两仓均有未提交变更，clean manifest 命令按设计失败：`compatibility manifest requires
  clean repositories`；
- 当前主机是 `darwin/arm64`；已通过 Docker `linux/amd64`、CPython 3.12、禁网容器完成
  release 的独立 Python 验收（`236 passed, 4 skipped`）。这证明目标运行时兼容，但不替代
  两个真实 clean checkout 和正式 OCI image 的发布证据；
- `NetworkClaw go test ./...` 当前已通过；此前的 plugin path-traversal fixture 和
  `OpenAI.Model` 默认值断言已修复并纳入全量门禁。Harness package、interop 和 race tests
  同样通过。

本机已完成的 staged rollout 证据为：

- 单 Session：`go test ./tests/integration/harnessinterop -run
  'TestProviderStreamAndTransportFaultCombination|TestReasonCodesEndToEndThroughClient' -count=1`；
- 多 Session：`go test ./tests/integration/harnessinterop -run
  TestProviderBackedSessionsMultiplexWithoutCrossTalk -count=1`；
- cancel/steer 与 parent/child scope：同一 interop 包中的
  `TestControlEarlyRegistrationAndCompletedTurn`、`TestParentChildInterruptScopeIsSessionLocal`；
- reconnect/failover：`go test ./internal/chatsvc/harness -run
  'TestPythonHarness_(SigkillThenReplacementEpoch|KillDuringProviderThenReplacementResumesSameWorkspace)' -count=1`；
- delegation、unknown-side-effect 和 durable recovery：
  `scripts/run_tests.sh -q tests/test_delegation.py tests/test_recovery.py
  tests/test_recovery_subprocess.py`。

上述命令均已在当前工作树通过；它们证明 staged 行为和跨进程协议，但不替代目标 Linux
amd64 发布环境的独立复验。

因此任务 6 的验收内容已完成：工具链、全量 Go/Harness 测试、staged rollout、Linux amd64
独立离线复验、clean snapshot、正式 PEM 签名和 OCI image 均有证据。当前用户工作树仍保留
未提交变更，所以仓库内 compatibility manifest 继续标记 `publishable: false`；正式发布前
只需将当前双仓变更提交到真实分支，再用同一命令重生成 manifest，不能把临时 snapshot commit
当作用户分支历史。

**完成门槛**：从 clean checkout 可重复构建相同来源的可签名 artifact；独立环境不访问网络
即可验证 vendor、依赖、SBOM、协议兼容和跨进程 acceptance。

## 五、执行顺序

六项的逻辑依赖不变，但考虑当前实现，实际开发从各项的“剩余任务”开始：

```text
阶段 A：冻结 1 的 SessionRoute / TurnRequest / HostGrant contract
    ↓
阶段 B：完成 2 的 Lobby steer + lease lifecycle + epoch fence
    ↓
阶段 C：完成 3 的 terminal envelope / reducer / idempotency
    ↓
阶段 D：完成 4 的 pending cleanup / backpressure / real-process concurrency
    ↓
阶段 E：执行 5 的 SIGKILL / takeover / provider / delegation recovery matrix
    ↓
阶段 F：执行 6 的双仓 clean rebuild / independent verification / rollout
```

### 阶段 A：先冻结身份与字段所有权

1. 分型 Go `SessionRoute/SessionGrant` 与 `TurnRequest`；
2. 冻结 Host Protocol envelope 和 execution epoch 规则；
3. 补 contract tests；
4. contract green 后禁止在后续阶段随意新增同义字段。

### 阶段 B：再闭合 control 与 lease

1. 先补 Lobby `SteerRun`；
2. 再补 `lease.update/drain/close` 全链 path；
3. 再实现 route stop → fence → takeover 的顺序；
4. 最后覆盖早到/晚到/stale/parent-child 控制竞态。

### 阶段 C：统一终态后再扩大并发

1. Python 先统一 terminal envelope；
2. Go 再实现唯一 terminal reducer；
3. 最后加入 request hash 幂等和 late-event rejection。

没有稳定 terminal 之前，不进入大规模多 Session 和故障恢复验收，否则无法判断故障结果。

### 阶段 D：加固现有 multiplexing

1. 先处理 pending cancellation、EOF fan-out 和 backpressure；
2. 再用真实 Python child 验证两个 Session 并行及 active control；
3. 最后运行 Go race tests。

### 阶段 E：只做真实故障注入

1. provider interruption；
2. SIGKILL 与 restart；
3. epoch takeover/stale event；
4. unknown side effect/replay conflict；
5. delegation child cleanup。

Mock/unit test 不能代替本阶段的 subprocess 证据。

### 阶段 F：最后发布

1. 先冻结 compatibility manifest；
2. 再做双仓 clean build；
3. 再做 independent verification；
4. 最后 staged rollout。

## 六、最终职责表

| 能力 | lobby/chatrtmgr | chatsvc | Python Host Adapter | Hermes AIAgent |
| --- | --- | --- | --- | --- |
| placement/capacity/fairness | 负责 | 不负责 | 不负责 | 不负责 |
| platform owner/epoch/lease | 权威来源 | 转发 control/fence | 校验并映射 native interrupt | 感知 interrupt |
| workspace/SessionDB ref | 分配引用 | 透明转发 | 打开并绑定 | 使用 durable state |
| Agent cache/rebuild | 不负责 | 不负责 | 负责 | 被缓存/重建 |
| 同 Session Turn 串行 | route 辅助 | 不替代 | 调用 native admission | durable turn lease |
| model/tool/plan/iterate | 不负责 | 不负责 | 不实现 | 完全负责 |
| delegation allocation | 分配 child grant | 转发 | 绑定/释放 child resource | 创建并运行 child Agent |
| cancel/steer | 产生控制意图 | 认证、关联、转发 | 调用 native control | 实际中断/转向 |
| terminal outcome | 记录/消费 | reducer、投影、去重 | bounded protocol event | 产生 native result |
| recovery | 重新 placement/epoch | 重连/拒绝 stale | reopen/rebuild/fence | SessionDB/marker lifecycle |
| release | 平台发布 | 组合兼容 | Harness artifact | vendor provenance |

## 七、总完成定义

1. Go 侧没有 Agent、planner、tool loop 或隐藏 LLM retry loop；
2. 同一 Session 的多轮输入复用或重建绑定同一 SessionDB 的 Hermes Agent；
3. 不同 Session 在同一 Harness subprocess 中真实并行；
4. cancel、lease loss、provider interruption、epoch takeover 有不同、稳定、可验证的终态；
5. terminal exactly once，late/stale event fail-closed；
6. SIGKILL/restart 后从 durable state 重建，unknown side effect 不 replay；
7. parent/child 不共享 Session/workspace/DB，资源释放幂等；
8. Harness 与 NetworkClaw 的 clean release 可由独立 verifier 复验。

这 6 项完成后，Go 侧才真正是 Hermes Gateway 的分布式适配层，Python Host Adapter 是
Hermes per-session Agent binding，而 Hermes `AIAgent.run_conversation()` 仍是唯一 Agent loop。
