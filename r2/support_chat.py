"""Self-contained Penny chat (no LLM): content-grounded answers, guided workflows, escalation, actions.

Mirrors the eGain hackathon requirements so the same experience can be demoed and tested locally.
"""
from __future__ import annotations

import re
from pathlib import Path

KB_DIR = Path(__file__).resolve().parent.parent / "docs" / "support-kb"
STOP = set("a an the is are do i my me to of for how what can you your it in on and or if at be".split())
MIN_SCORE = 0.34

ESCALATION = ("I can't answer that confidently. Options: (1) live chat with an agent, (2) request a callback "
              "(say 'callback'), (3) email demo@maplecu.example or call 555-0100.")
DECLINE = ("I can't give investment, legal or tax advice. " + ESCALATION)
RISKY = re.compile(r"\b(stock|invest|crypto|lawyer|sue|tax|close my account|hacked|takeover)\b", re.I)
HUMAN = re.compile(r"\b(human|agent|person|representative)\b", re.I)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOP}


def load_kb() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for path in sorted(KB_DIR.glob("*.md")):
        for block in re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.M)[1:]:
            q, _, a = block.partition("\n")
            entries.append((q.strip(), a.strip()))
    return entries


def answer(question: str, kb: list[tuple[str, str]]) -> str | None:
    qt = _tokens(question)
    if not qt:
        return None
    best, best_score = None, 0.0
    for q, a in kb:
        kt = _tokens(q) | _tokens(a)
        score = len(qt & kt) / len(qt)
        if score > best_score:
            best, best_score = a, score
    return best if best_score >= MIN_SCORE else None


class Session:
    """One visitor. `step` tracks the active workflow; `ctx` holds collected answers."""

    def __init__(self, name: str = "", topic: str = "", urgent: bool = False):
        self.name, self.topic, self.urgent = name, topic, urgent
        self.step = "lost_start" if urgent or topic == "lost_card" else None
        self.ctx: dict = {}
        self.misses = 0


def greet(s: Session) -> str:
    if s.step == "lost_start":
        return f"Hi {s.name or 'there'}, I'm sorry to hear that. Was your card lost or stolen?"
    return f"Hi {s.name or 'there'}, I'm Penny. Ask me about fees, cards or disputes."


def handle(s: Session, text: str, kb: list[tuple[str, str]], act) -> str:
    """`act(name, payload) -> dict` performs an action (the same logic as the HTTP endpoints)."""
    t = text.strip()
    if HUMAN.search(t):
        return ESCALATION
    if RISKY.search(t) and not s.step:
        return DECLINE

    if s.step == "lost_start":
        s.ctx["reason"] = "stolen" if "stol" in t.lower() else "lost"
        s.step = "lost_last4"
        return "Please give the last 4 digits of the card (never the full number or PIN)."
    if s.step == "lost_last4":
        m = re.search(r"\b(\d{4})\b", t)
        if not m or len(re.sub(r"\D", "", t)) > 4:
            return "Please send exactly 4 digits."
        s.ctx["last4"] = m.group(1)
        r = act("freeze_card", {"last4": s.ctx["last4"], "reason": s.ctx["reason"]})
        if not r.get("ok"):
            s.step = None
            return r["message"] + " " + ESCALATION
        s.step = "lost_speed"
        return r["message"] + " Replacement: standard (free, 5-7 days) or expedited ($15, 2 days)?"
    if s.step == "lost_speed":
        speed = "expedited" if "exp" in t.lower() else "standard"
        r = act("order_replacement", {"last4": s.ctx["last4"], "speed": speed})
        s.step = None
        return r["message"] + " Anything else?"

    if re.search(r"\b(callback|call me)\b", t, re.I):
        s.step = "cb_phone"
        return "What phone number should we call?"
    if s.step == "cb_phone":
        s.ctx["phone"] = t
        s.step = "cb_window"
        return "Morning, afternoon or evening?"
    if s.step == "cb_window":
        r = act("book_callback", {"name": s.name or "Member", "phone": s.ctx["phone"], "window": t.lower()})
        s.step = None
        return r["message"]

    if re.search(r"\b(lost|stolen|freeze)\b", t, re.I):
        s.step = "lost_start"
        return "Was your card lost or stolen?"

    a = answer(t, kb)
    if a:
        s.misses = 0
        return a
    s.misses += 1
    return ESCALATION if s.misses >= 1 else "Could you rephrase?"
