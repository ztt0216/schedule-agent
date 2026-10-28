"""批量运行验收场景，把报告与 trace 写到 examples/<mode>/。

python scripts/record_examples.py --mode mock
python scripts/record_examples.py --mode llm     # 需要 .env 中的 LLM_* 配置
"""
from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from schedule_agent.cli import main  # noqa: E402

REQ = "examples/requests"
FREED = "data/scenarios/schedule_18_freed.json"

# (输出名, 请求文件, 额外参数, 草稿存储分组)。同组场景按顺序共享草稿存储，用于验证重复提交。
SCENARIOS = [
    ("01_normal", "01_normal.json", [], "a"),
    ("02_slot_conflict", "02_slot_conflict.json", [], "b"),
    ("02b_user_confirmed", "02b_slot_conflict_confirmed.json", [], "b"),
    ("03_missing_info", "03_missing_info.json", [], "c"),
    ("04_rule_risk", "04_rule_risk.json", [], "d"),
    ("05a_submit", "05_multi.json", [], "e"),
    ("05b_duplicate_submit", "05_multi.json", [], "e"),
    ("05c_resubmit_after_slot_freed", "05_multi.json", ["--schedule", FREED], "e"),
    ("06a_save_failure", "02_slot_conflict.json", ["--fail", "save_draft=2"], "f"),
    ("06b_save_retry_ok", "02_slot_conflict.json", ["--fail", "save_draft=1"], "g"),
    ("07_query_tool_failure", "02_slot_conflict.json", ["--fail", "query_slots=2"], "h"),
    ("08_step_limit", "01_normal.json", ["--max-steps", "3"], "i"),
]


def main_(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["mock", "llm"], default="mock")
    p.add_argument("--only", nargs="*", help="只跑指定场景名前缀")
    a = p.parse_args(argv)
    out_dir = ROOT / "examples" / a.mode
    state = ROOT / ".state" / f"examples_{a.mode}"
    shutil.rmtree(state, ignore_errors=True)
    for name, req, extra, group in SCENARIOS:
        if a.only and not any(name.startswith(x) for x in a.only):
            continue
        args = ["run", f"{REQ}/{req}", "--store", str(state / f"{group}.json"), "--out", str(out_dir / f"{name}.json"), *extra]
        if a.mode == "mock":
            args.append("--mock")
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            main(args)
        (out_dir / f"{name}.log").write_text(log.getvalue(), encoding="utf-8")
        print(f"✓ {name}")


if __name__ == "__main__":
    main_()
