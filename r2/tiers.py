"""Tiered model routing: the right-sized model for each request, decided by plain code, never by a model.

  local  a private request (the gateway found a secret in it) stays on this PC, if you asked for that
  fast   plain conversation goes to a small model (Nemotron Nano class); see make_chat_brain
  act    tasks that use tools go to the main model (Nemotron Super class)
  deep   hard, many-step or long requests escalate to the biggest model (Nemotron Ultra class)

Every choice and its reason is written to the egress ledger, so "why did this model answer?" and
"did this leave my PC?" both have a visible answer. If a hosted model fails, a local model (when there
is one) finishes the job, so R2 keeps working without the network or credits."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from r2.brain import Brain, BrainError
from r2.privacy import EgressLedger, Gateway
from r2.types import Decision

_DEEP_WORDS = re.compile(
    r"\b(plan|analy[sz]e|analysis|compare|comparison|research|strategy|summari[sz]e|review|debug|investigate|"
    r"design|architecture|draft|write\s+(?:a|an|the)\s+\w+|step[- ]by[- ]step|pros\s+and\s+cons|"
    r"organi[sz]e|reconcile|audit)\b",
    re.I,
)
DEEP_LENGTH = 400  # characters in the request
LOCAL_RECHECK_SECONDS = 20  # how long "is the local model running?" is remembered
DEEP_AFTER_TOOL_CALLS = 6  # a task this long gets the biggest model for the rest of its steps


@dataclass(frozen=True)
class Choice:
    tier: str  # "local" | "act" | "deep"
    reason: str


def _last_user_index(messages: list[dict]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return -1


def classify(messages: list[dict], private_local: bool, has_local: bool, has_deep: bool) -> Choice:
    """Pure function: the same messages always get the same choice."""
    if private_local and has_local:
        # Only the current turn decides: the newest request and the tool results since. Older chat history
        # and the system prompt are still masked before anything is sent, but they do not pin every later
        # request to the local model.
        i = _last_user_index(messages)
        turn = [m for m in (messages[i:] if i >= 0 else messages) if m.get("role") in ("user", "tool")]
        found = Gateway().prepare(turn).counts
        if found:
            kinds = ", ".join(sorted(found))
            return Choice("local", f"sensitive data found ({kinds}); kept on this PC")
    if has_deep:
        i = _last_user_index(messages)
        request = messages[i].get("content", "") if i >= 0 else ""
        tool_steps = sum(1 for m in messages[i + 1:] if m.get("role") == "tool") if i >= 0 else 0
        if _DEEP_WORDS.search(request or ""):
            return Choice("deep", "request asks for planning or analysis")
        if len(request or "") > DEEP_LENGTH:
            return Choice("deep", f"long request (over {DEEP_LENGTH} characters)")
        if tool_steps >= DEEP_AFTER_TOOL_CALLS:
            return Choice("deep", f"long task ({tool_steps} tool steps so far)")
    return Choice("act", "task that uses tools")


class TieredBrain:
    hosted = True

    def __init__(
        self,
        act: Brain,
        deep: Brain | None = None,
        local: Brain | None = None,
        private_local: bool = True,
        ledger: EgressLedger | None = None,
        names: dict[str, str] | None = None,
        local_check: Callable[[], bool] | None = None,
    ):
        self._brains = {"act": act, "deep": deep, "local": local}
        self._local_check = local_check  # asked at request time, so starting Ollama later just works
        self._local_seen = (0.0, False)
        self._private_local = private_local
        self._ledger = ledger
        self._names = names or {}
        self.last_choice: Choice | None = None

    def _local_ok(self) -> bool:
        if self._brains["local"] is None:
            return False
        if self._local_check is None:
            return True
        when, ok = self._local_seen
        if time.monotonic() - when > LOCAL_RECHECK_SECONDS or when == 0.0:
            ok = bool(self._local_check())
            self._local_seen = (time.monotonic(), ok)
        return ok

    def _log(self, choice: Choice, fallback: bool = False) -> None:
        self.last_choice = choice
        if self._ledger is not None:
            self._ledger.append(
                {
                    "route": choice.tier,
                    "model": self._names.get(choice.tier, choice.tier),
                    "reason": choice.reason,
                    "left_pc": choice.tier != "local",
                    "fallback": fallback,
                }
            )

    def decide(self, messages, tools, on_token=None, should_stop=None) -> Decision:
        choice = classify(messages, self._private_local, self._local_ok(), self._brains["deep"] is not None)
        self._log(choice)
        brain = self._brains[choice.tier] or self._brains["act"]
        if choice.tier == "local":
            try:
                return brain.decide(messages, tools, on_token=on_token, should_stop=should_stop)
            except BrainError as exc:
                # A private request is never sent out just because the local model is unavailable.
                raise BrainError(f"This request contains private data, so it was not sent to a hosted model. {exc}") from None
        try:
            return brain.decide(messages, tools, on_token=on_token, should_stop=should_stop)
        except BrainError as exc:
            local = self._brains["local"] if self._local_ok() else None
            if local is None:
                raise
            self._log(Choice("local", f"hosted model failed ({str(exc)[:80]}); finished on this PC"), fallback=True)
            return local.decide(messages, tools, on_token=on_token, should_stop=should_stop)
