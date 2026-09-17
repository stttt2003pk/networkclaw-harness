# 13 安全、可观测性与性能容量

目标：在完整运行闭环上建立生产级安全边界、诊断能力、资源限制和容量模型。

模块状态：⬜ 未开始

前置依赖：12 已验证的恢复和故障语义。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 13.1 安全边界

- ⬜ 完成 tenant/user/session/workspace/secret 威胁模型。
- ⬜ secrets 只通过受控引用或进程内 provider 使用，不进入协议、日志、artifact 和模型无关上下文。
- ⬜ 网络 egress、文件访问、命令执行、MCP 和 browser 都有 deny-by-default profile。
- ⬜ 生产镜像非 root，最小 capability，依赖和镜像执行漏洞扫描。

## 13.2 审批和权限验证

- ⬜ 验证模型无法伪造 host approval、epoch 或用户身份。
- ⬜ 验证工具 schema/参数变更不会复用旧审批。
- ⬜ 跨 session、跨 chatsvc 和跨 tenant 攻击测试通过。

## 13.3 日志、trace 和 metrics

- ⬜ stderr 输出结构化日志，携带 trace/session/run/invocation 关联但不使用高基数 Prometheus label。
- ⬜ 暴露 health、readiness、协议错误、turn、tool、恢复、队列和资源指标。
- ⬜ 建立诊断快照、告警和 runbook，不将原始敏感输出复制到日志。

## 13.4 资源和容量

- ⬜ 测量一个 Harness 基线内存、冷启动、首 token、并发 session 和 tool/subagent 峰值。
- ⬜ 定义 CPU、内存、文件描述符、子进程、浏览器、并发和 token 预算。
- ⬜ 压测一个 chatsvc 多 session 的公平性和背压。

## 13.5 资源优化

- ⬜ 根据数据评估 lazy start、idle reclaim、懒加载 provider/browser/MCP。
- ⬜ 如需预热，只允许预热后独占租给一个 chatsvc，不变更 1:1 所有权。
- ⬜ 跨 chatsvc 共享 Harness 不作为优化选项，除非另立多租户架构评审。

## 13.6 模块出口

- ⬜ 安全审计、故障诊断、容量和性能基线均形成可重复报告。
- ⬜ 生产阈值、告警和资源 profile 经压测确认后，将状态更新为 ✅。

