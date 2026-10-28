"""业务工具 + 执行器。

模型只能通过这里的工具影响外部世界。执行器做两件事：
1. 执行工具并返回观察结果（支持故障注入，用于验证"工具失败/保存失败"）；
2. 记录已观察到的事实（facts），供护栏校验模型的动作与最终结论。
工具集中没有"发布"能力，从结构上杜绝越权发布。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .store import Dataset, DraftStore, request_key

ENTRY_STATUSES = {"confirmed", "pending_confirmation"}

_REASON = {"type": "string", "description": "一句话说明为什么选择这个动作（决策依据，非完整思考过程）"}

TOOL_SCHEMAS = [
    {
        "name": "read_material",
        "description": "读取素材元数据，并返回缺失的必填字段。",
        "parameters": {
            "type": "object",
            "properties": {"material_id": {"type": "string"}, "reason": _REASON},
            "required": ["material_id", "reason"],
        },
    },
    {
        "name": "check_rules",
        "description": "按审核规则检查素材。返回 risks，每条带 level：block=必须阻断；confirm=需用户确认；verify=需调用 followup_tool 补查后再判定。",
        "parameters": {
            "type": "object",
            "properties": {
                "material_id": {"type": "string"},
                "channel": {"type": "string"},
                "reason": _REASON,
            },
            "required": ["material_id", "channel", "reason"],
        },
    },
    {
        "name": "verify_license",
        "description": "补查素材版权授权的最新状态（verified / expired / unknown）。",
        "parameters": {
            "type": "object",
            "properties": {"material_id": {"type": "string"}, "reason": _REASON},
            "required": ["material_id", "reason"],
        },
    },
    {
        "name": "query_slots",
        "description": "查询某渠道某日某时段是否可用，并返回同日最近的可用替代时段。",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "HH:MM，整点"},
                "reason": _REASON,
            },
            "required": ["channel", "date", "time", "reason"],
        },
    },
    {
        "name": "save_draft",
        "description": (
            "保存排期草稿（不会发布）。entries 只放可排期的素材。status=confirmed 仅限用户原始时段，"
            "或 request.confirmations 中用户已确认的时段；其他变更必须用 pending_confirmation。"
            "同一请求重复保存只会产生一份草稿。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "material_id": {"type": "string"},
                            "channel": {"type": "string"},
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                            "status": {"type": "string", "enum": sorted(ENTRY_STATUSES)},
                            "note": {"type": "string"},
                        },
                        "required": ["material_id", "channel", "date", "time", "status"],
                    },
                },
                "reason": _REASON,
            },
            "required": ["entries", "reason"],
        },
    },
    {
        "name": "finish",
        "description": (
            "结束任务并提交每个素材的结论。decision ∈ scheduled / pending_confirmation / blocked / need_info / unresolved。"
            "scheduled 与 pending_confirmation 必须已写入草稿；blocked 需有 block 级风险依据；need_info 需有缺失信息依据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "material_id": {"type": "string"},
                            "decision": {
                                "type": "string",
                                "enum": ["scheduled", "pending_confirmation", "blocked", "need_info", "unresolved"],
                            },
                            "suggested_time": {"type": "string", "description": "HH:MM，须与草稿中该素材的时段一致"},
                            "risk_reasons": {"type": "array", "items": {"type": "string"}},
                            "pending_questions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["material_id", "decision"],
                    },
                },
                "summary": {"type": "string"},
                "reason": _REASON,
            },
            "required": ["items", "reason"],
        },
    },
]


def _hours(start: str, end: str) -> list[str]:
    return [f"{h:02d}:00" for h in range(int(start[:2]), int(end[:2]) + 1)]


@dataclass
class MaterialFacts:
    read_ok: bool = False
    not_found: bool = False
    missing_fields: list[str] = field(default_factory=list)
    rules_checked: bool = False
    risks: list[dict] = field(default_factory=list)
    license_status: str | None = None
    tool_errors: list[str] = field(default_factory=list)
    request_missing: list[str] = field(default_factory=list)  # 用户请求本身缺的字段

    @property
    def needs_license_check(self) -> bool:
        return any(r["level"] == "verify" for r in self.risks)

    @property
    def block_reasons(self) -> list[str]:
        out = [f'{r["rule_id"]} {r["reason"]}' for r in self.risks if r["level"] == "block"]
        if self.license_status == "expired":
            out.append("R02 版权授权已过期")
        return out

    @property
    def confirm_reasons(self) -> list[str]:
        return [f'{r["rule_id"]} {r["reason"]}' for r in self.risks if r["level"] == "confirm"]


class ToolExecutor:
    def __init__(self, dataset: Dataset, store: DraftStore, request: dict, faults: dict[str, int] | None = None):
        self.ds = dataset
        self.store = store
        self.request = request
        self.faults = dict(faults or {})
        self.req_items = {i.get("material_id"): i for i in request.get("items", [])}
        self.confirmations: dict[str, str] = request.get("confirmations", {}) or {}
        self.facts: dict[str, MaterialFacts] = {
            mid: MaterialFacts(request_missing=[k for k in ("channel", "date", "time") if not it.get(k)])
            for mid, it in self.req_items.items()
        }
        self.save_attempts = 0
        self.last_save_entries: list[dict] | None = None  # 最近一次通过校验、实际尝试写入的条目
        self.save_result: dict | None = None  # 以存储层返回为准，不采信模型的说法

    # ---- 调度 ----
    def call(self, name: str, args: dict) -> dict:
        args = {k: v for k, v in (args or {}).items() if k != "reason"}
        fn = getattr(self, f"_t_{name}", None)
        if fn is None or name == "finish":
            return {"ok": False, "error": "unknown_tool", "message": f"没有名为 {name} 的工具"}
        if name != "save_draft" and self._inject_fault(name):
            for mid, it in self.req_items.items():
                if args.get("material_id") == mid or (
                    "channel" in args and (it.get("channel"), it.get("date")) == (args.get("channel"), args.get("date"))
                ):
                    self.facts[mid].tool_errors.append(name)
            return {"ok": False, "error": "tool_unavailable", "retryable": True, "message": f"{name} 暂时不可用"}
        try:
            return fn(**args)
        except TypeError as e:
            return {"ok": False, "error": "bad_arguments", "message": str(e)}

    def _inject_fault(self, name: str) -> bool:
        if self.faults.get(name, 0) > 0:
            self.faults[name] -= 1
            return True
        return False

    # ---- 工具实现 ----
    def _t_read_material(self, material_id: str) -> dict:
        if material_id not in self.facts:
            return {"ok": False, "error": "not_in_request", "message": f"{material_id} 不在本次请求中"}
        f = self.facts[material_id]
        m = self.ds.materials.get(material_id)
        if m is None:
            f.not_found = True
            return {"ok": False, "error": "not_found", "message": f"素材库中没有 {material_id}"}
        missing = [k for k in self.ds.rules["required_fields"] if m.get(k) in (None, "")]
        f.read_ok, f.missing_fields = True, missing
        return {"ok": True, "material": m, "missing_fields": missing}

    def _t_check_rules(self, material_id: str, channel: str) -> dict:
        f = self.facts.get(material_id)
        m = self.ds.materials.get(material_id)
        if f is None or m is None:
            return {"ok": False, "error": "not_found", "message": f"无法检查 {material_id}"}
        risks = []
        for r in self.ds.rules["rules"]:
            rid, lvl = r["rule_id"], r["level"]
            if "banned_terms" in r:
                hits = [t for t in r["banned_terms"] if t in (m.get(r["field"]) or "")]
                if hits:
                    risks.append({"rule_id": rid, "level": lvl, "reason": f'{r["name"]}：标题含 {"、".join(hits)}'})
            elif "max_duration_sec" in r:
                limit = r["max_duration_sec"].get(channel)
                if limit and (m.get("duration_sec") or 0) > limit:
                    risks.append({"rule_id": rid, "level": lvl, "reason": f'{r["name"]}：{m["duration_sec"]}s > {limit}s'})
            elif "license_status" in r.get("when", {}):
                if m.get("license_status") == r["when"]["license_status"]:
                    risks.append({"rule_id": rid, "level": lvl, "reason": r["name"], "followup_tool": r["followup_tool"]})
            elif "tag" in r.get("when", {}):
                if r["when"]["tag"] in m.get("tags", []):
                    risks.append({"rule_id": rid, "level": lvl, "reason": r["name"]})
        f.rules_checked, f.risks = True, risks
        return {"ok": True, "material_id": material_id, "passed": not risks, "risks": risks}

    def _t_verify_license(self, material_id: str) -> dict:
        m = self.ds.materials.get(material_id)
        if material_id not in self.facts or m is None:
            return {"ok": False, "error": "not_found"}
        rec = self.ds.license_registry.get(m.get("license_id") or "", {"status": "unknown", "note": "无登记记录"})
        self.facts[material_id].license_status = rec["status"]
        return {"ok": True, "material_id": material_id, "license_id": m.get("license_id"), **rec}

    def _t_query_slots(self, channel: str, date: str, time: str) -> dict:
        oh = self.ds.schedule["open_hours"]
        hours = _hours(oh["start"], oh["end"])
        if time not in hours:
            return {"ok": False, "error": "invalid_time", "message": f"可排时段为 {hours[0]}-{hours[-1]} 整点"}
        occ = self.ds.occupant(channel, date, time)
        base = int(time[:2])
        free = [h for h in hours if h != time and self.ds.occupant(channel, date, h) is None]
        free.sort(key=lambda h: (abs(int(h[:2]) - base), -int(h[:2])))  # 距离优先，同距离取更晚
        return {
            "ok": True,
            "channel": channel,
            "date": date,
            "requested": {"time": time, "available": occ is None, "occupied_by": occ},
            "alternatives": free[:3],
        }

    def _t_save_draft(self, entries: list[dict]) -> dict:
        violations = validate_entries(self, entries)
        if violations:
            return {"ok": False, "error": "validation_failed", "violations": violations, "message": "草稿未写入，请修正后重试"}
        self.save_attempts += 1
        self.last_save_entries = entries
        if self._inject_fault("save_draft"):
            self.save_result = {"ok": False, "error": "storage_unavailable"}
            return {"ok": False, "error": "storage_unavailable", "retryable": True, "message": "草稿存储暂不可用，本次未写入"}
        try:
            res = self.store.save(request_key(self.request), entries)
        except OSError as e:
            self.save_result = {"ok": False, "error": "storage_error"}
            return {"ok": False, "error": "storage_error", "retryable": True, "message": f"草稿写入失败（{type(e).__name__}），本次未写入"}
        self.save_result = {"ok": True, **res}
        return {"ok": True, **res, "published": False}


def validate_entries(ex: ToolExecutor, entries: list[dict]) -> list[str]:
    """save_draft 的护栏：硬约束由代码保证，模型无法绕过。"""
    v: list[str] = []
    seen = set()
    if not isinstance(entries, list) or not entries:
        return ["entries 不能为空"]
    for e in entries:
        mid = e.get("material_id")
        req = ex.req_items.get(mid)
        if req is None:
            v.append(f"{mid}: 不在本次请求中")
            continue
        f = ex.facts[mid]
        if e.get("status") not in ENTRY_STATUSES:
            v.append(f"{mid}: status 必须是 confirmed 或 pending_confirmation")
        if not f.read_ok or f.missing_fields:
            v.append(f"{mid}: 尚未读取素材或存在缺失字段，不能排期")
        if not f.rules_checked:
            v.append(f"{mid}: 尚未做规则检查")
        if f.block_reasons:
            v.append(f"{mid}: 存在阻断风险（{'; '.join(f.block_reasons)}），不能排期")
        if f.needs_license_check and f.license_status != "verified":
            v.append(f"{mid}: 版权待补查，尚未核验通过")
        if f.request_missing:
            v.append(f"{mid}: 请求缺少 {', '.join(f.request_missing)}，不能代替用户选择，请返回 need_info")
        if (e.get("channel"), e.get("date")) != (req.get("channel"), req.get("date")):
            v.append(f"{mid}: 不得擅自改变渠道或日期（用户要求 {req.get('channel')} {req.get('date')}）")
        oh = ex.ds.schedule["open_hours"]
        if e.get("time") not in _hours(oh["start"], oh["end"]):
            v.append(f"{mid}: 时段 {e.get('time')} 非法，可排时段为 {oh['start']}-{oh['end']} 整点")
        slot = (e.get("channel"), e.get("date"), e.get("time"))
        if slot in seen:
            v.append(f"{mid}: 与本草稿中其他素材时段重复 {e.get('time')}")
        seen.add(slot)
        occ = ex.ds.occupant(*slot)
        if occ:
            v.append(f"{mid}: {e.get('time')} 已被 {occ} 占用")
        if e.get("status") == "confirmed":
            approved = ex.confirmations.get(mid)
            if approved:
                ok = e.get("time") == approved
            else:
                ok = e.get("time") == req.get("time") and not f.confirm_reasons
            if not ok:
                v.append(f"{mid}: 时段变更或风险项未经用户确认，只能标记 pending_confirmation")
    return v
