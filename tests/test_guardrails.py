"""护栏：模型给出越权或不实的动作时，代码拒绝执行并把原因反馈给模型。"""
import json

from conftest import actions, item, load_request
from schedule_agent.agent import validate_finish
from schedule_agent.llm import _assistant
from schedule_agent.store import Dataset, DraftStore
from schedule_agent.tools import TOOL_SCHEMAS, ToolExecutor


def make_ex(store_path, name="02_slot_conflict.json"):
    return ToolExecutor(Dataset.load(), DraftStore(store_path), load_request(name))


def prepare_m01(ex):
    ex.call("read_material", {"material_id": "M01"})
    ex.call("check_rules", {"material_id": "M01", "channel": "douyin"})


ENTRY = {"material_id": "M01", "channel": "douyin", "date": "2026-11-03"}


def test_no_publish_tool_exists():
    assert not any("publish" in t["name"] for t in TOOL_SCHEMAS)


def test_changed_time_cannot_be_confirmed_without_user(store_path):
    ex = make_ex(store_path)
    prepare_m01(ex)
    obs = ex.call("save_draft", {"entries": [{**ENTRY, "time": "19:00", "status": "confirmed"}]})
    assert obs["error"] == "validation_failed"
    assert any("pending_confirmation" in v for v in obs["violations"])
    assert not store_path.exists()


def test_occupied_slot_rejected(store_path):
    ex = make_ex(store_path)
    prepare_m01(ex)
    obs = ex.call("save_draft", {"entries": [{**ENTRY, "time": "18:00", "status": "confirmed"}]})
    assert any("X90" in v for v in obs["violations"])


def test_cannot_change_channel(store_path):
    ex = make_ex(store_path)
    prepare_m01(ex)
    obs = ex.call("save_draft", {"entries": [{**ENTRY, "channel": "xiaohongshu", "time": "19:00", "status": "pending_confirmation"}]})
    assert any("渠道或日期" in v for v in obs["violations"])


def test_unchecked_material_cannot_be_saved(store_path):
    ex = make_ex(store_path)
    obs = ex.call("save_draft", {"entries": [{**ENTRY, "time": "19:00", "status": "pending_confirmation"}]})
    assert obs["error"] == "validation_failed"


def test_blocked_material_cannot_be_saved(store_path):
    ex = make_ex(store_path, "04_rule_risk.json")
    ex.call("read_material", {"material_id": "M04"})
    ex.call("check_rules", {"material_id": "M04", "channel": "douyin"})
    obs = ex.call("save_draft", {"entries": [{"material_id": "M04", "channel": "douyin", "date": "2026-11-05", "time": "12:00", "status": "pending_confirmation"}]})
    assert any("阻断" in v for v in obs["violations"])


def test_finish_cannot_claim_scheduled_without_draft(store_path):
    ex = make_ex(store_path)
    prepare_m01(ex)
    v = validate_finish(ex, [{"material_id": "M01", "decision": "scheduled", "suggested_time": "19:00"}])
    assert any("save_draft" in x for x in v)


def test_finish_cannot_block_without_evidence(store_path):
    ex = make_ex(store_path)
    prepare_m01(ex)
    v = validate_finish(ex, [{"material_id": "M01", "decision": "blocked"}])
    assert any("block 级风险" in x for x in v)


class ScriptedLLM:
    """模拟一个先犯错、看到护栏反馈后修正的模型，验证循环能把错误作为观察回传。"""

    def __init__(self, script):
        self.script, self.i = script, 0

    def complete(self, messages, tools):
        name, args = self.script[self.i]
        self.i += 1
        return _assistant(name, args, self.i)


def test_loop_feeds_violation_back_and_model_corrects(run, store_path):
    r = "x"
    entry = {**ENTRY, "time": "19:00"}
    llm = ScriptedLLM([
        ("read_material", {"material_id": "M01", "reason": r}),
        ("check_rules", {"material_id": "M01", "channel": "douyin", "reason": r}),
        ("query_slots", {"channel": "douyin", "date": "2026-11-03", "time": "18:00", "reason": r}),
        ("save_draft", {"entries": [{**entry, "status": "confirmed"}], "reason": r}),  # 越权：擅自确认
        ("finish", {"items": [{"material_id": "M01", "decision": "scheduled"}], "reason": r}),  # 虚报
        ("save_draft", {"entries": [{**entry, "status": "pending_confirmation"}], "reason": r}),
        ("finish", {"items": [{"material_id": "M01", "decision": "pending_confirmation", "suggested_time": "19:00"}], "reason": r}),
    ])
    report, trace, _ = run("02_slot_conflict.json", llm=llm)
    assert trace[3].observation.startswith("失败 validation_failed")
    assert trace[4].observation.startswith("失败 finish_rejected")
    assert item(report, "M01")["decision"] == "pending_confirmation"
    assert report["draft"]["status"] == "saved"
    stored = json.loads(store_path.read_text())
    assert [e["status"] for d in stored.values() for e in d["entries"]] == ["pending_confirmation"]
    assert actions(trace)[-1] == "finish"


def test_failed_save_does_not_count_as_written(store_path):
    ex = ToolExecutor(Dataset.load(), DraftStore(store_path), load_request("02_slot_conflict.json"), {"save_draft": 1})
    prepare_m01(ex)
    ex.call("query_slots", {"channel": "douyin", "date": "2026-11-03", "time": "18:00"})
    assert ex.call("save_draft", {"entries": [{**ENTRY, "time": "19:00", "status": "pending_confirmation"}]})["error"] == "storage_unavailable"
    # 保存失败后如实报告 unresolved 应被接受，而不是被告知"已写入草稿"
    assert validate_finish(ex, [{"material_id": "M01", "decision": "unresolved", "pending_questions": ["保存失败"]}]) == []


def test_request_missing_time_cannot_be_filled_by_model(store_path):
    ex = make_ex(store_path, "03_missing_info.json")
    ex.call("read_material", {"material_id": "M02"})
    ex.call("check_rules", {"material_id": "M02", "channel": "douyin"})
    obs = ex.call("save_draft", {"entries": [{"material_id": "M02", "channel": "douyin", "date": "2026-11-04", "time": "12:00", "status": "pending_confirmation"}]})
    assert any("请求缺少 time" in v for v in obs["violations"])


def test_invalid_json_arguments_get_clear_feedback(run):
    r = "x"
    llm = ScriptedLLM([
        ("finish", {"_raw": '{"items": [}'}),
        ("finish", {"items": [{"material_id": "M01", "decision": "need_info", "pending_questions": ["?"]}], "reason": r}),
    ])
    _, trace, _ = run("02_slot_conflict.json", llm=llm)
    assert trace[0].observation.startswith("失败 invalid_json")


def test_note_change_still_counts_as_duplicate(store_path):
    s = DraftStore(store_path)
    e = {**ENTRY, "time": "19:00", "status": "pending_confirmation"}
    assert s.save("D-1", [{**e, "note": "a"}])["outcome"] == "created"
    assert s.save("D-1", [{**e, "note": "b"}])["outcome"] == "duplicate"
