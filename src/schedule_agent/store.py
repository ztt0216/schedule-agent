"""数据加载与草稿存储。

DraftStore 是唯一的"写"出口：只保存草稿，不存在任何发布接口。
草稿按请求幂等键（draft_id）去重，同一请求无论提交多少次只保留一份草稿。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@dataclass
class Dataset:
    materials: dict[str, dict]
    license_registry: dict[str, dict]
    rules: dict
    schedule: dict

    @classmethod
    def load(cls, data_dir: Path | None = None, schedule_path: Path | None = None) -> "Dataset":
        data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        m = _load_json(data_dir / "materials.json")
        return cls(
            materials={x["material_id"]: x for x in m["materials"]},
            license_registry=m.get("license_registry", {}),
            rules=_load_json(data_dir / "rules.json"),
            schedule=_load_json(Path(schedule_path) if schedule_path else data_dir / "schedule.json"),
        )

    def occupant(self, channel: str, date: str, time: str) -> str | None:
        for s in self.schedule["occupied"]:
            if (s["channel"], s["date"], s["time"]) == (channel, date, time):
                return s["material_id"]
        return None


def request_key(request: dict) -> str:
    """请求幂等键：只取排期诉求本身（素材、渠道、日期、时间），不含 confirmations，
    因此用户确认后的第二轮会更新同一份草稿，而不是新建一份。"""
    items = sorted(
        (i.get("material_id"), i.get("channel"), i.get("date"), i.get("time"))
        for i in request.get("items", [])
    )
    raw = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return "D-" + hashlib.sha256(raw.encode()).hexdigest()[:10]


SLOT_FIELDS = ("material_id", "channel", "date", "time", "status")


def _content_hash(entries: list[dict]) -> str:
    """只按排期字段判断内容是否变化；note 等自由文本每次生成都可能不同，不参与去重。"""
    norm = sorted(json.dumps([e.get(k) for k in SLOT_FIELDS], ensure_ascii=False) for e in entries)
    return hashlib.sha256("\n".join(norm).encode()).hexdigest()[:12]


@dataclass
class DraftStore:
    path: Path
    _drafts: dict = field(default_factory=dict)

    def __post_init__(self):
        self.path = Path(self.path)
        if self.path.exists():
            self._drafts = _load_json(self.path)

    def save(self, draft_id: str, entries: list[dict]) -> dict:
        """返回 {draft_id, version, outcome}，outcome ∈ created / updated / duplicate。"""
        h = _content_hash(entries)
        cur = self._drafts.get(draft_id)
        if cur and cur["content_hash"] == h:
            return {"draft_id": draft_id, "version": cur["version"], "outcome": "duplicate"}
        version = cur["version"] + 1 if cur else 1
        new = {
            **self._drafts,
            draft_id: {
                "draft_id": draft_id,
                "version": version,
                "content_hash": h,
                "entries": entries,
                "published": False,
            },
        }
        # 先落盘再更新内存：写盘失败时内存保持原状，重试不会被误判为 duplicate
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self._drafts = new
        return {"draft_id": draft_id, "version": version, "outcome": "updated" if cur else "created"}

    def all(self) -> dict:
        return dict(self._drafts)
