from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
CODE_TTL = 600  # seconds
MAX_FAILURES = 10
LOCKOUT = 60  # seconds
MAX_DEMO_TOKENS = 500


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthStore:
    """One-time pairing codes -> long-lived device tokens.

    Codes and tokens live in one JSON file as SHA-256 hashes only, so the CLI
    (`r2 pair`) and the running server can share them across processes."""

    def __init__(self, path, clock=time.time, static_code: str = ""):
        self._static = _hash(static_code.strip().upper()) if static_code.strip() else ""
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._locked_until = 0.0

    # ---- pairing ----
    def new_pairing_code(self) -> str:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        with self._lock:
            data = self._load()
            data["codes"][_hash(code)] = self._clock() + CODE_TTL
            self._save(data)
        return code

    def redeem(self, code) -> str | None:
        if not isinstance(code, str):
            return None
        wanted = _hash(code.strip().upper())
        with self._lock:
            now = self._clock()
            if now < self._locked_until:
                return None
            data = self._load()
            if self._static and hmac.compare_digest(self._static, wanted):
                # A fixed access code (hosted demo only): hands out a fresh token every time, never used up.
                self._failures = 0
                token = secrets.token_urlsafe(32)
                data["tokens"].append({"hash": _hash(token), "created": now})
                data["tokens"] = data["tokens"][-MAX_DEMO_TOKENS:]  # oldest visitors drop off; the file cannot grow forever
                self._save(data)
                return token
            data["codes"] = {h: exp for h, exp in data["codes"].items() if exp >= now}
            match = next((h for h in data["codes"] if hmac.compare_digest(h, wanted)), None)
            if match is None:
                self._failures += 1
                if self._failures >= MAX_FAILURES:
                    self._locked_until = now + LOCKOUT
                    self._failures = 0
                self._save(data)
                return None
            del data["codes"][match]
            self._failures = 0
            token = secrets.token_urlsafe(32)
            data["tokens"].append({"hash": _hash(token), "created": now})
            self._save(data)
            return token

    # ---- tokens ----
    def verify(self, token) -> bool:
        if not isinstance(token, str) or not token:
            return False
        digest = _hash(token)
        with self._lock:
            return any(hmac.compare_digest(digest, t["hash"]) for t in self._load()["tokens"])

    def revoke_all(self) -> None:
        with self._lock:
            self._save({"tokens": [], "codes": {}})

    def count(self) -> int:
        with self._lock:
            return len(self._load()["tokens"])

    # ---- storage ----
    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return {"tokens": list(data.get("tokens", [])), "codes": dict(data.get("codes", {}))}
        except (OSError, ValueError, AttributeError):
            return {"tokens": [], "codes": {}}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self._path)
