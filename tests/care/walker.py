"""Executes config/workflows.json against the sandbox service with scripted visitor answers.

This validates workflow DESIGN and the action contract locally. It is not a substitute for testing the
workflows inside eGain (see docs/test-plan.md, section 'Platform validation').
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "egain"
WF = json.loads((ROOT / "config" / "workflows.json").read_text(encoding="utf-8"))
ACTIONS = json.loads((ROOT / "config" / "actions.json").read_text(encoding="utf-8"))["actions"]
KEY = "test-key"


class Result:
    def __init__(self):
        self.said: list[str] = []
        self.actions: list[tuple[str, int]] = []
        self.path: list[str] = []
        self.ctx: dict = {}

    @property
    def text(self) -> str:
        return " ".join(self.said)


def run(client, workflow: str, answers: dict | None = None, yes_no: list[str] | None = None,
        faults: set[str] | None = None, ctx: dict | None = None, max_steps: int = 60) -> Result:
    """answers: field -> value or list of values (consumed in order, for invalid-then-valid retries)."""
    res = Result()
    res.ctx = dict(ctx or {})
    answers = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (answers or {}).items()}
    yn = list(yes_no or [])
    faults = faults or set()
    wf, step = workflow, WF["workflows"][workflow]["entry"]

    def fmt(s: str) -> str:
        return re.sub(r"\{(\w+)\}", lambda m: str(res.ctx.get(m.group(1), "")), s)

    def goto(target):
        nonlocal wf, step
        if target is None:
            step = None
        elif target.startswith("wf_"):
            wf, step = target, WF["workflows"][target]["entry"]
        else:
            step = target

    for _ in range(max_steps):
        if step is None:
            return res
        res.path.append(f"{wf}.{step}")
        s = WF["workflows"][wf]["steps"][step]
        if "say" in s:
            res.said.append(fmt(s["say"]))
            goto(s.get("next"))
        elif "ask" in s:
            ok = True
            for f in s["ask"]:
                if not answers.get(f):
                    raise AssertionError(f"no scripted answer for {f} at {wf}.{step}")
                v = answers[f].pop(0) if len(answers[f]) > 1 else answers[f][0]
                pat = s.get("validate", {}).get(f)
                if pat and not re.fullmatch(pat, str(v)):
                    ok = False
                    res.said.append(f"invalid {f}")
                    break
                res.ctx[f] = v
            goto(s["next"] if ok else s["on_invalid"])
        elif "ask_yes_no" in s:
            a = yn.pop(0)
            goto(s["yes"] if a == "yes" else s["no"])
        elif "action" in s:
            name = s["action"]
            spec = ACTIONS[name]
            if "category" in s:
                res.ctx["category"] = s["category"]
            res.ctx.setdefault("summary", f"Visitor needs help with {res.ctx.get('category', 'other')} (demo)")
            res.ctx.setdefault("category", "other")
            body = {k: res.ctx[k] for k in spec["body"] if k in res.ctx and res.ctx[k] not in (None, "")}
            headers = {"X-API-Key": KEY}
            if name in faults:
                headers["X-Simulate-Failure"] = "1"
            r = client.post(spec["path"], json=body, headers=headers)
            res.actions.append((name, r.status_code))
            data = r.json()
            res.ctx.update({k: v for k, v in data.items() if isinstance(v, (str, int, float, bool))})
            if r.status_code == 200:
                goto(s["on_success"])
            else:
                goto(s.get(f"on_{r.status_code}", s["on_error"]))
        elif "goto" in s:
            goto(s["goto"])
        elif "choices" in s:
            goto(s["choices"][res.ctx["choice"]])
        else:
            raise AssertionError(f"unknown step type {wf}.{step}")
    raise AssertionError("workflow did not terminate (loop?)")
