# 03 评估

## 测试结果

`python -m pytest -q` → **29 passed**（Mock 策略 + 脚本化模型；离线、确定性）。

## 场景：预期与实际（Mock 模式）

所有文件位于 `examples/mock/`。每个场景有一个 `.json`（报告 + trace）和一个 `.log`（可读记录）。

| 场景 | 输入 | 预期 | 实际 | 文件 |
|---|---|---|---|---|
| 正常排期 | M02 抖音 20:00；M05 小红书 19:00（授权待核验） | 都 scheduled；M05 先补查 | completed；M05 经 `verify_license` 得到 verified 后排期 | `01_normal` |
| 时段冲突 | M01 抖音 18:00（被 X90 占用） | 建议 19:00，待确认，未发布 | awaiting_user，19:00 `pending_confirmation`，published=false | `02_slot_conflict` |
| 用户确认 | 同上，并带 `confirmations: {M01: 19:00}` | scheduled，同一草稿 v2 | completed，`outcome=updated`，version=2 | `02b_user_confirmed` |
| 信息缺失 | M03 缺封面和授权；M02 缺 time；M99 不存在 | 都是 need_info，不写草稿 | 3 步结束，未调用 check_rules 和 save_draft，draft=not_saved | `03_missing_info` |
| 规则风险 | M04 含绝对化用语；M06 授权待核验；M07 商业合作 | M04、M06 阻断，M07 待确认 | M04 因 R01 阻断；M06 补查得到 expired 后阻断；M07 原时段可用，但仍为 `pending_confirmation` | `04_rule_risk` |
| 首次提交 | M01 18:00 和 M02 19:00 同一渠道 | M01 不抢占 M02 的 19:00 | M01 被建议 20:00 待确认，M02 排 19:00 confirmed | `05a_submit` |
| 重复提交 | 同上，同一个存储再跑一次 | 只有一份草稿 | `outcome=duplicate`，存储中只有 1 份 | `05b_duplicate_submit` |
| 更换占用后重复提交 | 18:00 占用撤下 | 动作随之调整，仍只有一份草稿 | M01 改为 18:00 confirmed，同一草稿 v2 | `05c_resubmit_after_slot_freed` |
| 保存失败 | `--fail save_draft=2` | 明确失败，不虚报 | 重试 1 次后放弃；`task_status=save_failed`，`draft.status=failed`，存储无写入 | `06a_save_failure` |
| 保存失败后恢复 | `--fail save_draft=1` | 重试成功 | 第 2 次保存成功 | `06b_save_retry_ok` |
| 工具失败 | `--fail query_slots=2` | 重试后标记未完成 | M01 为 unresolved，task=incomplete | `07_query_tool_failure` |
| 步数上限 | `--max-steps 3` | 明确未完成 | `stop_reason=step_limit(3)`，所有素材为 unresolved | `08_step_limit` |

**"模型根据不同工具结果调整动作"的对照**：

| 对照 | 变化的工具结果 | 动作差异 |
|---|---|---|
| `05a` vs `05c` | 18:00 被占用 / 空闲 | M01 分别选了替代时段 + 请求确认，以及原时段 + confirmed |
| `04` 中的 M06 vs `01` 中的 M05 | 授权补查得到 expired / verified | 一个阻断，一个继续排期 |

## 真实模型运行

模型：DeepSeek `deepseek-chat`，temperature=0，通过 OpenAI 兼容接口调用。运行日期：2026-10-28。
运行命令：`python scripts/record_examples.py --mode llm`。

### 当前真实模型记录（`examples/llm/`）：12 个场景的记录符合预期

| 场景 | 实际结果 | 与预期是否一致 |
|---|---|---|
| `01_normal` | M05 先补查授权（verified）再排期，completed | ✓ |
| `02_slot_conflict` | 19:00 `pending_confirmation`，awaiting_user | ✓ |
| `02b_user_confirmed` | 19:00 confirmed，同一草稿 v2 | ✓ |
| `03_missing_info` | M03、M02、M99 都是 need_info，没有写草稿 | ✓ |
| `04_rule_risk` | M04、M06 阻断（M06 先补查得到 expired），M07 待确认 | ✓ |
| `05a` / `05b` / `05c` | M01 选 20:00，避开了 M02 指定的 19:00；重复提交 `duplicate`；18:00 空出后改排 18:00 confirmed，草稿 v2 | ✓ |
| `06a_save_failure` | 重试 1 次后如实报告 unresolved，`save_failed` | ✓ |
| `06b_save_retry_ok` | 重试成功 | ✓ |
| `07_query_tool_failure` | 重试 1 次后报告 unresolved，incomplete | ✓ |
| `08_step_limit` | `step_limit(3)`，incomplete | ✓ |

真实模型在两组对照中都根据工具结果调整了动作：
- `05a` vs `05c`：同一个请求，时段被占用时选替代时段并请求确认，时段空闲时直接按原时段确认。
- `04` 中的 M06 vs `01` 中的 M05：授权补查结果不同，一个阻断，一个继续排期。

### 第一轮暴露的问题与修复（`examples/llm_round1/`）

第一轮用真实模型跑完 12 个场景，有 4 个出现偏差。其中 2 个是我代码的 bug，Mock 测试没能发现：

| 现象 | 根因 | 修复 |
|---|---|---|
| `06a`：保存两次失败后，模型如实给出 unresolved，护栏却回复"已写入草稿"，模型被误导后第三次保存 | **代码 bug**：finish 护栏把"尝试写入的条目"当成了"实际写入的条目" | 区分 attempted 和 written，只有存储成功的条目才算已写入；新增 `test_failed_save_does_not_count_as_written` |
| `06b`：finish 参数 JSON 括号不匹配，护栏回复"必须给出一个结论"，模型连续 3 次提交同样的错误 | **代码 bug**：解析失败后错误信息不准确 | 返回 `invalid_json` 和明确提示；新增 `test_invalid_json_arguments_get_clear_feedback` |
| `05b`：重复提交被记成 `updated` 而不是 `duplicate`（仍只有一份草稿） | 模型每次写的 note 文字不同，内容哈希跟着变 | 幂等哈希只看排期字段，不看自由文本；新增 `test_note_change_still_counts_as_duplicate` |
| `03`：请求缺少 time，模型自己选了 12:00，并标记待确认 | prompt 没有规定；护栏也没拦 | prompt 第 0 条加上这条规定，并在 save_draft 护栏中强制；新增 `test_request_missing_time_cannot_be_filled_by_model` |

另外，冒烟测试中模型把 `suggested_time` 写成了 "2026-11-03 19:00"，被 finish 护栏拒绝，随后模型自行修正。之后我在 schema 里补上了 `HH:MM` 格式说明。

**结论：** Mock 只能验证"正确的模型"能否走通流程；"不完美的模型"会把护栏反馈的质量问题暴露出来。护栏的报错必须准确且可执行，否则会把模型引向错误的动作。

### 第三轮：代码评审发现的缺口与修复

外部代码评审（基于 commit `818fd12`）指出 3 个缺口，均已复现并修复。修复第 2 个缺口时，新增的测试又发现了 1 个更深的问题：

| 缺口 | 复现 | 修复 | 回归测试 |
|---|---|---|---|
| 保存失败后，护栏仍接受 `decision=scheduled`。报告里同时出现 `save_failed` 和"已排期"，结论自相矛盾 | 保存失败后提交 `scheduled`，护栏返回的 violations 为空 | finish 护栏只依据**实际写入**的条目；保存失败时只能给出 `unresolved`，建议时段放在 `unsaved_suggestion` 字段，不再填 `suggested_slot` | `test_finish_cannot_claim_scheduled_after_save_failure`、`test_save_failure_reported` |
| 真实文件系统异常（如存储目录被同名文件占用）直接抛错，程序中断 | 抛出 `NotADirectoryError` 或 `FileExistsError` | `save_draft` 捕获 `OSError`，作为 `storage_error` 观察返回给模型，最终生成 `save_failed` 报告 | `test_real_storage_error_becomes_failed_report` |
| `save_draft` 不校验时段合法性，`99:99` 也能写入草稿 | 跳过 `query_slots` 直接保存，返回 `ok` | 保存护栏按营业时段（08:00–23:00 整点）校验 | `test_invalid_time_rejected` |
| **新发现**：`DraftStore.save` 先改内存、后写盘。写盘失败后重试，会被误判为 `duplicate` 并返回成功，实际上没有写入（也是虚报保存成功） | 由上面第 2 个测试发现：第二次保存返回"duplicate（未发布）" | 改为先写盘，成功后再更新内存 | 同上 |

修复后重新录制了受影响的场景：Mock 的 `06a`；DeepSeek 的 `01`、`02`、`02b`、`06a`、`06b`。DeepSeek 在 `06a` 中按新规则给出 `unresolved`，报告不再出现矛盾的结论。

### 未验证

- 每个场景只跑了 1 次，没有统计多次运行的决策一致率。
- 没有对比其他模型。

## 失败与限制

1. **Mock 不能证明真实模型的决策效果。** MockLLM 是规则策略，只用来验证流程、护栏和状态。`examples/llm/` 提供单次运行证据；稳定性仍需重复实验验证。
2. **替代时段只在同一天内找。** 跨日期改期属于修改硬性要求，当前只能返回 need_info，让用户提供日期。
3. **时段粒度是整点，每个渠道每个时段只能放一条素材。** 规则也是简化的合成规则：关键词匹配、标签、时长。
4. **草稿存储是本地 JSON，没有并发锁。** 两个进程同时保存同一请求时可能互相覆盖。
5. **护栏只能拦住"不该做的"，不能保证"选得最好"。** 例如模型选了一个合法但不是最近的替代时段，护栏不会拒绝。
6. **重复提交的判定以请求诉求为键。** 同一批素材的诉求一旦改变（例如改了时间），会被视为新请求，生成新草稿。

## 评估证据如何阅读

本项目区分三类证据，避免把流程跑通等同于模型效果：

| 证据 | 可以说明 | 不能说明 |
|---|---|---|
| `tests/` 的自动化测试 | 在已定义输入下，Mock、护栏、幂等与报告逻辑满足断言 | 任意模型、任意输入下均正确 |
| `examples/mock/` 的 12 组记录 | 离线执行可复制，失败注入、确认和重复提交流程可观察 | 真实模型具备相同决策能力 |
| `examples/llm/` 的 DeepSeek 记录 | 所存运行中模型根据观察选择工具，并完成对应场景 | 统计成功率、跨模型泛化或生产稳定性 |

`examples/llm/` 包含第二轮的记录，以及第三轮修复后重新录制的受影响场景，并非所有文件都在同一轮重录。第一轮仅保留四个偏差场景，位于 `examples/llm_round1/`；未保留完整的第一轮 12 场景记录。没有原始接口响应、账单或重复实验统计，模型名称来自运行配置和导出记录。

## 可复制的复核步骤

以下命令从仓库根目录运行。Mock 不需要 API key。

```bash
python -m pytest -q
python scripts/record_examples.py --mode mock
```

批量记录脚本会重建 `.state/examples_mock/` 并覆盖 examples/mock 的输出。同组场景共享草稿存储，因此确认和重复提交的版本号具有可检查的上下文。单独运行确认场景可能产生 v1；这不等于顺序复跑中的 v2 行为失效。

如需保留仓库记录，可使用独立目录执行单个场景：

```bash
PYTHONPATH=src python -m schedule_agent run examples/requests/02_slot_conflict.json --mock --store .state/review/drafts.json --out .state/review/conflict.json
PYTHONPATH=src python -m schedule_agent run examples/requests/02b_slot_conflict_confirmed.json --mock --store .state/review/drafts.json --out .state/review/confirmed.json
```

第一轮应看到 awaiting_user、pending_confirmation、saved、published=false；第二轮应看到 completed、scheduled、updated，两个报告的 draft_id 相同。若这个存储文件之前已有结果，created/updated/duplicate 和版本号会受到已有内容影响。

## 本次独立复核记录

2026-10-28，Codex 对代码版本 `9840f1c` 进行复核：

- 使用独立虚拟环境，Python 3.14、pytest 9.2.0，执行 `python -m pytest -q`，结果为 **29 passed**。
- 重新生成全部 12 个 Mock 场景，均成功运行。
- 核对 Mock 和所存 DeepSeek 的 06a 报告：task_status=save_failed、draft.status=failed、decision=unresolved、suggested_slot=null，未保存建议单独列出。
- 本次复核没有再次调用 DeepSeek；真实模型效果依据仓库已有记录。

12 个脚本场景成功运行表示命令执行完成；业务预期主要由自动化断言和报告核对验证，脚本打印的勾号本身不等于完整验收。CLI 当前对于业务未完成也可以返回退出码 0，自动化集成应读取 JSON 中的 task_status 和 draft.status。

## 后续实验设计（尚未执行）

建议在固定数据和提示词下重复运行每个真实模型场景，记录任务状态正确率、越权确认次数、虚报保存次数、有效工具调用数、护栏拒绝次数，以及耗时与费用。应分别报告首次成功与重试后成功，保留失败记录，避免只选择成功样例。

进一步应新增未覆盖或覆盖不足的场景：多个不同草稿之间的冲突、并发写入、同素材多次发布、独立商务审批、模型返回类型错误、模型网络不可用，以及三个替代候选不足但全天仍有空闲时段。以上属于后续验证计划，没有计入当前通过数量。
