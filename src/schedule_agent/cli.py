"""命令行入口。

python -m schedule_agent run examples/requests/01_normal.json --mock
python -m schedule_agent run examples/requests/02_slot_conflict.json --fail save_draft=1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import run_agent
from .llm import MockLLM, OpenAICompatLLM
from .store import Dataset, DraftStore
from .tools import ToolExecutor


def _load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _parse_faults(specs: list[str]) -> dict[str, int]:
    out = {}
    for s in specs or []:
        name, _, n = s.partition("=")
        out[name] = int(n or 1)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="schedule_agent", description="素材审核与发布排期 Agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="执行一次排期请求")
    r.add_argument("request", type=Path, help="请求 JSON 文件")
    r.add_argument("--mock", action="store_true", help="使用离线 Mock 策略（不调用真实模型）")
    r.add_argument("--fail", action="append", default=[], metavar="TOOL=N", help="故障注入：让某工具前 N 次调用失败")
    r.add_argument("--max-steps", type=int, default=20, help="最大工具调用步数（含 finish）")
    r.add_argument("--data-dir", type=Path, default=None)
    r.add_argument("--schedule", type=Path, default=None, help="替换已有排期文件（用于更换占用时段）")
    r.add_argument("--store", type=Path, default=Path(".state/drafts.json"), help="草稿存储文件")
    r.add_argument("--out", type=Path, default=None, help="把报告与 trace 写入该 JSON 文件")
    args = p.parse_args(argv)

    _load_env()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    ds = Dataset.load(args.data_dir, args.schedule)
    ex = ToolExecutor(ds, DraftStore(args.store), request, _parse_faults(args.fail))
    llm = MockLLM() if args.mock else OpenAICompatLLM()
    result = run_agent(request, llm, ex, max_steps=args.max_steps)

    for s in result.trace:
        a = json.dumps(s.args, ensure_ascii=False)
        print(f"[{s.step:02d}] {s.action} {a}\n     依据: {s.reason}\n     观察: {s.observation}\n     下一步: {s.next_action}")
    print("\n=== 报告 ===")
    print(json.dumps(result.report, ensure_ascii=False, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "request_file": str(args.request),
            "mode": "mock" if args.mock else f"llm:{llm.model}",
            "faults": _parse_faults(args.fail),
            "max_steps": args.max_steps,
            "schedule_override": str(args.schedule) if args.schedule else None,
            "report": result.report,
            "trace": result.trace_dicts(),
        }
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
