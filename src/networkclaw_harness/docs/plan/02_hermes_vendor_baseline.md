# 02 Hermes 上游基线与 Vendor 供应链

目标：从固定 Hermes fork 提取可追溯、可验证、可离线交付的运行时 snapshot，为所有运行能力提供可信源码基础。

模块状态：🟨 已有脚手架，待完成 H0

前置依赖：01 架构合同与实施基线。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 02.1 仓库和 Python 基线

- ✅ 建立 `src/`、`vendor/hermes/`、`upstream/`、`offline/`、`deploy/` 和 `tests/` 目录结构。
- ✅ 固定 CPython `>=3.12,<3.13`，建立 Poetry 和 wheel 构建基础。
- ✅ 记录初始 Hermes 来源提交 `6005aa1fd9aac8b1024ace50fec8cd1c85a04bae`。
- ⬜ 在完整 `networkclaw-hermes-fork` 上运行 Python 3.12 基线测试并归档结果。

## 02.2 Runtime allowlist 形成

- ⬜ 对 agent loop、session、provider、memory、compaction、tools、skills、MCP、browser、subagent 做真实 import tracing。
- ⬜ 记录动态 import、entry point、模板、schema、默认配置和资源文件访问。
- ⬜ 运行能力探针，逐项证明 allowlist 不遗漏运行依赖。
- ⬜ 将验证后的路径写入 `upstream/hermes-runtime-files.txt`，禁止凭目录名猜测裁剪。

## 02.3 同步和补丁链

- ✅ 提供 `sync-hermes-runtime.py` 和来源 commit 校验。
- ✅ 提供 allowlist 复制、patch 应用、post-patch SHA-256 manifest。
- ✅ 完成单文件临时 worktree 的同步与校验烟测。
- ⬜ 建立 patch 编号、说明、上游对应提交和淘汰条件。
- ⬜ 对未声明 vendor 修改、缺失文件和额外文件实施 CI 阻断。

## 02.4 许可证和组件清单

- ✅ 保留 Hermes MIT 许可证和第三方 notices 基础文件。
- ⬜ 扫描 vendor 和锁文件中的全部第三方组件。
- ⬜ 生成 CycloneDX SBOM、许可证清单和 notice 聚合结果。
- ⬜ 验证自有 wheel 不会隐藏第三方组件身份。

## 02.5 H0 回归矩阵

- ⬜ 针对完整 fork 和 vendor snapshot 运行相同的 headless 能力矩阵。
- ⬜ 比较 import、工具注册、资源访问和关键行为差异。
- ⬜ 证明 vendor snapshot 在无完整 fork、无 submodule、无公网时可导入和测试。
- ⬜ 归档 SOURCE_COMMIT、allowlist hash、patch hash 和 vendor manifest。

## 02.6 模块出口

- ⬜ `verify-hermes-vendor.py` 对正式 snapshot 通过。
- ⬜ H0 能力矩阵无未解释缺口。
- ⬜ Python 3.12、许可证、SBOM 和来源追溯全部通过后，将模块状态更新为 ✅。

