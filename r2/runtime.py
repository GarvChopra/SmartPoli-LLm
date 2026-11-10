from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from r2.agent import Agent
from r2.auth import AuthStore
from r2.brain import Brain, make_brain, make_chat_brain
from r2.claude_bridge import find_claude
from r2.config import Settings
from r2.memory import Memory
from r2.tools import ToolRegistry, full_registry
from r2.types import ToolCall

APPROVAL_TIMEOUT = 120.0  # seconds


class TaskBusy(Exception):
    pass


def _display_args(args: dict) -> dict:
    out = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 300:
            value = value[:300] + "..."
        out[key] = value
    return out


class _Pending:
    def __init__(self, call: ToolCall):
        self.id = uuid.uuid4().hex[:10]
        self.tool = call.name
        self.args = _display_args(call.args)
        self.event = threading.Event()
        self.decision: bool | None = None


class ApprovalBroker:
    """Blocks the agent thread until the user approves or denies, or the request expires."""

    def __init__(self, timeout: float = APPROVAL_TIMEOUT):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._current: _Pending | None = None

    def request(self, call: ToolCall) -> bool:
        pending = _Pending(call)
        with self._lock:
            self._current = pending
        pending.event.wait(self.timeout)
        with self._lock:
            if self._current is pending:
                self._current = None
        return pending.decision is True

    def pending(self) -> dict | None:
        with self._lock:
            p = self._current
            if p is None or p.event.is_set():
                return None
            return {"id": p.id, "tool": p.tool, "args": p.args, "timeout": self.timeout}

    def resolve(self, approval_id: str, approve: bool) -> bool:
        with self._lock:
            p = self._current
            if p is None or p.id != approval_id or p.event.is_set():
                return False
            p.decision = bool(approve)
            p.event.set()
            return True

    def cancel(self) -> None:
        with self._lock:
            p = self._current
            if p is not None and not p.event.is_set():
                p.decision = False
                p.event.set()


class TaskManager:
    def __init__(self, agent: Agent, broker: ApprovalBroker):
        self.agent = agent
        self.broker = broker
        self.paused = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._task_id: str | None = None
        self._message: str | None = None
        self._result: dict | None = None
        self._partial = ""

    def start(self, message: str) -> str:
        with self._lock:
            if self.paused:
                raise TaskBusy("R2 is paused")
            if self._running:
                raise TaskBusy("a task is already running")
            self._task_id = uuid.uuid4().hex[:8]
            self._message = message
            self._result = None
            self._partial = ""
            self._running = True
            self._stop = threading.Event()
            task_id, stop = self._task_id, self._stop
            self._thread = threading.Thread(
                target=self._run, args=(message, stop), name=f"r2-task-{task_id}", daemon=True
            )
            self._thread.start()
            return task_id

    def _run(self, message: str, stop: threading.Event) -> None:
        try:
            r = self.agent.run(message, stop=stop, on_token=self._on_token)
            result = {"status": r.status, "answer": r.answer, "steps": r.steps}
        except Exception as exc:  # never let a task crash the server
            result = {"status": "failed", "answer": f"{type(exc).__name__}: {exc}", "steps": 0}
        with self._lock:
            self._result = result
            self._partial = ""
            self._running = False

    def _on_token(self, piece: str) -> None:
        with self._lock:
            self._partial = (self._partial + piece)[-4000:]

    def stop(self) -> None:
        self._stop.set()
        self.broker.cancel()

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self._running

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "task_id": self._task_id,
                "message": self._message,
                "result": self._result,
                "partial": self._partial,
                "pending": self.broker.pending(),
                "paused": self.paused,
            }


@dataclass
class Runtime:
    settings: Settings
    memory: Memory
    registry: ToolRegistry
    broker: ApprovalBroker
    tasks: TaskManager
    auth: AuthStore


def build_runtime(
    settings: Settings, brain: Brain | None = None, registry: ToolRegistry | None = None
) -> Runtime:
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = settings.db_path
    if db_path.parent == Path("."):
        db_path = data_dir / db_path.name
    memory = Memory(db_path)
    registry = registry or full_registry(
        memory, data_dir, settings.headless,
        claude_dirs=settings.claude_dirs,
        claude_finder=find_claude if settings.claude else (lambda: None),
        profile=settings.profile,
    )
    broker = ApprovalBroker()
    # The real brain gets the fast chat path and tool filtering; an injected brain (tests) is used as-is.
    real = brain is None
    agent = Agent(
        brain or make_brain(settings),
        registry,
        memory,
        broker.request,
        settings.max_steps,
        chat_brain=make_chat_brain(settings) if real else None,
        smart_routing=real,
    )
    return Runtime(
        settings, memory, registry, broker, TaskManager(agent, broker), AuthStore(data_dir / "tokens.json", static_code=settings.demo_code if settings.profile == "demo" else "")
    )
