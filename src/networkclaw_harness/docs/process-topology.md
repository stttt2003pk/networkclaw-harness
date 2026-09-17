# Harness 进程形态：chatsvc 与 Harness 1:1

> 状态：已确认的架构决策。
>
> 日期：2026-09-17。
>
> 适用范围：NetworkClaw Headless Harness 与 `chatsvc`、`chatrtmgr` 的进程归属、会话密度、故障域和生命周期。

## 1. 决策

一个 `chatsvc` 只拥有一个专属的 `networkclaw-harness` 子进程；一个 Harness 也只服务它
所属的这个 `chatsvc`。Harness 在该边界内承载 chatsvc 的多个 session，不为每个 session
单独创建进程。

```text
chatrtmgr
├── chatsvc A（用户亲和）
│   └── networkclaw-harness A（专属）
│       ├── session A1
│       └── session A2
└── chatsvc B（用户亲和）
    └── networkclaw-harness B（专属）
        ├── session B1
        └── session B2
```

必须同时满足以下约束：

1. `chatsvc : Harness = 1 : 1`。
2. `Harness : session = 1 : N`，上限与宿主分配的会话容量和资源策略一致。
3. 不允许多个 chatsvc 连接或复用同一个 Harness 进程。
4. 不允许 Harness 在 chatsvc 退出后作为孤儿进程继续接受或执行任务。
5. `chatrtmgr` 仍是 chatsvc 调度、用户亲和、节点容量和 session execution lease 的权威；
   Harness 不升级为 chatrtmgr 内的节点级共享服务。

## 2. 职责和所有权

| 对象 | 职责 |
| --- | --- |
| `chatrtmgr` | 启停、排水、监控和重启 chatsvc；维护用户亲和、节点容量与 session execution lease |
| `chatsvc` | Harness 的进程 owner 和协议 host；启动、监控、排水并回收专属 Harness；转发输入、控制和结构化事件 |
| Harness | chatsvc 内全部 session 的唯一 coordinator/agent 内核；维护每个 session 的模型、上下文、计划、工具、memory、审批和恢复状态 |
| session workspace | 跨进程和节点保存恢复事实与 artifact，不承担进程所有权或分布式锁职责 |

“Harness 是 chatsvc 子进程”描述的是生命周期归属。“Harness 是唯一决策内核”描述的是
执行权归属。这两点不冲突：chatsvc 管进程和协议，Harness 管 agent 决策。

## 3. 通信形态

1:1 边界允许初始版本使用 stdin/stdout JSONL：

```text
chatsvc -> Harness stdin   命令、用户输入、控制、审批结果
chatsvc <- Harness stdout  版本化协议事件
                    stderr 仅 Harness 日志，由 chatsvc 或部署环境采集
```

标准输出必须只包含完整协议帧。即使未来把传输替换为 UDS、protobuf 或其他双向通道，
1:1 所有权也保持不变；传输升级不能顺带把 Harness 变成跨 chatsvc 共享服务。

## 4. 多 session 隔离

同一 Harness 内可以并发承载所属 chatsvc 的多个 session，但以下状态必须以 `session_id`
和宿主下发的身份显式分区：

- 对话历史、prompt cache、compaction 和 memory 引用；
- 当前 turn、计划、取消、steer、澄清和审批等待；
- 工作目录、artifact、索引和恢复检查点；
- 工具调用、subagent、预算、模型用量和事件 sequence；
- execution epoch 和副作用 intent/result 状态。

进程级用户亲和不能替代 session 隔离。任何模块级可变状态、当前工作目录、环境变量或
全局 registry 都不得隐式充当 session 身份。

## 5. 生命周期与故障语义

### 5.1 启动

chatsvc 建立自己的运行配置和用户边界后启动专属 Harness，完成协议版本、能力和健康握手
后才将 agent 能力标记为可用。是否延迟到首个 AI turn 再启动属于资源优化，不改变 1:1
所有权。

### 5.2 排水与关闭

chatsvc 进入 draining 后，不再向 Harness 开启新 turn；正在运行的 turn 按宿主策略完成、
取消或到达安全检查点。随后 chatsvc 发送 shutdown，等待有界时间并回收 Harness。强制
终止 chatsvc 时，必须通过 parent-death signal、进程组、cgroup/container 或等价机制确保
Harness 同步退出。

### 5.3 Harness 崩溃

Harness 崩溃只影响所属 chatsvc。chatsvc 将未完成 turn 标记为中断或结果未知，并可按退避
策略重启 Harness，再从 session workspace 恢复。结果未知的副作用工具不得自动重放。

### 5.4 chatsvc 崩溃或迁移

旧 chatsvc 消失时，其 Harness 必须退出。chatrtmgr 重新建立 chatsvc 所有权后，新 chatsvc
启动新的专属 Harness，并使用新的 execution epoch 从共享工作区恢复。旧 Harness 即使短暂
存活，也必须因 epoch 失效而无法写状态或执行外部副作用。

## 6. 不采用节点级共享 Harness 的原因

多个 chatsvc 共用一个 Harness 会把当前用户亲和边界改造成节点级多租户 Python 运行时，
并额外引入连接注册、跨 chatsvc 事件路由、密钥隔离、公平调度、全局状态治理、节点级故障
域和独立升级协调。它也会使 stdin/stdout JSONL 不再适用，并扩大单次 Harness 崩溃的影响。

共享进程可能节省解释器内存和初始化时间，但这些收益优先通过延迟启动、空闲回收、按
chatsvc 预热的独占 worker、懒加载 browser/MCP 等方式获得。跨 chatsvc 共享不是当前方案的
可互换实现细节；若未来确有压测证据需要采用，必须作为新的多租户架构重新设计和评审。

## 7. 验收不变量

自动化验收至少覆盖：

1. 两个 chatsvc 分别启动两个不同 PID 的 Harness，不能连接到对方实例。
2. 同一 chatsvc 的两个 session 可复用一个 Harness，且状态、工作区、取消和事件不串线。
3. Harness 崩溃不影响同节点其他 chatsvc/Harness 对。
4. chatsvc 正常退出和强制退出后均不存在孤儿 Harness。
5. chatsvc/Harness 重建后可从工作区恢复，未知副作用不自动重放。
6. execution epoch 失效后，旧 Harness 的写入和外部操作被拒绝。

