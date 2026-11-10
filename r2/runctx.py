"""Per-run context for tools. A tool that starts a long job (such as Claude Code) reads the current
Stop event here so pressing Stop can end it."""
from __future__ import annotations

import threading
from contextvars import ContextVar

current_stop: ContextVar[threading.Event | None] = ContextVar("r2_current_stop", default=None)
