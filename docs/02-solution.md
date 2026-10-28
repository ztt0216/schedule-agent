# 02 方案

## 调用流程

```
请求 ──► LLM 选择动作（tool call + reason）
          │
          ▼
     执行器 ToolExecutor ── 故障注入 / 护栏校验 ──► 观察结果（JSON）
          │                                       │
          └────────── 写回消息历史 ◄──────────────┘
          │
   重复以上过程，直到 finish 被护栏接受，或达到 max_steps
          │
          ▼
   build_report：事实字段（草稿状态、published、审核状态）由代码根据执行记录生成
```

工具共 6 个：

| 工具 | 作用 |
|---|---|
| `read_material` | 读取素材元数据，返回缺失的必填字段 |
| `check_rules` | 按规则检查，返回风险等级 block / verify / confirm |
| `verify_license` | 补查授权状态 |
| `query_slots` | 查询时段是否可用，返回同日最近的 3 个替代时段 |
| `save_draft` | 保存草稿 |
| `finish` | 提交每个素材的结论 |

**工具集中没有发布能力**，从结构上排除了越权发布。

## 动态决策点

| 观察到的结果 | 模型的下一步 |
|---|---|
| `missing_fields` 非空，或素材不存在 | 停止处理该素材 → `need_info` |
| 风险 `block` | 阻断 |
| 风险 `verify` | **补查** `verify_license`：expired → 阻断；verified → 继续 |
| 风险 `confirm` | 原时段可用也只能标记待确认 |
| 时段被占用 | 从替代时段中选一个，避开同批其他素材指定的时段 → **请求确认** |
| 请求里带 `confirmations` | 直接查询已确认的时段，可用则标记 confirmed |
| `tool_unavailable` / `storage_unavailable` | 重试一次；仍失败则标记 `unresolved` 或如实报告保存失败 |
| 护栏返回 `validation_failed` / `finish_rejected` | 按 violations 修正后重新提交 |

**两条代表性路径**，同一个素材 M01、同一个请求、只换已有排期：
- 18:00 被占用：`query_slots` → 选 19:00 → `pending_confirmation`。
- 18:00 空闲：`query_slots` → 原时段 → `confirmed`。

见 `examples/mock/05a_submit` 与 `05c_resubmit_after_slot_freed`。

## 模型与代码的分工（核心取舍）

题目规定"仅有固定条件分支不算 ReAct"，同时又要求"不得越权、不得虚报"。我的做法是：

- **模型决定动作**：调用顺序、是否补查、选哪个替代时段、何时请求确认或结束，全部由模型根据观察结果决定，代码里没有固定的执行顺序。
- **代码只做约束校验，不替模型做决定**：
  - `save_draft` 护栏会拒绝以下情况，并把原因作为观察结果返回给模型：
    - 素材没读过或没做规则检查
    - 有阻断风险
    - 授权没补查
    - 改了渠道或日期
    - 时段已被占用，或同一草稿内重复
    - 未经确认就标记 confirmed
  - `finish` 护栏会拒绝以下情况：
    - 结论与草稿不一致
    - 没有依据的阻断
    - 遗漏素材
- **事实字段不交给模型**：`draft.status` 来自存储层的返回值，`published` 恒为 false，`review_status` 来自执行器记录的观察。即使模型声称"已保存"，报告也不会采信。

`tests/test_guardrails.py::test_loop_feeds_violation_back_and_model_corrects` 演示了这个闭环：模型先越权确认、再虚报 scheduled，两次都被拒，之后修正为 pending_confirmation。

## 幂等与确认边界

- **幂等键**：`draft_id = hash(素材, 渠道, 日期, 时间)`，不包含 `confirmations`。
  - 内容相同时重复保存 → `duplicate`，不写入。
  - 第二轮确认或排期变化后内容不同 → 同一个 `draft_id` 的版本号 +1。
  - 因此"相同请求只有一份草稿"。
- **确认边界**：`confirmed` 只能出现在两种情况：
  - 用户原始时段，且没有 confirm 级风险；
  - `confirmations` 中用户明确确认过的时段。

## 技术取舍

| 选择 | 理由 |
|---|---|
| 原生实现，不用框架 | 循环核心约 60 行。护栏要插在"工具执行前"和"结束前"两个位置，自己写比在框架回调里改更直接，也方便评审逐行核对 |
| 只依赖标准库，用 urllib 调用 OpenAI 兼容接口 | 离线即可运行测试；切换 DeepSeek、千问、OpenAI 只需改 `.env` |
| 每个工具带 `reason` 参数 | 满足"简要决策依据"，同时不需要提交模型内部的思维过程 |
| MockLLM 只读消息历史 | 和真实模型的信息边界一致，不偷看执行器状态，Mock 跑通的流程对真实模型同样成立 |
| 步数上限按工具调用计（含 finish 和被拒的调用） | 防止模型在护栏拒绝和重试之间无限循环 |
| 规则命中、时段计算用确定性代码 | 这些是事实查询，交给模型反而会引入不确定性 |
