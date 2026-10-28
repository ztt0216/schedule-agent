"""模型接入。

- OpenAICompatLLM：任意 OpenAI 兼容的 Chat Completions + function calling 接口
  （DeepSeek / 通义千问 DashScope 兼容模式 / OpenAI / 本地 vLLM 等），仅用标准库实现。
- MockLLM：离线规则策略。它和真实模型一样，只能看到消息历史（请求 + 工具返回），
  不能直接读取执行器内部状态。它只用于验证流程与护栏，不能证明真实模型的决策效果。
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request


def _ssl_context() -> ssl.SSLContext:
    # python.org 版 Python 在 macOS 上默认不带根证书；装了 certifi 就用它的证书包
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _assistant(name: str, args: dict, n: int) -> dict:
    call_id = f"call_{n}"
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}],
    }
    return {"message": msg, "tool_calls": [{"id": call_id, "name": name, "arguments": args}]}


class OpenAICompatLLM:
    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None, timeout: int = 60):
        self.model = model or os.environ["LLM_MODEL"]
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ["LLM_API_KEY"]
        self.timeout = timeout

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": [{"type": "function", "function": t} for t in tools],
            "tool_choice": "auto",
            "temperature": 0,
        }
        data = self._post(body)
        msg = data["choices"][0]["message"]
        keep = {"role": "assistant", "content": msg.get("content")}
        calls = []
        if msg.get("tool_calls"):
            keep["tool_calls"] = msg["tool_calls"]
            for c in msg["tool_calls"]:
                raw = c["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw)
                except json.JSONDecodeError:
                    args = {"_raw": raw}
                calls.append({"id": c["id"], "name": c["function"]["name"], "arguments": args})
        return {"message": keep, "tool_calls": calls}

    def _post(self, body: dict, retries: int = 2) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context()) as r:
                    return json.loads(r.read())
            except (urllib.error.URLError, TimeoutError) as e:
                status = getattr(e, "code", None)
                if attempt == retries or (status and status < 500 and status != 429):
                    detail = e.read().decode(errors="replace")[:500] if hasattr(e, "read") else ""
                    raise RuntimeError(f"LLM 调用失败: {e} {detail}") from e
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")


class MockLLM:
    """确定性的离线策略：根据已观察到的工具结果选择下一步。"""

    def __init__(self):
        self.n = 0

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        self.n += 1
        request = json.loads(messages[1]["content"])
        history = _history(messages)
        name, args = self._decide(request, history)
        return _assistant(name, args, self.n)

    def _decide(self, req: dict, H: list[tuple[str, dict, dict]]):
        def calls(name, **match):
            return [(a, o) for n, a, o in H if n == name and all(a.get(k) == v for k, v in match.items())]

        def latest(name, **match):
            c = calls(name, **match)
            return c[-1][1] if c else None

        confirmations = req.get("confirmations") or {}
        decisions, entries, taken = [], [], set()
        for item in req["items"]:
            mid = item.get("material_id")
            ch, date, want = item.get("channel"), item.get("date"), item.get("time")

            missing = [k for k in ("channel", "date", "time") if not item.get(k)]
            if missing:
                decisions.append({"material_id": mid, "decision": "need_info", "pending_questions": [f"请补充发布要求: {', '.join(missing)}"]})
                continue

            r = latest("read_material", material_id=mid)
            if r is None or (r.get("error") == "tool_unavailable" and len(calls("read_material", material_id=mid)) < 2):
                return "read_material", {"material_id": mid, "reason": f"先读取 {mid} 元数据，确认必填字段"}
            if not r["ok"]:
                q = "素材库中不存在该素材，请确认素材 ID" if r.get("error") == "not_found" else "读取素材失败，请稍后重试"
                decisions.append({"material_id": mid, "decision": "need_info" if r.get("error") == "not_found" else "unresolved", "pending_questions": [q]})
                continue
            if r["missing_fields"]:
                decisions.append({"material_id": mid, "decision": "need_info", "pending_questions": [f"请补充素材字段: {', '.join(r['missing_fields'])}"]})
                continue

            c = latest("check_rules", material_id=mid)
            if c is None or (c.get("error") == "tool_unavailable" and len(calls("check_rules", material_id=mid)) < 2):
                return "check_rules", {"material_id": mid, "channel": ch, "reason": f"{mid} 字段齐全，进行审核规则检查"}
            if not c["ok"]:
                decisions.append({"material_id": mid, "decision": "unresolved", "pending_questions": ["规则检查工具不可用，无法完成审核"]})
                continue
            if any(x["level"] == "block" for x in c["risks"]):
                decisions.append({"material_id": mid, "decision": "blocked"})
                continue
            if any(x["level"] == "verify" for x in c["risks"]):
                lv = latest("verify_license", material_id=mid)
                if lv is None:
                    return "verify_license", {"material_id": mid, "reason": f"{mid} 授权状态为 pending，按 R02 补查授权"}
                if not lv["ok"] or lv["status"] == "unknown":
                    decisions.append({"material_id": mid, "decision": "need_info", "pending_questions": ["授权状态无法核验，请提供授权证明"]})
                    continue
                if lv["status"] == "expired":
                    decisions.append({"material_id": mid, "decision": "blocked"})
                    continue
            need_confirm = any(x["level"] == "confirm" for x in c["risks"])

            target = confirmations.get(mid, want)
            q = latest("query_slots", channel=ch, date=date, time=target)
            if q is None or (q.get("error") == "tool_unavailable" and len(calls("query_slots", channel=ch, date=date, time=target)) < 2):
                why = f"用户已确认 {target}，核实该时段仍可用" if mid in confirmations else f"查询用户要求的 {target} 是否可用"
                if q is not None:
                    why = "时段查询工具失败，重试一次"
                return "query_slots", {"channel": ch, "date": date, "time": target, "reason": why}
            if not q["ok"]:
                decisions.append({"material_id": mid, "decision": "unresolved", "pending_questions": ["时段查询连续失败，无法确定排期，请稍后重试"]})
                continue

            if q["requested"]["available"] and (ch, date, target) not in taken:
                time_, status = target, "confirmed"
                note = "用户已确认" if mid in confirmations else "按用户要求时段"
                if need_confirm and mid not in confirmations:
                    status, note = "pending_confirmation", "商业合作内容需商务确认"
            else:
                # 不抢占本次请求里其他素材指定的时段
                reserved = {(i.get("channel"), i.get("date"), i.get("time")) for i in req["items"] if i is not item}
                alts = [t for t in q["alternatives"] if (ch, date, t) not in taken | reserved]
                if not alts:
                    decisions.append({"material_id": mid, "decision": "need_info", "pending_questions": [f"{date} 无可用时段，请提供其他日期"]})
                    continue
                time_, status = alts[0], "pending_confirmation"
                note = f"{target} 已被占用，建议改为 {alts[0]}，需用户确认"
            taken.add((ch, date, time_))
            entries.append({"material_id": mid, "channel": ch, "date": date, "time": time_, "status": status, "note": note})
            d = {"material_id": mid, "decision": "scheduled" if status == "confirmed" else "pending_confirmation", "suggested_time": time_}
            if status != "confirmed":
                d["pending_questions"] = [note]
            decisions.append(d)

        if entries:
            saves = calls("save_draft")
            last = saves[-1][1] if saves else None
            retry_ok = last is not None and last.get("error") in ("storage_unavailable", "storage_error") and len(saves) < 2
            if last is None or retry_ok:
                why = "草稿存储失败，重试一次" if retry_ok else f"所有素材已判定，保存 {len(entries)} 条排期草稿"
                return "save_draft", {"entries": entries, "reason": why}
            if not last["ok"]:
                for d in decisions:
                    if d["decision"] in ("scheduled", "pending_confirmation"):
                        d["decision"] = "unresolved"
                        d.pop("suggested_time", None)
                        d.setdefault("pending_questions", []).append("草稿保存失败，未写入，需重新提交")
        summary = "；".join(f'{d["material_id"]}:{d["decision"]}' for d in decisions)
        return "finish", {"items": decisions, "summary": summary, "reason": "所有素材都已得出结论"}


def _history(messages: list[dict]) -> list[tuple[str, dict, dict]]:
    """把消息历史还原成 (工具名, 参数, 观察结果) 列表。"""
    pending, out = {}, []
    for m in messages:
        if m["role"] == "assistant":
            for c in m.get("tool_calls") or []:
                pending[c["id"]] = (c["function"]["name"], json.loads(c["function"]["arguments"]))
        elif m["role"] == "tool" and m["tool_call_id"] in pending:
            name, args = pending.pop(m["tool_call_id"])
            out.append((name, args, json.loads(m["content"])))
    return out
