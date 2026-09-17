# 14 离线发布、部署与供应链验收

目标：从同一 Git 提交生成可下载源码、Python 3.12 wheelhouse、SBOM 和运行镜像，并在无公网环境重复构建。

模块状态：🟨 已有构建脚手架

前置依赖：13 已完成生产安全和容量基线。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 14.1 锁文件和 wheelhouse

- ✅ 已有 Python 3.12 Poetry 锁文件和空 runtime requirements 基础。
- ✅ 已有 `build-offline-release.py` 基础入口。
- ⬜ 生成全部 cp312 wheels 和完整 `--require-hashes` requirements.lock。
- ✅ 首期发布目标固定为 Ubuntu 22.04 `linux/amd64`；`linux/arm64` 在独立兼容矩阵通过后再承诺。
- ⬜ 验证 Ubuntu 22.04 `linux/amd64` 上的原生 wheel，不允许客户现场源码编译或联网下载。

## 14.2 源码发布包

- ✅ 明确源码交付对象是整个可构建仓库的发布快照，不是单独的 `src/networkclaw_harness/` 或 `vendor/hermes/`。
- ⬜ 发布包包含 NetworkClaw 源码、vendor snapshot、allowlist、patches、tests、docs、skills/tools 和构建脚本。
- ⬜ 客户构建不依赖完整 Git history、其他仓库、Git submodule、PyPI、客户 Nexus 或运行时下载。
- ⬜ SOURCE_COMMIT、vendor manifest 和文档版本一致。

## 14.3 发布组装与落点

- ⬜ 发布脚本在 `dist/release/<version>/` 建立一次性的本地组装目录；该目录及 `dist/` 下其他构建结果不提交 Git。
- ⬜ 组装目录至少产生完整源码包、离线构建包、Python wheel/sdist、供应链附件和统一 release manifest。
- ⬜ `offline/wheels/` 只保存本次离线构建所需的 cp312 wheelhouse；`vendor/hermes/` 不接受 wheel、SBOM、镜像 archive 或其他发布输出。
- ⬜ 源码包和离线构建包发布到 release artifact store；Python wheel/sdist 发布到受控制品库或作为 release artifact 保存。
- ⬜ OCI 镜像发布到 OCI registry 并以 digest 固定身份；无 registry 的离线交付可额外生成 OCI archive，但 archive 仍放在发布组装目录而不是源码目录。
- ⬜ release manifest 绑定 source commit、vendor source commit、vendor manifest hash、lock hash、各文件 hash、SBOM hash、签名和 image digest。

## 14.4 OCI 镜像

- ✅ 已有 Python 3.12 slim Dockerfile 基础。
- ⬜ 使用离线 wheelhouse 构建非 root、只读根文件系统兼容的镜像。
- ⬜ 镜像包含必要 Ubuntu 22.04 系统工具及固定来源。
- ⬜ 记录 image digest、source commit、protocol version 和 vendor commit。

## 14.5 SBOM、许可证和签名

- ⬜ 生成源码、wheelhouse 和镜像级 SBOM。
- ⬜ 聚合第三方 notices，验证许可证随所有制品交付。
- ⬜ 对 release manifest、源码包、wheelhouse 和镜像签名并提供校验流程。

## 14.6 离线重建验收

- ⬜ 在无公网、空缓存环境仅使用交付包完成安装、测试和镜像构建。
- ⬜ 比对重建制品 manifest 和允许的非确定性字段。
- ⬜ 执行 H0-H4 核心回归及 headless 演示。

## 14.7 模块出口

- ⬜ H5 源码与离线交付闭环通过。
- ⬜ 源码、依赖、SBOM、许可证和镜像身份可互相追溯后，将状态更新为 ✅。
