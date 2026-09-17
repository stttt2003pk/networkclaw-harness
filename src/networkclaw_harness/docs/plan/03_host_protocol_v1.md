# 03 Headless Host 协议 v1

目标：建立 chatsvc 与其专属 Harness 之间稳定、可版本协商、可恢复和可测试的双向协议合同。

模块状态：🟨 已有最小 JSONL 基础，待冻结 v1

前置依赖：01 架构合同；02 可导入的 Hermes vendor 基线。

状态图例：✅ 已完成；🟨 待确认或已有基础但未验收；⬜ 未开始。

## 03.1 协议信封基础

- ✅ 建立 `protocol_version`、`type`、`request_id`、`session_id`、`turn_id`、`sequence`、`occurred_at` 和 `payload` 基础信封。
- ✅ 实现输入校验、未知版本拒绝、单调输出 sequence 和 JSONL 单帧输出。
- ✅ 区分 `request.accepted` 与后续完成或失败事件。
- ⬜ 增加 `run_id`、`event_id`、`invocation_id`、`parent_item_id` 等已冻结身份字段。

## 03.2 命令和事件目录

- ⬜ 冻结 session、user control、clarification、approval、health、capabilities、shutdown 命令。
- ⬜ 冻结 assistant、plan、tool、subagent、artifact、warning、error、heartbeat 和终态事件。
- ⬜ 为每个类型规定必填字段、合法状态转换、幂等规则和错误码。
- ⬜ 将控制类未知语义设为 fail closed，展示类未知语义允许显式降级。

## 03.3 流式和背压

- ⬜ 定义 assistant delta、tool progress 和大型结果摘要的分片规则。
- ⬜ 定义 stdout 单写者、帧大小、队列上限、背压、超时和慢消费者行为。
- ⬜ 规定文本增量可聚合、关键终态不可静默丢失的可靠性等级。
- ⬜ 验证 stderr 日志永不污染 stdout 协议帧。

## 03.4 版本协商和兼容

- ⬜ 定义启动握手、支持版本集合、能力声明和不兼容退出码。
- ⬜ 建立向后兼容字段添加规则和破坏性版本升级流程。
- ⬜ 提供协议 golden cases、跨版本 fixtures 和 host 模拟器。

## 03.5 协议测试

- ✅ 已有健康查询、session open、user input 明确失败和 shutdown 的最小行为测试。
- ⬜ 覆盖 malformed JSON、非法关联 ID、重复命令、乱序和迟到事件。
- ⬜ 覆盖并发多 session 下 sequence、关联和隔离。
- ⬜ 覆盖 chatsvc 断管、Harness 断管和半写帧。

## 03.6 模块出口

- ⬜ Host 协议 v1 schema、错误目录和兼容策略完成评审。
- ⬜ 模拟 chatsvc 可独立驱动全部命令和事件路径。
- ⬜ 协议冻结后将模块状态更新为 ✅，后续模块不得绕开协议直接耦合 chatsvc 内部代码。

