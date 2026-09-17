# 07 Provider、上下文、Compaction 与 Memory

目标：接入 Hermes 成熟的模型和上下文能力，同时保持每个 session 的 prompt cache 稳定、预算可控和租户数据隔离。

模块状态：⬜ 未开始

前置依赖：06 Session Runtime 与语义持久化。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 07.1 Provider runtime

- ⬜ 从 host 接收 provider/model/config 引用，不把密钥写入协议事件或工作区。
- ⬜ 适配流式响应、重试、限流、超时、fallback 和用量统计。
- ⬜ provider client 生命周期按 chatsvc/Harness 所有权复用，session 配置不得串线。

## 07.2 Prompt assembly

- ⬜ 组合稳定系统提示、会话历史、上下文文件、附件、skill 和按需 artifact 片段。
- ⬜ 系统提示在会话生命周期内保持 byte-stable，除 context compression 外不破坏缓存前缀。
- ⬜ 中途安装 skill/tool 或变更工具集默认延后到新 session，避免 prompt cache 失效。

## 07.3 Context budgeting

- ⬜ 计算模型上下文、附件、工具 schema、计划和输出预算。
- ⬜ 对大型资料使用摘要、索引、路径和按需读取。
- ⬜ 预算不足时优先压缩或请求选择，不静默截断关键事实。

## 07.4 Compaction

- ⬜ 复用 Hermes compaction，保留工具配对、未完成交互和来源引用。
- ⬜ 压缩前后恢复行为一致，不能把 unknown 结果压成 succeeded。
- ⬜ 记录 compaction 版本、输入范围和摘要 artifact。

## 07.5 Memory

- ⬜ 区分 session memory、用户偏好和跨会话检索范围。
- ⬜ 所有检索受 tenant/user policy 约束并保留来源。
- ⬜ memory 可以产出改进建议，但不得写 skill/tool 源码或安装依赖。

## 07.6 模块出口

- ⬜ provider、cache、compaction、memory 在多 session A→B→A 测试中保持隔离。
- ⬜ 长会话压缩后可以继续工具循环和恢复，达到验收后将状态更新为 ✅。

