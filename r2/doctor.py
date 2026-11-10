"""`r2 doctor` (is everything set up and fast?) and the timing helper behind `r2 bench`.

The idea of a built-in diagnostics command and measuring latency as a first-class number
comes from reading the OpenJarvis README (Apache-2.0); this is an independent implementation."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from r2.config import Settings


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str = ""


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def has_browser() -> bool:
    if shutil.which("chrome") or shutil.which("msedge"):
        return True
    roots = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    candidates = [
        Path(r) / sub
        for r in roots
        if r
        for sub in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe")
    ]
    return any(p.exists() for p in candidates)


def _names(response: httpx.Response) -> set[str]:
    out = set()
    for m in response.json().get("models", []):
        name = m.get("name", "")
        out.add(name)
        if name.endswith(":latest"):
            out.add(name[: -len(":latest")])
    return out


def claude_version() -> str | None:
    """The installed Claude Code version, or None."""
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        done = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    return done.stdout.split()[0] if done.returncode == 0 and done.stdout.strip() else None


def run_checks(
    settings: Settings, http_get=httpx.get, env=None, has_module=has_module, has_browser=has_browser, claude_version=None
) -> list[Check]:
    env = os.environ if env is None else env
    checks: list[Check] = []

    if settings.brain == "cloud":
        if settings.anthropic_api_key:
            checks.append(Check("Cloud brain", "ok", f"using {settings.cloud_model}"))
        else:
            checks.append(Check("Cloud brain", "fail", "R2_BRAIN=cloud but ANTHROPIC_API_KEY is not set in .env"))

    if settings.brain == "openai":
        base, key, model = settings.openai_base_url, settings.openai_api_key, settings.openai_model
        if not (base and key and model):
            checks.append(Check("Cloud model", "fail", "not configured. Run: r2 cloud"))
        else:
            try:
                status = http_get(base.rstrip("/") + "/models", timeout=8, headers={"Authorization": f"Bearer {key}"}).status_code
                if status in (401, 403):
                    checks.append(Check("Cloud model", "fail", "the provider rejected the API key. Run: r2 cloud"))
                elif status >= 400:
                    checks.append(Check("Cloud model", "warn", f"the provider answered HTTP {status}"))
                else:
                    checks.append(Check("Cloud model", "ok", f"{model} via {base}"))
            except Exception:
                checks.append(Check("Cloud model", "warn", "could not reach the provider (offline?)"))
            checks.append(Check(
                "Model tiers", "ok" if (settings.fast_model or settings.deep_model) else "warn",
                f"chat -> {settings.fast_model or 'main model'}, tasks -> {model}, hard requests -> {settings.deep_model or 'main model'}"
                + ("" if (settings.fast_model or settings.deep_model) else " (run r2 cloud to set up Nano / Ultra tiers)"),
            ))
            checks.append(Check(
                "Privacy gateway", "ok",
                "secrets are masked before sending; "
                + ("private requests stay on this PC when Ollama is running" if settings.private_local
                   else "R2_PRIVATE_LOCAL=0, so private requests are masked and sent")
                + ". See what was sent: r2 ledger",
            ))

    models: list[tuple[str, str]] = []
    if settings.brain in ("ollama", "openai") and settings.chat_model and (
        settings.brain == "openai" or settings.chat_model != settings.model
    ):
        models.append((settings.chat_model, "Chat model"))
    if settings.brain == "ollama":
        models.append((settings.model, "Tool model"))
    if models:
        base = settings.ollama_url.rstrip("/")
        try:
            installed = _names(http_get(base + "/api/tags", timeout=3))
            checks.append(Check("Ollama server", "ok", "running"))
        except Exception:
            installed = None
            checks.append(Check("Ollama server", "fail", "not reachable. Start it with: ollama serve (the R2 daemon does this for you)"))
        if installed is not None:
            for model, label in models:
                if model in installed:
                    checks.append(Check(label, "ok", model))
                else:
                    checks.append(Check(label, "fail", f"{model} is not installed. Run: ollama pull {model}"))
            try:
                loaded = _names(http_get(base + "/api/ps", timeout=3))
                missing = [m for m, _ in models if m not in loaded]
                if missing:
                    checks.append(Check("Models in memory", "warn",
                                        f"not loaded yet: {', '.join(missing)} (the first request that needs it will be slow)"))
                else:
                    checks.append(Check("Models in memory", "ok", "all loaded"))
            except Exception:
                checks.append(Check("Models in memory", "warn", "could not ask Ollama what is loaded"))
        if len(models) > 1:
            raw = env.get("OLLAMA_MAX_LOADED_MODELS", "")
            if raw.isdigit() and int(raw) >= 2:
                checks.append(Check("Ollama keeps 2 models loaded", "ok", f"OLLAMA_MAX_LOADED_MODELS={raw}"))
            else:
                checks.append(Check("Ollama keeps 2 models loaded", "warn",
                                    f"OLLAMA_MAX_LOADED_MODELS is {raw or 'not set'}: the chat and tool models will swap "
                                    "in and out of memory (slow). Fix: setx OLLAMA_MAX_LOADED_MODELS 2, then restart Ollama"))

    try:
        http_get(f"http://localhost:{settings.port}/", timeout=2)
        checks.append(Check("R2 server", "ok", f"running on port {settings.port}"))
    except Exception:
        checks.append(Check("R2 server", "warn", "not running (start it with: r2 daemon)"))

    checks.append(
        Check("Browser for web tasks", "ok", "Chrome or Edge found")
        if has_browser()
        else Check("Browser for web tasks", "warn", "Chrome or Edge not found; browser tools will not work")
    )

    if settings.claude and claude_version is not None:
        version = claude_version()
        checks.append(
            Check("Claude Code", "ok", f"{version} found; 'ask claude to ...' uses your Claude plan")
            if version
            else Check("Claude Code", "warn", "not found, so 'ask claude to ...' will not work. Install it from https://claude.com/claude-code")
        )

    if not settings.voice:
        checks.append(Check("Voice packages", "ok", "voice is off"))
    else:
        needed = ["sounddevice", "faster_whisper"] + (["openwakeword"] if settings.wake_model else [])
        missing = [m for m in needed if not has_module(m)]
        checks.append(
            Check("Voice packages", "warn", f'missing {", ".join(missing)}. Run: pip install -e ".[voice]"')
            if missing
            else Check("Voice packages", "ok", "installed")
        )
    return checks


def format_report(checks: list[Check]) -> str:
    tags = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
    lines = [f"{tags[c.status]} {c.name}" + (f" - {c.detail}" if c.detail else "") for c in checks]
    problems = sum(c.status == "fail" for c in checks)
    warnings = sum(c.status == "warn" for c in checks)
    if problems or warnings:
        lines.append(f"\n{problems} problem{'s' if problems != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''}")
    else:
        lines.append("\nEverything looks good")
    return "\n".join(lines)


def time_brain(brain, prompt: str, clock=time.time) -> tuple[float, float, float]:
    """(seconds to first word, total seconds, words per second) for one reply."""
    start = clock()
    state = {"first": None, "count": 0}

    def on_token(piece: str) -> None:
        if state["first"] is None:
            state["first"] = clock() - start
        state["count"] += 1

    brain.decide([{"role": "user", "content": prompt}], [], on_token=on_token)
    total = clock() - start
    first = state["first"] if state["first"] is not None else total
    return first, total, state["count"] / max(total - first, 1e-9)
