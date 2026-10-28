"""六个必须验收点（Mock 策略驱动，验证流程与护栏）。"""
import json

from conftest import FREED, actions, item


# 验收 1：正常排期
def test_normal_schedule(run):
    report, trace, _ = run("01_normal.json")
    assert report["task_status"] == "completed"
    assert item(report, "M02")["decision"] == "scheduled"
    assert item(report, "M02")["suggested_slot"]["time"] == "20:00"
    # M05 授权待核验 → 模型先补查再排期
    assert "verify_license" in actions(trace)
    assert item(report, "M05")["decision"] == "scheduled"
    assert report["draft"]["status"] == "saved"
    assert report["published"] is False


# 验收 2：素材信息缺失（素材字段缺失 / 请求缺时间 / 素材不存在）
def test_missing_info(run, store_path):
    report, trace, _ = run("03_missing_info.json")
    assert report["task_status"] == "awaiting_user"
    m03 = item(report, "M03")
    assert m03["decision"] == "need_info" and m03["review_status"] == "missing_info"
    assert "cover_url" in m03["pending_questions"][0] and "license_id" in m03["pending_questions"][0]
    assert "time" in item(report, "M02")["pending_questions"][0]
    assert item(report, "M99")["decision"] == "need_info"
    # 缺信息的素材不做规则检查、不写草稿
    assert "check_rules" not in actions(trace) and "save_draft" not in actions(trace)
    assert report["draft"]["status"] == "not_saved"
    assert not store_path.exists()


# 验收 3：规则风险（阻断 / 补查后阻断 / 需确认）
def test_rule_risk(run):
    report, trace, _ = run("04_rule_risk.json")
    m04, m06, m07 = item(report, "M04"), item(report, "M06"), item(report, "M07")
    assert m04["decision"] == "blocked" and any("R01" in r for r in m04["risk_reasons"])
    assert m06["decision"] == "blocked" and any("过期" in r for r in m06["risk_reasons"])
    assert m07["decision"] == "pending_confirmation" and any("R03" in r for r in m07["risk_reasons"])
    assert m04["suggested_slot"] is None and m06["suggested_slot"] is None
    assert report["task_status"] == "awaiting_user"


# 验收 4：时段冲突 —— 参考实例：18:00 被占，建议 19:00 并待确认，不得称为已发布
def test_slot_conflict_reference_case(run):
    report, _, _ = run("02_slot_conflict.json")
    m01 = item(report, "M01")
    assert m01["decision"] == "pending_confirmation"
    assert m01["suggested_slot"]["time"] == "19:00"
    assert report["task_status"] == "awaiting_user"
    assert report["published"] is False


def test_conflict_then_user_confirms_updates_same_draft(run, store_path):
    r1, _, _ = run("02_slot_conflict.json")
    r2, _, _ = run("02b_slot_conflict_confirmed.json")
    assert item(r2, "M01")["decision"] == "scheduled"
    assert r2["task_status"] == "completed"
    assert r2["draft"]["draft_id"] == r1["draft"]["draft_id"]
    assert r2["draft"]["version"] == 2
    assert len(json.loads(store_path.read_text())) == 1


def test_multi_items_do_not_steal_each_others_slot(run):
    report, _, _ = run("05_multi.json")
    assert item(report, "M02")["suggested_slot"]["time"] == "19:00"
    assert item(report, "M02")["decision"] == "scheduled"
    assert item(report, "M01")["suggested_slot"]["time"] == "20:00"


# 验收 5：重复提交 —— 相同请求只产生一份草稿
def test_duplicate_submit_single_draft(run, store_path):
    r1, _, _ = run("05_multi.json")
    r2, _, _ = run("05_multi.json")
    assert r1["draft"]["outcome"] == "created"
    assert r2["draft"]["outcome"] == "duplicate"
    assert r2["draft"]["draft_id"] == r1["draft"]["draft_id"]
    assert len(json.loads(store_path.read_text())) == 1


# 审核要点：更换占用时段并重复提交 → 动作随之调整，仍只有一份草稿
def test_resubmit_after_occupancy_changes(run, store_path):
    r1, _, _ = run("05_multi.json")
    r2, _, _ = run("05_multi.json", schedule=FREED)
    assert item(r1, "M01")["decision"] == "pending_confirmation"
    assert item(r2, "M01")["decision"] == "scheduled"
    assert item(r2, "M01")["suggested_slot"]["time"] == "18:00"
    assert r2["draft"]["version"] == 2
    assert len(json.loads(store_path.read_text())) == 1


# 验收 6：保存失败 —— 必须明确返回失败，不能虚报成功
def test_save_failure_reported(run, store_path):
    report, trace, _ = run("02_slot_conflict.json", faults={"save_draft": 2})
    assert actions(trace).count("save_draft") == 2  # 重试一次后放弃
    assert report["task_status"] == "save_failed"
    assert report["draft"]["status"] == "failed"
    assert not store_path.exists()
    m01 = item(report, "M01")
    # 保存失败时不能出现 scheduled / pending_confirmation 这类"已排期"结论
    assert m01["decision"] == "unresolved" and m01["suggested_slot"] is None
    assert m01["unsaved_suggestion"]["time"] == "19:00"
    assert any("保存失败" in q for q in m01["pending_questions"])


def test_save_failure_then_retry_succeeds(run):
    report, trace, _ = run("02_slot_conflict.json", faults={"save_draft": 1})
    assert actions(trace).count("save_draft") == 2
    assert report["draft"]["status"] == "saved"
    assert report["task_status"] == "awaiting_user"


# 其他工具失败：重试一次；连续失败则标记未完成
def test_query_tool_failure_retry(run):
    report, trace, _ = run("02_slot_conflict.json", faults={"query_slots": 1})
    assert actions(trace).count("query_slots") == 2
    assert item(report, "M01")["decision"] == "pending_confirmation"


def test_query_tool_failure_gives_up(run):
    report, _, _ = run("02_slot_conflict.json", faults={"query_slots": 2})
    assert item(report, "M01")["decision"] == "unresolved"
    assert report["task_status"] == "incomplete"
    assert report["draft"]["status"] == "not_saved"


# 步数上限：返回明确的未完成状态
def test_step_limit(run):
    report, trace, _ = run("01_normal.json", max_steps=3)
    assert len(trace) == 3
    assert report["task_status"] == "incomplete"
    assert report["stop_reason"] == "step_limit(3)"
    assert all(i["decision"] == "unresolved" for i in report["items"])
