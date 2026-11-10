from __future__ import annotations

import re
from enum import Enum

from r2.types import Risk, RiskInfo


class Verdict(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


# Only these action types can ever be granted by the user's own words.
# "post" and "send" need a verb form so "fill this post" or "send me a summary"
# does not pre-approve anything.
_GRANT_PATTERNS = {
    "submit": re.compile(r"\bsubmit(?:ting)?\b"),
    "post": re.compile(r"\b(?:(?:and|then|also)\s+post|post\s+it)\b"),
    "send": re.compile(r"\b(?:(?:and|then|also)\s+send|send\s+it)\b"),
    # naming Claude explicitly is the user's own go-ahead to hand the task to Claude Code
    "delegate": re.compile(r"\b(?:ask|tell|let|use)\s+claude\b|\bclaude\s*[,:]"),
}
_NEGATIONS = frozenset(
    {"not", "dont", "don't", "never", "without", "wont", "won't", "cant", "can't"}
)


def parse_grants(message: str) -> frozenset[str]:
    text = message.lower().replace("’", "'")
    grants: set[str] = set()
    for action_type, pattern in _GRANT_PATTERNS.items():
        for match in pattern.finditer(text):
            before = re.findall(r"[a-z']+", text[: match.start()])[-3:]
            if not _NEGATIONS.intersection(before):
                grants.add(action_type)
    return frozenset(grants)


class Gate:
    """One Gate per task. It is built from the user's own message only,
    never from tool output, so web pages and files cannot create grants."""

    def __init__(self, user_message: str):
        self._grants = parse_grants(user_message)

    def evaluate(self, info: RiskInfo | None) -> Verdict:
        if info is None:
            return Verdict.ASK
        if info.risk is Risk.BLOCKED:
            return Verdict.DENY
        if info.risk is Risk.SAFE:
            return Verdict.ALLOW
        if info.action_type in self._grants:
            return Verdict.ALLOW
        return Verdict.ASK
