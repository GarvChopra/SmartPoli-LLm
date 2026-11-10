from __future__ import annotations

import re
import subprocess

from r2.types import Risk, RiskInfo

_BLOCKED = [
    r"\bformat(\.com)?\s+[a-z]:",
    r"\bdiskpart\b",
    r"\b(rd|rmdir)\s+/s\b.*\b[a-z]:\\?(\s|$)",
    r"\b(rd|rmdir|del|erase|remove-item)\b.*[a-z]:\\windows",
    r"\bremove-item\b.*-recurse.*\b[a-z]:\\?(\s|$)",
    r"\breg(\.exe)?\s+delete\s+hk(lm|ey_local_machine)",
    r"\bbcdedit\b",
    r"\bset-mppreference\b",
    r"\bnetsh\s+advfirewall\s+set\s+\w+\s+state\s+off",
    r"\bcipher\s+/w",
]
_BLOCKED_RE = [re.compile(p, re.I) for p in _BLOCKED]


def classify_shell(args: dict) -> RiskInfo:
    command = str(args.get("command", ""))
    if any(p.search(command) for p in _BLOCKED_RE):
        return RiskInfo(Risk.BLOCKED, "shell")
    return RiskInfo(Risk.CONFIRM, "shell")


def run_shell(command: str, timeout: int = 30) -> str:
    proc = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="oem",
        errors="replace",
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        proc.communicate()
        raise TimeoutError(f"command timed out after {timeout}s") from None
    out = out.strip()
    if len(out) > 4000:
        out = out[:4000] + "\n...[truncated]"
    return f"{out}\n[exit code {proc.returncode}]"
