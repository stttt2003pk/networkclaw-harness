# 15 chatsvc 接线、Coordinator 迁移与最终验收

目标：将 chatsvc 内旧 coordinator 安全替换为 1:1 Harness host adapter，并完成全链路灰度、回滚和客户发布验收。

模块状态：⬜ 未开始

前置依赖：14 可离线交付的完整 Harness 制品；此前 01-14 全部达到模块出口。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 15.1 Host adapter 设计

- ⬜ 在 NetworkClaw 仓库先更新正式协议定义，再生成代码和适配既有链路。
- ⬜ chatsvc 实现 Harness spawn、握手、JSONL/后续传输、事件映射、健康、排水和回收。
- ⬜ chatrtmgr 仍只管理 chatsvc 和 execution lease，不直接共享或调度 Harness。

## 15.2 旧 Coordinator 替换

- ⬜ 用 feature flag 按 session/run 选择旧路径或 Harness 路径。
- ⬜ 验证 Harness 路径后删除或隔离旧 coordinator loop，禁止双执行。
- ⬜ 迁移期间同一 run 只能由一条路径拥有，切换不能复活旧审批或旧工具句柄。

## 15.3 全链路协议映射

- ⬜ chatsvc 将输入、steer、cancel、clarification 和 approval 正确转发。
- ⬜ Harness 事件映射到 chatrtmgr/lobby/客户端，保持 accepted/completed 和终态语义。
- ⬜ 旧客户端对未知非关键事件明确降级，未知控制事件 fail closed。

## 15.4 1:1 生命周期验收

- ⬜ chatrtmgr 启动两个 chatsvc 时产生两个专属 Harness PID。
- ⬜ 同一 chatsvc 多 session 复用一个 Harness 且状态不串线。
- ⬜ chatsvc 正常退出、崩溃、SIGKILL、排水和重启均不遗留孤儿 Harness。
- ⬜ 节点迁移后新 1:1 进程对使用新 epoch 恢复，旧执行者无法写入。

## 15.5 灰度、回滚和观测

- ⬜ 按 tenant/session/run 灰度，比较完成率、失败率、延迟、成本和用户干预次数。
- ⬜ 回滚只停止新 run 进入 Harness 或等待当前 run 明确收口，不通过重启绕过审批。
- ⬜ 已生成语义资产保持版本化可读，回滚不丢 artifact 和审计事实。

## 15.6 H6 和客户验收

- ⬜ Host 协议 v1、模拟 chatsvc、真实 chatsvc 和客户端兼容测试全部通过。
- ⬜ 完成 H0-H6 自动化矩阵和一条端到端演示。
- ⬜ 客户环境仅凭源码包和离线制品部署成功。
- ⬜ 旧 coordinator 完成退役、运维 runbook 和发布说明完成后，将模块状态更新为 ✅。

