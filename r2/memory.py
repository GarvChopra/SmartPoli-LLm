from __future__ import annotations

import json
import re
import functools
import sqlite3
import threading
import time
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS facts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL, source TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS actions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL,
    result TEXT NOT NULL, approval TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activity(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, app TEXT NOT NULL);
"""

_SECRET_PATTERNS = [
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    re.compile(r"\b(?:password|passwd|pwd|passcode|pin)\b\s*(?:is|=|:)\s*\S+", re.I),
]


class SecretRejected(ValueError):
    pass


def _has_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


@dataclass(frozen=True)
class Fact:
    id: int
    text: str
    source: str
    ts: float


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


_STOP_WORDS = {
    "the", "and", "for", "you", "can", "please", "with", "this", "that", "from", "have", "what",
    "how", "are", "was", "not", "but", "all", "any", "get", "use", "into", "some", "your", "about",
}


def _meaningful(text: str) -> set[str]:
    return {w for w in _words(text) if len(w) >= 3 and w not in _STOP_WORDS}


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class Memory:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # --- chat history ---
    @_locked
    def add_message(self, role: str, content: str) -> None:
        self._db.execute(
            "INSERT INTO messages(role, content, ts) VALUES (?, ?, ?)",
            (role, redact(content), time.time()),
        )
        self._db.commit()

    @_locked
    def recent_messages(self, n: int = 20) -> list[dict]:
        rows = self._db.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # --- long-term facts ---
    @_locked
    def save_fact(self, text: str, source: str) -> int:
        if _has_secret(text):
            raise SecretRejected("looks like a password or card number")
        cur = self._db.execute(
            "INSERT INTO facts(text, source, ts) VALUES (?, ?, ?)",
            (text, source, time.time()),
        )
        self._db.commit()
        return int(cur.lastrowid)

    @_locked
    def update_fact(self, fact_id: int, text: str, source: str) -> bool:
        if _has_secret(text):
            raise SecretRejected("looks like a password or card number")
        cur = self._db.execute("UPDATE facts SET text = ?, source = ?, ts = ? WHERE id = ?", (text, source, time.time(), fact_id))
        self._db.commit()
        return cur.rowcount > 0

    @_locked
    def list_facts(self) -> list[Fact]:
        rows = self._db.execute("SELECT * FROM facts ORDER BY id").fetchall()
        return [Fact(r["id"], r["text"], r["source"], r["ts"]) for r in rows]

    @_locked
    def search_facts(self, query: str, limit: int = 5) -> list[Fact]:
        wanted = {w for w in _words(query) if len(w) >= 3}
        scored = []
        for fact in self.list_facts():
            score = len(wanted & _words(fact.text))
            if score:
                scored.append((score, fact.id, fact))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [f for _, _, f in scored[:limit]]

    @_locked
    def delete_fact(self, fact_id: int) -> bool:
        cur = self._db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        self._db.commit()
        return cur.rowcount > 0

    # --- action log ---
    @_locked
    def log_action(self, tool: str, args: dict, result: str, approval: str) -> None:
        self._db.execute(
            "INSERT INTO actions(ts, tool, args, result, approval) VALUES (?, ?, ?, ?, ?)",
            (
                time.time(),
                tool,
                redact(json.dumps(args, default=str)),
                redact(result[:2000]),
                approval,
            ),
        )
        self._db.commit()

    @_locked
    def list_actions(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT ts, tool, args, result, approval FROM actions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- learning from the user's own history ---
    @_locked
    def similar_requests(self, query: str, limit: int = 3) -> list[dict]:
        """Past requests that look like `query`, with the tools that were used for them.
        This is how R2 picks up the user's habits and phrasing: retrieval, not model training."""
        wanted = _meaningful(query)
        if not wanted:
            return []
        need = min(2, len(wanted))
        rows = self._db.execute("SELECT id, content, ts FROM messages WHERE role = 'user' ORDER BY id").fetchall()
        best: dict[str, tuple] = {}
        for i, row in enumerate(rows):
            score = len(wanted & _meaningful(row["content"]))
            if score < need:
                continue
            key = row["content"]
            if key not in best or row["id"] > best[key][1]:
                end = rows[i + 1]["ts"] if i + 1 < len(rows) else float("inf")
                best[key] = (score, row["id"], row["ts"], end)
        ranked = sorted(best.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)[:limit]
        out = []
        for text, (_, _, start, end) in ranked:
            acts = self._db.execute(
                "SELECT tool FROM actions WHERE ts >= ? AND ts < ? ORDER BY id", (start, end)
            ).fetchall()
            tools: list[str] = []
            for a in acts:
                if a["tool"] not in tools:
                    tools.append(a["tool"])
            out.append({"request": text, "tools": tools, "ts": start})
        return out

    @_locked
    def clear_history(self) -> None:
        """Forget what was said and done (facts are kept; delete those one by one)."""
        self._db.execute("DELETE FROM messages")
        self._db.execute("DELETE FROM actions")
        self._db.execute("DELETE FROM activity")
        self._db.commit()

    # --- routines (opt-in activity learning) ---
    @_locked
    def log_activity(self, app: str, ts: float | None = None) -> None:
        self._db.execute("INSERT INTO activity(ts, app) VALUES (?, ?)", (ts if ts is not None else time.time(), app))
        self._db.commit()

    @_locked
    def usual_apps(self, hour: int, limit: int = 3, days: int = 30, now: float | None = None, min_count: int = 3) -> list[str]:
        """Programs most often in front at this hour of the day over the last `days` days."""
        now = now if now is not None else time.time()
        rows = self._db.execute("SELECT ts, app FROM activity WHERE ts >= ?", (now - days * 86400,)).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            if time.localtime(row["ts"]).tm_hour == hour:
                counts[row["app"]] = counts.get(row["app"], 0) + 1
        ranked = sorted((c, a) for a, c in counts.items() if c >= min_count)
        ranked.sort(key=lambda ca: (-ca[0], ca[1]))
        return [a for _, a in ranked[:limit]]
