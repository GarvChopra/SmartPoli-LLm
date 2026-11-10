"""The privacy gateway: nothing sensitive leaves this PC for a hosted model, and you can check what did.

Before any request goes to a hosted model (for example Nemotron on Nebius Token Factory), `Gateway.prepare`
swaps sensitive text for stable placeholders such as [EMAIL_1]. The model reasons over the placeholders, and
`Gateway.restore` puts the real values back locally, in the reply and in tool arguments, so tools still work.
Every request is appended to the egress ledger: when, where, how many messages and bytes, which kinds of
values were masked, and a short preview of what was actually sent. The ledger never stores the original values.

This is pattern based. It catches common formats, not every secret; see "Honest failures" in the README."""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if flip:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        flip = not flip
    return total % 10 == 0


def _card(match: re.Match) -> bool:
    digits = re.sub(r"\D", "", match.group(0))
    return 13 <= len(digits) <= 19 and _luhn(digits)


# (kind, pattern, optional check). Order matters: the first pattern to claim text wins.
_RULES: list[tuple[str, re.Pattern, object]] = [
    ("API_KEY", re.compile(r"\b(?:sk|nvapi|gsk|tvly|nb|AKIA|ghp|xox[bp])[-_]?[A-Za-z0-9_-]{16,}\b"), None),
    ("PASSWORD", re.compile(r"\b(?:password|passwd|pwd|passcode|pin)\b\s*(?:is|=|:)\s*\S+", re.I), None),
    ("OTP", re.compile(r"\b(?:otp|one[- ]time (?:password|code))\b\s*(?:is|=|:)?\s*\d{4,8}\b", re.I), None),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), None),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b"), _card),
    ("AADHAAR", re.compile(r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b"), None),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), None),
    ("PHONE", re.compile(r"(?<![\w])(?:\+\d{1,3}[ -]?)?[6-9]\d{4}[ -]?\d{5}\b|\+\d{1,3}[ -]?\d{6,12}\b"), None),
]

_PLACEHOLDER = re.compile(r"\[([A-Z_]+)_(\d+)\]")


@dataclass
class Prepared:
    """One request's masked messages, plus the private map used to restore replies locally."""
    messages: list[dict]
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original (never persisted)
    counts: dict[str, int] = field(default_factory=dict)  # kind -> how many values were masked


class Gateway:
    def __init__(self, ledger: "EgressLedger | None" = None):
        self.ledger = ledger

    def mask(self, text: str, state: Prepared, seen: dict[str, str]) -> str:
        for kind, pattern, check in _RULES:
            def swap(m: re.Match, kind=kind, check=check) -> str:
                if check is not None and not check(m):
                    return m.group(0)
                original = m.group(0)
                if original in seen:
                    return seen[original]
                n = sum(1 for key in state.mapping if key.startswith(f"[{kind}_")) + 1
                placeholder = f"[{kind}_{n}]"
                seen[original] = placeholder
                state.mapping[placeholder] = original
                state.counts[kind] = state.counts.get(kind, 0) + 1
                return placeholder
            text = pattern.sub(swap, text)
        return text

    def prepare(self, messages: list[dict]) -> Prepared:
        """Mask the text content of every message. Tool-call arguments are model output, already masked."""
        state = Prepared(messages=[])
        seen: dict[str, str] = {}
        for m in messages:
            copy = dict(m)
            if isinstance(copy.get("content"), str):
                copy["content"] = self.mask(copy["content"], state, seen)
            state.messages.append(copy)
        return state

    @staticmethod
    def restore(value, state: Prepared):
        """Put real values back into text (or every string inside args), locally."""
        if isinstance(value, str):
            return _PLACEHOLDER.sub(lambda m: state.mapping.get(m.group(0), m.group(0)), value)
        if isinstance(value, dict):
            return {k: Gateway.restore(v, state) for k, v in value.items()}
        if isinstance(value, list):
            return [Gateway.restore(v, state) for v in value]
        return value

    def record(self, state: Prepared, base_url: str, model: str, payload_bytes: int, outcome: str) -> None:
        if self.ledger is None:
            return
        preview = ""
        for m in reversed(state.messages):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                preview = m["content"][:160]
                break
        self.ledger.append(
            {
                "host": urlparse(base_url).netloc or base_url,
                "model": model,
                "messages": len(state.messages),
                "bytes": payload_bytes,
                "masked": dict(state.counts),
                "preview": preview,
                "outcome": outcome,
            }
        )


class EgressLedger:
    """Append-only JSONL of everything sent to a hosted model. Plain text on purpose: you can read it."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, entry: dict) -> None:
        entry = {"ts": time.time(), **entry}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read(self, limit: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows[-limit:]

    def summary(self) -> dict:
        rows = [r for r in self.read(limit=10**9) if "route" not in r]
        masked: dict[str, int] = {}
        for r in rows:
            for kind, n in r.get("masked", {}).items():
                masked[kind] = masked.get(kind, 0) + n
        return {"requests": len(rows), "bytes": sum(r.get("bytes", 0) for r in rows), "masked": masked}


def format_ledger(ledger: EgressLedger, limit: int = 20) -> str:
    rows = ledger.read(limit)
    if not rows:
        return "Nothing has been sent to a hosted model yet."
    lines = []
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
        if "route" in r:
            where = "left this PC" if r.get("left_pc") else "stayed on this PC"
            lines.append(f"{when}  route -> {r['route']} ({r['model']}): {r['reason']}  [{where}]")
            continue
        masked =", ".join(f"{n} {k}" for k, n in r.get("masked", {}).items()) or "nothing masked"
        lines.append(f"{when}  {r['host']}  {r['model']}  {r['messages']} msgs, {r['bytes']} B  [{masked}]  {r.get('outcome', '')}")
        if r.get("preview"):
            lines.append(f"    sent: {r['preview']}")
    total = ledger.summary()
    routes = [r for r in ledger.read(limit=10**9) if "route" in r]
    lines.append("")
    if routes:
        local_n = sum(1 for r in routes if not r.get("left_pc"))
        lines.append(f"Routing: {len(routes)} decisions, {local_n} stayed on this PC.")
    lines.append(f"Total: {total['requests']} requests, {total['bytes']} bytes. Masked so far: "
                 + (", ".join(f"{n} {k}" for k, n in total["masked"].items()) or "nothing"))
    return "\n".join(lines)
