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

真实模型（DeepSeek）的运行记录放在 `examples/llm/`，12 个场景全部符合预期。第一轮暴露问题的记录放在 `examples/llm_round1/`。详见 [docs/03-evaluation.md](docs/03-evaluation.md)。

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
