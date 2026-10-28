"""ReAct 循环：模型选择动作 → 执行工具 → 观察结果 → 模型决定下一步或结束。

职责划分：
- 模型：决定下一步调用哪个工具、选哪个替代时段、何时请求确认/阻断/结束；
- 代码：执行工具、硬约束校验（save_draft / finish 护栏）、步数上限、
  以及最终报告中"草稿是否保存成功""是否已发布"等事实字段（只采信存储层结果）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .tools import TOOL_SCHEMAS, ToolExecutor

SYSTEM_PROMPT = """你是内容团队的"素材审核与发布排期"助手。你只能保存排期草稿，不能发布。

工作方式：每次调用一个工具，根据返回结果决定下一步。对请求中的每个素材：
0. 请求本身缺少 channel / date / time 时 → need_info，请用户补充；不要代替用户挑选时段。
1. read_material 读取元数据；有缺失必填字段或素材不存在 → need_info，列出需要补充的内容。
2. check_rules 检查规则：level=block → blocked；level=verify → 先调用 followup_tool 补查，
   补查结果为 expired → blocked，verified → 继续；level=confirm → 只能 pending_confirmation。
3. query_slots 查询用户要求的时段：可用则按原时段；被占用则从 alternatives 中选一个替代时段，
   且必须标记 pending_confirmation 并说明原因——未经用户同意不得把变更后的时段当作已确定。
   若 request.confirmations 里有用户已确认的时段，先查询该时段，可用则可标记 confirmed。
   同一草稿内不同素材不能占用同一时段。
4. 工具返回 tool_unavailable 时可以重试一次；仍失败则把相关素材标记为 unresolved 并说明。
5. 所有可排期素材（confirmed 或 pending_confirmation）一次性用 save_draft 保存。
   save_draft 返回 storage_unavailable / storage_error 时可重试一次；仍失败则相关素材标记 unresolved，如实说明未保存，
   不能给出 scheduled / pending_confirmation，也不能声称已保存或已排期。
   返回 validation_failed 时按 violations 修正后再保存。
6. 最后调用 finish 提交每个素材的结论。不要声称任何内容"已发布"。
每次调用都要在 reason 参数里用一句话写明决策依据。"""


@dataclass
class TraceStep:
    step: int
    action: str
    args: dict
    reason: str
    observation: str
    next_action: str = ""


@dataclass
class RunResult:
    report: dict
    trace: list[TraceStep] = field(default_factory=list)

    def trace_dicts(self) -> list[dict]:
        return [s.__dict__ for s in self.trace]


def summarize(name: str, obs: dict) -> str:
    if not obs.get("ok"):
        extra = obs.get("violations") or obs.get("message") or ""
        return f'失败 {obs.get("error")}: {extra}'
    if name == "read_material":
        mf = obs["missing_fields"]
        return f'读取成功，缺失字段: {mf}' if mf else "读取成功，必填字段齐全"
    if name == "check_rules":
        if obs["passed"]:
            return "规则检查通过"
        return "风险: " + "; ".join(f'{r["rule_id"]}[{r["level"]}] {r["reason"]}' for r in obs["risks"])
    if name == "verify_license":
        return f'授权 {obs["license_id"]}: {obs["status"]}（{obs.get("note", "")}）'
    if name == "query_slots":
        r = obs["requested"]
        state = "可用" if r["available"] else f'已被 {r["occupied_by"]} 占用'
        return f'{obs["channel"]} {obs["date"]} {r["time"]} {state}；替代: {obs["alternatives"]}'
    if name == "save_draft":
        return f'草稿 {obs["draft_id"]} v{obs["version"]} {obs["outcome"]}（未发布）'
    if name == "finish":
        return "已接受"
    return json.dumps(obs, ensure_ascii=False)[:200]


def validate_finish(ex: ToolExecutor, items: list[dict]) -> list[str]:
    """finish 护栏：结论必须与已观察到的事实、已写入的草稿一致。"""
    v: list[str] = []
    ids = [i.get("material_id") for i in items]
    for mid in ex.req_items:
        if ids.count(mid) != 1:
            v.append(f"{mid}: 必须且只能给出一个结论")
    written = _written_entries(ex)
    for it in items:
        mid, d = it.get("material_id"), it.get("decision")
        f = ex.facts.get(mid)
        if f is None:
            v.append(f"{mid}: 不在本次请求中")
            continue
        if d in ("scheduled", "pending_confirmation"):
            e = written.get(mid)
            want = "confirmed" if d == "scheduled" else "pending_confirmation"
            if e is None and ex.last_save_entries is not None and not ex.save_result["ok"]:
                v.append(f"{mid}: 草稿保存失败、未写入，不能给出 {d}；请改为 unresolved 并说明保存失败")
            elif e is None:
                v.append(f"{mid}: 结论为 {d}，但没有写入草稿，请先调用 save_draft")
            elif e["status"] != want:
                v.append(f"{mid}: 结论 {d} 与草稿状态 {e['status']} 不一致")
            elif it.get("suggested_time") and it["suggested_time"] != e["time"]:
                v.append(f"{mid}: suggested_time 与草稿时段 {e['time']} 不一致")
        elif d == "blocked":
            if not f.block_reasons:
                v.append(f"{mid}: 没有 block 级风险依据，不能阻断")
        elif d in ("need_info", "unresolved"):
            if not it.get("pending_questions") and not it.get("risk_reasons"):
                v.append(f"{mid}: {d} 需要在 pending_questions 中说明缺什么或卡在哪里")
        else:
            v.append(f"{mid}: 未知结论 {d}")
        if d not in ("scheduled", "pending_confirmation") and mid in written:
            v.append(f"{mid}: 已写入草稿却给出结论 {d}，请修正草稿或结论")
    return v


def _written_entries(ex: ToolExecutor) -> dict[str, dict]:
    """只有存储层确认写入成功的条目才算数；保存失败时尝试写入的条目不算。"""
    if not (ex.save_result or {}).get("ok"):
        return {}
    return {e["material_id"]: e for e in (ex.last_save_entries or [])}


def _review_status(f) -> str:
    if f.not_found or f.missing_fields or f.request_missing:
        return "missing_info"
    if f.block_reasons:
        return "blocked"
    if not f.rules_checked:
        return "not_checked"
    if f.needs_license_check and f.license_status != "verified":
        return "pending_check"
    if f.confirm_reasons:
        return "needs_confirmation"
    return "passed"


def build_report(ex: ToolExecutor, items: list[dict] | None, stop_reason: str, summary: str = "") -> dict:
    saved = _written_entries(ex)
    attempted = {e["material_id"]: e for e in (ex.last_save_entries or [])}
    by_mid = {i["material_id"]: i for i in (items or [])}
    out_items = []
    for mid, req in ex.req_items.items():
        f = ex.facts[mid]
        it = by_mid.get(mid, {"decision": "unresolved", "pending_questions": ["任务未完成，未得出结论"]})
        e = saved.get(mid)
        unsaved = attempted.get(mid) if e is None else None
        out_items.append({
            "material_id": mid,
            "requested": {k: req.get(k) for k in ("channel", "date", "time")},
            "review_status": _review_status(f),
            "risk_reasons": f.block_reasons + f.confirm_reasons + (it.get("risk_reasons") or []),
            "decision": it["decision"],
            "suggested_slot": {k: e[k] for k in ("channel", "date", "time")} if e else None,
            "pending_questions": it.get("pending_questions") or [],
            # 保存失败时，建议时段只作为"未保存的建议"给出，不作为排期结果
            **({"unsaved_suggestion": {k: unsaved[k] for k in ("channel", "date", "time", "status")}} if unsaved else {}),
        })
    # 草稿状态只以存储层结果为准
    sr = ex.save_result
    if sr is None:
        draft = {"status": "not_saved"}
    elif sr["ok"]:
        draft = {"status": "saved", "draft_id": sr["draft_id"], "version": sr["version"], "outcome": sr["outcome"]}
    else:
        draft = {"status": "failed", "error": sr["error"]}

    decisions = {i["decision"] for i in out_items}
    if stop_reason != "finished":
        task_status = "incomplete"
    elif draft["status"] == "failed":
        task_status = "save_failed"
    elif "unresolved" in decisions:
        task_status = "incomplete"
    elif decisions & {"pending_confirmation", "need_info"}:
        task_status = "awaiting_user"
    else:
        task_status = "completed"
    return {
        "task_status": task_status,
        "stop_reason": stop_reason,
        "items": out_items,
        "draft": draft,
        "published": False,
        "summary": summary,
    }


def run_agent(request: dict, llm, executor: ToolExecutor, max_steps: int = 20) -> RunResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]
    trace: list[TraceStep] = []
    step = 0
    while step < max_steps:
        turn = llm.complete(messages, TOOL_SCHEMAS)
        messages.append(turn["message"])
        if not turn["tool_calls"]:
            step += 1
            trace.append(TraceStep(step, "(no_tool_call)", {}, (turn["message"].get("content") or "")[:200], "提醒模型必须调用工具"))
            messages.append({"role": "user", "content": "请通过工具调用推进任务；完成时调用 finish。"})
            continue
        for call in turn["tool_calls"]:
            step += 1
            name, args = call["name"], call["arguments"]
            reason = args.get("reason", "") if isinstance(args, dict) else ""
            if isinstance(args, dict) and "_raw" in args:
                obs = {"ok": False, "error": "invalid_json", "message": "工具参数不是合法 JSON（括号或引号不匹配），请检查后重新调用"}
            elif name == "finish":
                violations = validate_finish(executor, args.get("items", []))
                obs = {"ok": False, "error": "finish_rejected", "violations": violations} if violations else {"ok": True}
            else:
                obs = executor.call(name, args)
            clean = {k: v for k, v in args.items() if k != "reason"} if isinstance(args, dict) else {"_raw": args}
            trace.append(TraceStep(step, name, clean, reason, summarize(name, obs)))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(obs, ensure_ascii=False)})
            if name == "finish" and obs["ok"]:
                _link(trace)
                return RunResult(build_report(executor, args["items"], "finished", args.get("summary", "")), trace)
            if step >= max_steps:
                break
    _link(trace)
    report = build_report(executor, None, f"step_limit({max_steps})")
    return RunResult(report, trace)


def _link(trace: list[TraceStep]) -> None:
    for a, b in zip(trace, trace[1:]):
        a.next_action = b.action
    if trace:
        trace[-1].next_action = "end"
