# 02 Hermes 上游基线与 Vendor 供应链

目标：从当前 fork history 中固定的 Hermes 提交提取可追溯、可验证、可离线交付的运行时 snapshot，为所有运行能力提供可信源码基础。

模块状态：🟨 已有脚手架，待完成 H0

前置依赖：01 架构合同与实施基线。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 02.1 仓库和 Python 基线

- ✅ 建立 `src/`、`vendor/hermes/`、`upstream/`、`offline/`、`deploy/` 和 `tests/` 目录结构。
- ✅ 固定 CPython `>=3.12,<3.13`，建立 Poetry 和 wheel 构建基础。
- ✅ 记录初始 Hermes 来源提交 `6005aa1fd9aac8b1024ace50fec8cd1c85a04bae`。
- ✅ 接受当前 `networkclaw-harness/main` 作为 Hermes fork 的开发基线，不另建第二个完整 fork。
- ✅ 确认来源提交是当前 HEAD 的祖先；`hermes-source.json` 以当前 Harness 仓库及固定 commit 记录来源。
- 🟨 将该 commit 作为 H0 原料版本；它表示“从 Hermes 哪个精确源码快照抽取”，最终接受仍以 H0 探针和 Python 3.12 测试为准。
- ⬜ 让同步脚本能从当前仓库的固定祖先 commit 创建临时只读 worktree/archive，无需外部 checkout。
- ⬜ 在固定 Hermes commit 上运行 Python 3.12 基线测试并归档结果。

## 02.2 Runtime allowlist 形成

- ⬜ 对 agent loop、session、provider、memory、compaction、tools、skills、MCP、browser、subagent 做真实 import tracing。
- ⬜ 记录动态 import、entry point、模板、schema、默认配置和资源文件访问。
- ⬜ 运行能力探针，逐项证明 allowlist 不遗漏运行依赖。
- ⬜ 将验证后的路径写入 `upstream/hermes-runtime-files.txt`，禁止凭目录名猜测裁剪。

## 02.3 同步和补丁链

- ✅ 明确 `vendor/hermes/` 是 allowlist 导出和补丁应用后的 runtime snapshot，不是临时抽取目录或通用发布制品目录。
- ✅ 明确临时只读 worktree/archive 与 staging 才是抽取媒介，同步结束后必须销毁。
- ✅ 提供 `sync-hermes-runtime.py` 和来源 commit 校验。
- ✅ 提供 allowlist 复制、patch 应用、post-patch SHA-256 manifest。
- ✅ 完成单文件临时 worktree 的同步与校验烟测。
- ⬜ 建立 patch 编号、说明、上游对应提交和淘汰条件。
- ⬜ 正式 snapshot 生成后移除对 `vendor/hermes/*` 的 Git 忽略，确保 snapshot 与 manifest 在同一提交中受版本追踪。
- ⬜ 对未声明 vendor 修改、缺失文件和额外文件实施 CI 阻断。

## 02.4 许可证和组件清单

- ✅ 保留 Hermes MIT 许可证和第三方 notices 基础文件。
- ⬜ 扫描 vendor 和锁文件中的全部第三方组件。
- ⬜ 生成 CycloneDX SBOM、许可证清单和 notice 聚合结果。
- ⬜ 验证自有 wheel 不会隐藏第三方组件身份。

## 02.5 H0 回归矩阵

- ⬜ 针对固定 Hermes commit 的临时 worktree 和 vendor snapshot 运行相同的 headless 能力矩阵。
- ⬜ 逐项执行 `hermes-capability-matrix.md` 的能力探针并记录保留、适配或缺口结论。
- ⬜ 比较 import、工具注册、资源访问和关键行为差异。
- ⬜ 证明 vendor snapshot 在无 Git history、无 submodule、无公网时可导入和测试。
- ⬜ 归档 SOURCE_COMMIT、allowlist hash、patch hash 和 vendor manifest。

## 02.6 模块出口

- ⬜ 客户源码发布快照同时包含 `src/networkclaw_harness/`、`vendor/hermes/` 和 `upstream/`，且无需同步时使用的临时 worktree/archive。
- ⬜ `verify-hermes-vendor.py` 对正式 snapshot 通过。
- ⬜ H0 能力矩阵无未解释缺口。
- ⬜ Python 3.12、许可证、SBOM 和来源追溯全部通过后，将模块状态更新为 ✅。
