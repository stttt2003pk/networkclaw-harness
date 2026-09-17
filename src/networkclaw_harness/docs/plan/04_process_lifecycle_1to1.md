# 04 chatsvc 与 Harness 1:1 进程生命周期

目标：实现一个 chatsvc 安全拥有、监控、排水和回收一个专属 Harness 子进程的完整生命周期。

模块状态：⬜ 未开始

前置依赖：03 Host 协议 v1。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 04.1 启动合同

- ⬜ 定义 Harness 可执行入口、命令行参数、环境白名单、工作目录和日志归属。
- ⬜ chatsvc 启动专属 Harness，完成 protocol/capabilities/health 握手后才开放 agent 能力。
- ⬜ 规定 eager start 与首个 AI turn lazy start 的配置和相同行为合同。
- ⬜ 确保同一 chatsvc 不会并发拉起两个有效 Harness owner。

## 04.2 运行监控

- ⬜ 监控 PID、stdin/stdout 管道、heartbeat、退出码和资源用量。
- ⬜ 实现崩溃退避、重启上限、熔断和对上游可见的 degraded 状态。
- ⬜ Harness 崩溃只影响所属 chatsvc，不影响同节点其他 1:1 进程对。

## 04.3 排水与关闭

- ⬜ chatsvc draining 后禁止开启新 turn。
- ⬜ 定义运行 turn 的完成、取消、安全检查点和有界 shutdown 顺序。
- ⬜ 正常关闭等待 Harness 确认，超时后升级为强制终止。

## 04.4 孤儿进程防护

- ⬜ 选择并实现 parent-death signal、进程组、cgroup/container 或等价回收机制。
- ⬜ chatsvc 崩溃、SIGKILL、OOM 和节点终止后均不遗留可继续执行的 Harness。
- ⬜ 旧 Harness 即使短暂存活，也必须因 execution epoch 失效停止写入和副作用。

## 04.5 生命周期 E2E

- ⬜ 两个 chatsvc 必须对应两个不同 Harness PID，且不可交叉通信。
- ⬜ 同一 chatsvc 的多个 session 必须复用同一 Harness PID。
- ⬜ 覆盖启动失败、握手失败、运行崩溃、排水超时和强杀。

## 04.6 模块出口

- ⬜ 1:1 所有权、无孤儿、故障隔离和有界排水全部通过 E2E。
- ⬜ 模块完成后将状态更新为 ✅，后续会话和恢复能力只在该生命周期容器内运行。

