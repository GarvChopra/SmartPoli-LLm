"""Which tools R2 may use: you choose.

A tool you switch off is removed from R2 entirely: the model is never told it exists, and a call to it
fails as "unknown tool". The choice lives in `<data>/policy.json`, a file you can read and edit.

R2_PROFILE=demo switches off everything that touches the machine (files, shell, apps, screen, browser)
and keeps only memory, skills and web search. That is the profile used for the public hosted demo."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEMO_TOOLS = frozenset({"remember", "use_skill", "propose_skill", "web_search", "web_fetch"})


def policy_path(data_dir) -> Path:
    return Path(data_dir) / "policy.json"


def load_disabled(data_dir) -> set[str]:
    try:
        raw = json.loads(policy_path(data_dir).read_text(encoding="utf-8"))
        return {str(name) for name in raw.get("disabled", [])}
    except (OSError, ValueError, AttributeError):
        return set()


def save_disabled(data_dir, disabled: set[str]) -> None:
    path = policy_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"disabled": sorted(disabled)}, indent=2) + "\n", encoding="utf-8")


def apply_policy(registry, data_dir, profile: str | None = None) -> list[str]:
    """Remove disabled tools from the registry; returns their names."""
    profile = (profile if profile is not None else os.environ.get("R2_PROFILE", "")).strip().lower()
    names = {spec_name for spec_name in registry._tools}
    disabled = load_disabled(data_dir) & names
    if profile == "demo":
        disabled |= names - DEMO_TOOLS
    for name in disabled:
        registry._tools.pop(name, None)
    return sorted(disabled)
