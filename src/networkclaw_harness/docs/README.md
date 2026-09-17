# NetworkClaw Harness 文档

本目录随 `networkclaw_harness` 源码包、wheel 和离线交付制品一起发布，是当前仓库的
架构事实来源。

文档关系是演进而非并列：

```text
Coordinator 语义运行时（前序语义设计、原设想内置于 chatsvc）
    -> Hermes Headless Harness（当前目标、从 chatsvc 分离出的 coordinator/agent 内核）
```

- [Hermes Headless Harness 内核设计](./hermes-headless-harness.md)：当前架构事实来源。
- [Harness 进程形态：chatsvc 与 Harness 1:1](./process-topology.md)：已确认的进程归属、
  多 session 复用、故障域和生命周期决策。
- [Hermes Runtime 能力范围矩阵](./hermes-capability-matrix.md)：首期保留、适配、替换、
  禁用和后置能力，以及开发者业务工具边界。
- [Coordinator 语义运行时](./coordinator-semantic-runtime.md)：保留的前序语义设计和演进背景；其中 coordinator 留在 chatsvc 的部署结论已失效。

开发实施计划位于 [`plan/`](./plan/)。编号表达主要交付顺序，实际实施遵循各模块声明的
显式依赖；04 进程生命周期与 05 workspace/epoch 可在 01 合同和 03 协议骨架后并行，
不得让后置实现反向补齐前置合同。计划状态使用 ✅ 已完成、🟨 待确认或已有基础但未验收、
⬜ 未开始。

修改 Harness 的协议、工作区、恢复、Hermes vendor、离线交付或 chatsvc 边界时，应在
同一变更中更新这里的对应设计说明。

目录与发布边界的快速约定：`src/networkclaw_harness/` 保存 NetworkClaw 自有工程源码；
`vendor/hermes/` 保存由固定 Hermes 提交生成并校验的 runtime snapshot，它是抽取结果而非
临时媒介；`upstream/` 保存来源、allowlist、patch 和 hash 证据。客户源码交付对象是包含
这些目录以及测试、脚本、锁文件和许可证的完整仓库发布快照。wheel、wheelhouse、SBOM、
release manifest 和 OCI 镜像是从同一 Git 提交生成的发布制品，不进入 `vendor/hermes/`；
详细路径和发布位置见 [内核设计的源码交付章节](./hermes-headless-harness.md)。
