import json
from pathlib import Path

import pytest

from schedule_agent.agent import run_agent
from schedule_agent.llm import MockLLM
from schedule_agent.store import Dataset, DraftStore
from schedule_agent.tools import ToolExecutor

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "examples" / "requests"
FREED = ROOT / "data" / "scenarios" / "schedule_18_freed.json"


def load_request(name: str) -> dict:
    return json.loads((REQ / name).read_text(encoding="utf-8"))


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "drafts.json"


@pytest.fixture
def run(store_path):
    """用 MockLLM 跑一次请求，返回 (report, trace, executor)。"""

    def _run(request, faults=None, schedule=None, max_steps=20, llm=None):
        if isinstance(request, str):
            request = load_request(request)
        ex = ToolExecutor(Dataset.load(schedule_path=schedule), DraftStore(store_path), request, faults)
        res = run_agent(request, llm or MockLLM(), ex, max_steps=max_steps)
        return res.report, res.trace, ex

    return _run


def item(report, mid):
    return next(i for i in report["items"] if i["material_id"] == mid)


def actions(trace):
    return [s.action for s in trace]
