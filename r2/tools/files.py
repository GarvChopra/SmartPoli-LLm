from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from r2.types import Risk, RiskInfo


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def list_dir(path: str) -> str:
    """The entries of a folder, preceded by exact counts: small models count badly, so the
    tool does the counting."""
    entries = sorted(Path(path).iterdir(), key=lambda p: p.name.lower())
    folders = sum(p.is_dir() for p in entries)
    header = f"{_plural(len(entries), 'item')} ({_plural(len(entries) - folders, 'file')}, {_plural(folders, 'folder')}):"
    lines = [p.name + ("/" if p.is_dir() else "") for p in entries]
    return "\n".join([header, *lines])


def read_file(path: str, max_chars: int = 4000) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated, {len(text)} chars total]"
    return text


def find_files(root: str, pattern: str, limit: int = 50) -> str:
    pattern = pattern.lower()
    found: list[str] = []
    hit_limit = False
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if fnmatch.fnmatch(name.lower(), pattern):
                found.append(os.path.join(dirpath, name))
                if len(found) >= limit:
                    hit_limit = True
                    break
        if hit_limit:
            break
    if not found:
        return "No matches"
    noun = "match" if len(found) == 1 else "matches"
    header = f"{len(found)} {noun} (stopped at the limit of {limit}):" if hit_limit else f"{len(found)} {noun}:"
    return "\n".join([header, *found])


def write_file(path: str, content: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {p}"


def delete_file(path: str) -> str:
    p = Path(path)
    if p.is_dir():
        raise IsADirectoryError(f"{p} is a directory; only single files can be deleted")
    p.unlink()
    return f"Deleted {p}"


_SENSITIVE_NAMES = {
    ".env", "tokens.json", "overlay.token", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".npmrc", ".pypirc", ".git-credentials", ".netrc", "credentials", "credentials.json",
    "login data", "cookies", "web data", "key4.db", "logins.json",
}
_SENSITIVE_EXT = {".pem", ".key", ".kdbx", ".pfx", ".p12", ".ppk", ".keystore"}
_SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", ".azure", ".kube", "browser-profile", "user data"}


def classify_read(args: dict) -> RiskInfo:
    """Reading is normally safe, but a web page that hijacks the agent could read a secret
    and send it out in a URL. Secret-looking files therefore need approval."""
    parts = [p for p in str(args.get("path", "")).replace("\\", "/").lower().split("/") if p]
    name = parts[-1] if parts else ""
    if (
        name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or Path(name).suffix in _SENSITIVE_EXT
        or any(p in _SENSITIVE_DIRS for p in parts[:-1])
    ):
        return RiskInfo(Risk.CONFIRM, "sensitive_read")
    return RiskInfo(Risk.SAFE)
