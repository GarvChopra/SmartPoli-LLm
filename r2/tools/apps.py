from __future__ import annotations

import difflib
import logging
import re
import shutil
import subprocess
import time

from r2.types import Risk, RiskInfo

log = logging.getLogger("r2.apps")

_PLAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_DANGEROUS_EXT = {
    "bat", "cmd", "ps1", "vbs", "vbe", "js", "jse", "wsf", "msi", "reg", "scr", "com", "hta", "lnk",
}
_SHELL_LIKE = {
    "cmd", "powershell", "pwsh", "wt", "regedit", "wscript", "cscript", "mshta", "bash", "wsl",
}


def classify_app(args: dict) -> RiskInfo:
    name = str(args.get("name", ""))
    if not _PLAIN_NAME.match(name):
        return RiskInfo(Risk.CONFIRM, "app")
    lowered = name.lower()
    base, _, ext = lowered.rpartition(".")
    if not base:
        base, ext = lowered, ""
    if ext in _DANGEROUS_EXT or base in _SHELL_LIKE or lowered in _SHELL_LIKE:
        return RiskInfo(Risk.CONFIRM, "app")
    return RiskInfo(Risk.SAFE)


# ---------------------------------------------------------------------------
# Finding installed apps. Windows' Start menu list (Get-StartApps) covers classic programs AND
# Microsoft Store apps such as WhatsApp, which `start <name>` cannot launch.
# ---------------------------------------------------------------------------
_cache: dict = {"apps": None, "at": 0.0}
_CACHE_SECONDS = 600


def parse_start_apps(text: str) -> list[tuple[str, str]]:
    apps = []
    for line in text.splitlines():
        name, sep, app_id = line.rpartition("|")
        if sep and name.strip() and app_id.strip():
            apps.append((name.strip(), app_id.strip()))
    return apps


def _query_start_apps() -> list[tuple[str, str]]:
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"
    )
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=0x08000000,  # no console window
    )
    return parse_start_apps(done.stdout)


def installed_apps() -> list[tuple[str, str]]:
    """(name, app id) of every Start-menu app. Cached, so opening an app is instant."""
    if _cache["apps"] is not None and time.time() - _cache["at"] < _CACHE_SECONDS:
        return _cache["apps"]
    try:
        apps = _query_start_apps()
    except Exception:
        log.exception("could not list installed apps")
        return []
    _cache["apps"], _cache["at"] = apps, time.time()
    return apps


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


_ALIASES = {
    "vs code": "visual studio code", "vscode": "visual studio code", "wa": "whatsapp",
    "ppt": "powerpoint", "msword": "word", "ms word": "word", "edge": "microsoft edge",
}


def find_app(typed: str, apps: list[tuple[str, str]], cutoff: float = 0.82) -> tuple[str, str] | None:
    """The installed app that best matches what the user typed (loose: case, typos, part of a name)."""
    typed = _ALIASES.get(typed.strip().lower(), typed)
    wanted = _norm(typed)
    if len(wanted) < 2:
        return None
    best, best_score = None, 0.0
    for name, app_id in apps:
        norm = _norm(name)
        if name.lower() == typed.strip().lower():
            score = 101.0
        elif norm == wanted:
            score = 100.0
        elif norm.startswith(wanted):
            score = 90.0 - min(len(norm) - len(wanted), 40) / 10
        elif len(wanted) >= 3 and wanted in norm:
            score = 70.0 - min(len(norm) - len(wanted), 40) / 10
        else:
            ratio = difflib.SequenceMatcher(None, wanted, norm).ratio()
            score = ratio * 60 if ratio >= cutoff else 0.0
        if score > best_score:
            best, best_score = (name, app_id), score
    return best


def open_app(name: str, installed=installed_apps, which=shutil.which, launcher=subprocess.Popen) -> str:
    """Open an app, but only claim success if something was really launched.
    Raises FileNotFoundError (and launches nothing) when no such app exists."""
    if not _PLAIN_NAME.match(name):
        raise ValueError(
            "app name may only contain letters, digits, spaces, dots, dashes and underscores"
        )
    apps = installed()
    hit = find_app(name, apps)
    if hit:
        display, app_id = hit
        launcher(["explorer.exe", "shell:AppsFolder\\" + app_id])
        return f"Opened {display}"
    path = which(name) or which(name + ".exe")
    if path:
        launcher([path])
        return f"Opened {name}"
    names = [n for n, _ in apps]
    similar = difflib.get_close_matches(name.lower(), [n.lower() for n in names], n=3, cutoff=0.5)
    pretty = [next(n for n in names if n.lower() == s) for s in similar]
    message = f"No app called '{name}' is installed."
    if pretty:
        message += " Similar: " + ", ".join(pretty) + "."
    raise FileNotFoundError(message)
