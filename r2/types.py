from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Risk(IntEnum):
    SAFE = 0
    CONFIRM = 1
    BLOCKED = 2


@dataclass(frozen=True)
class RiskInfo:
    risk: Risk
    # e.g. "submit", "post", "send", "delete", "shell", "payment", "password"
    action_type: str | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict


@dataclass(frozen=True)
class Decision:
    tool_call: ToolCall | None = None
    final: str | None = None


@dataclass
class AgentResult:
    status: str  # "done" | "stopped" | "step_limit" | "failed"
    answer: str
    steps: int  # number of tool calls executed
