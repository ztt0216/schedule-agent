# 素材审核与发布排期 Agent

小影科技 2027 校招 AI Agent 笔试 · **第 1 题**。

这个 Agent 帮内容团队逐个审核待处理素材，给出无冲突、可确认的发布排期，并**只保存草稿，不做真实发布**。

- 模型负责决策：下一步调用哪个工具；遇到风险时选择补查、阻断或请求确认；遇到时段冲突时选哪个替代时段；什么时候结束。
- 代码负责：执行工具、校验硬约束（护栏）、限制步数上限，以及报告里"草稿是否保存成功"等事实字段。这些字段只认存储层的返回结果，不采信模型的说法。

## 环境

- Python ≥ 3.10。运行时**零第三方依赖**，只用标准库。测试需要 pytest。
- 真实模型接入的是任意 OpenAI 兼容、支持 tools（function calling）的接口，例如 DeepSeek、通义千问、OpenAI。
- 离线 `--mock` 模式不需要任何配置。

## 安装

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 运行

```bash
# 离线 Mock：参考实例，18:00 被占用 → 建议 19:00，标记待确认
PYTHONPATH=src python -m schedule_agent run examples/requests/02_slot_conflict.json --mock

# 用户确认 19:00 后进行第二轮：更新同一份草稿，状态变为 confirmed
PYTHONPATH=src python -m schedule_agent run examples/requests/02b_slot_conflict_confirmed.json --mock

# 故障注入：保存连续失败两次
PYTHONPATH=src python -m schedule_agent run examples/requests/02_slot_conflict.json --mock --fail save_draft=2

# 真实模型：先 cp .env.example .env 并填写 LLM_*，然后去掉 --mock
PYTHONPATH=src python -m schedule_agent run examples/requests/05_multi.json
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--fail TOOL=N` | 让某个工具前 N 次调用失败 |
| `--max-steps N` | 步数上限，默认 20 |
| `--schedule FILE` | 替换已有排期文件 |
| `--store FILE` | 草稿存储位置，默认 `.state/drafts.json` |
| `--out FILE` | 把报告和 trace 写成 JSON |

## 测试

```bash
python -m pytest -q          # 29 个用例，覆盖全部验收点和护栏
python scripts/record_examples.py --mode mock   # 重新生成 examples/mock/
python scripts/record_examples.py --mode llm    # 用真实模型生成 examples/llm/
```

## 评审入口

| 验收点 | 测试 | 运行记录（examples/mock/） |
|---|---|---|
| 正常排期（含授权补查） | `test_normal_schedule` | `01_normal` |
| 素材信息缺失 | `test_missing_info` | `03_missing_info` |
| 规则风险 | `test_rule_risk` | `04_rule_risk` |
| 时段冲突（参考实例） | `test_slot_conflict_reference_case`、`test_conflict_then_user_confirms_updates_same_draft` | `02_slot_conflict`、`02b_user_confirmed` |
| 重复提交 | `test_duplicate_submit_single_draft`、`test_resubmit_after_occupancy_changes` | `05a`、`05b`、`05c` |
| 保存失败 | `test_save_failure_reported`、`test_save_failure_then_retry_succeeds` | `06a_save_failure`、`06b_save_retry_ok` |
| 其他工具失败 | `test_query_tool_failure_*` | `07_query_tool_failure` |
| 步数上限 | `test_step_limit` | `08_step_limit` |
| 越权和虚报被护栏拦截 | `tests/test_guardrails.py` | — |

每个场景都有两份文件：`.json` 是完整报告加结构化 trace，`.log` 是可读的逐步记录。

真实模型（DeepSeek）的运行记录放在 `examples/llm/`，覆盖 12 个场景，其中受第三轮修复影响的场景已重新录制；所存记录符合对应预期，未统计重复运行成功率。第一轮暴露问题的记录放在 `examples/llm_round1/`。详见 [docs/03-evaluation.md](docs/03-evaluation.md)。

## 目录

```
src/schedule_agent/
  agent.py   ReAct 循环、finish 护栏、报告生成
  tools.py   工具定义与执行器、save_draft 护栏、故障注入
  llm.py     OpenAI 兼容客户端、离线 MockLLM
  store.py   数据加载、幂等草稿存储
  cli.py     命令行
data/        合成素材、审核规则、已有排期、场景数据
examples/    requests/ 输入；mock/ 与 llm/ 是实际输出和 trace
tests/       验收测试与护栏测试
docs/        01 需求 · 02 方案 · 03 评估 · 04 AI 使用 · 05 复盘
```

## 文档阅读顺序

| 文档 | 重点 |
|---|---|
| [需求](docs/01-requirements.md) | 业务目标、输入示例、状态语义与合成数据覆盖 |
| [方案](docs/02-solution.md) | 工具循环、模型与护栏分工、幂等和存储一致性 |
| [评估](docs/03-evaluation.md) | 预期/实际结果、真实模型迭代、复核步骤与证据边界 |
| [AI 使用说明](docs/04-ai-usage.md) | Claude Code / Codex 辅助范围、个人贡献与凭据处理 |
| [复盘](docs/05-retrospective.md) | 问题根因、工程改进和下一步优先级 |

### 结果应如何判断

- `scheduled` / `confirmed` 表示草稿中的排期状态，不表示真实发布；`published` 始终为 false。
- 保存失败时返回 `save_failed`、`draft.status=failed` 和 `decision=unresolved`；建议保存在 `unsaved_suggestion`，不能当成已保存排期。
- CLI 退出码不是业务成功标志，应检查结构化报告的 `task_status`、每项 `decision` 和 `draft.status`。
- 用户确认及重复提交场景需要共享同一 `--store` 文件。批量记录脚本会重建自己的演示存储并覆盖对应 examples 输出。

当前为合成数据、单进程、本地草稿原型；未实现跨草稿占用预留、独立风险审批、完整输入校验或生产部署。真实模型记录为有限样例，尚无重复实验得到的稳定成功率。详细边界见方案与评估文档。
