"""`r2 cloud`: connect R2 to a hosted model in one guided step.

It asks which provider, takes your API key without showing it, asks the provider which models that
key can actually use (so no stale model names), lets you pick one, and writes the settings to `.env`.
The key is only ever written to `.env`; it is never printed, logged or kept in a chat."""
from __future__ import annotations

import getpass
from pathlib import Path

import httpx

PROVIDERS = [
    {"name": "Groq (free tier, very fast)", "base": "https://api.groq.com/openai/v1",
     "prefer": ["llama-3.3-70b", "70b", "llama"], "keys": "https://console.groq.com/keys"},
    {"name": "Google Gemini (free tier)", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
     "prefer": ["flash"], "keys": "https://aistudio.google.com/apikey"},
    {"name": "OpenRouter (many models, some free)", "base": "https://openrouter.ai/api/v1",
     "prefer": [":free", "llama"], "keys": "https://openrouter.ai/keys"},
    {"name": "Nebius Token Factory (NVIDIA Nemotron, open models)", "base": "https://api.tokenfactory.nebius.com/v1",
     "prefer": ["nemotron-3-super", "nemotron-3-ultra", "nemotron", "nvidia"], "keys": "https://tokenfactory.nebius.com",
     "fast": ["nemotron-3-nano-30b", "nano", "lightning"], "deep": ["nemotron-3-ultra", "ultra"]},
    {"name": "Another OpenAI-compatible service", "base": None, "prefer": [], "keys": ""},
]

_NOT_CHAT_MODELS = (
    "whisper", "embed", "tts", "guard", "moderation", "image", "dall", "audio", "rerank", "speech", "imagen", "veo",
)

PRIVACY = (
    "Privacy: with a cloud model, what R2 sends to the model is sent to the provider you choose: your messages, "
    "and any text it reads from files, web pages or the screen while doing a task. Passwords and card numbers "
    "are never put into R2's own memory, but they would still be sent if they appear in text R2 reads. "
    "Switch back any time with: r2 local"
)


def write_env(path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file, keeping every other line (including comments) as it was."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def list_models(base: str, key: str, http_get=httpx.get) -> list[str]:
    """Chat-capable model ids this key can use, asked from the provider itself."""
    resp = http_get(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    ids = set()
    for item in body.get("data") or body.get("models") or []:
        model_id = (item.get("id") or item.get("name") or "").removeprefix("models/")
        if model_id and not any(word in model_id.lower() for word in _NOT_CHAT_MODELS):
            ids.add(model_id)
    return sorted(ids)


def recommend(models: list[str], prefer: list[str]) -> str | None:
    for wanted in prefer:
        for model in models:
            if wanted in model.lower():
                return model
    return models[0] if models else None


def pick_tier(models: list[str], wanted: list[str], exclude: str) -> str:
    """The first model matching a size hint (nano, ultra, ...), or "" so that tier simply stays off."""
    for hint in wanted:
        for model in models:
            if hint in model.lower() and model != exclude and "omni" not in model.lower():
                return model
    return ""


def setup_cloud(input_fn=input, secret_fn=getpass.getpass, http_get=httpx.get, env_path=".env", out=print) -> int:
    out("Connect R2 to a hosted model for faster, more accurate answers.")
    out(PRIVACY)
    out("")
    for i, p in enumerate(PROVIDERS, 1):
        out(f"  [{i}] {p['name']}")
    choice = input_fn("Provider [1]: ").strip() or "1"
    provider = PROVIDERS[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(PROVIDERS) else PROVIDERS[0]
    base = provider["base"] or input_fn("Base URL of the service (ends with /v1): ").strip()
    if provider["keys"]:
        out(f"Get a free key at {provider['keys']}")
    key = secret_fn("Paste your API key (it is hidden): ").strip()
    if not key or not base:
        out("No key or address given, so nothing was changed.")
        return 1
    try:
        models = list_models(base, key, http_get)
    except httpx.HTTPStatusError as exc:
        out(f"The provider rejected the key (HTTP {exc.response.status_code}). Nothing was changed.")
        return 1
    except Exception as exc:
        out(f"Could not reach the provider ({type(exc).__name__}). Nothing was changed.")
        return 1
    if not models:
        out("The provider listed no usable chat models for this key. Nothing was changed.")
        return 1
    best = recommend(models, provider["prefer"])
    shown = models[:15]
    for i, name in enumerate(shown, 1):
        out(f"  [{i}] {name}" + ("   <- recommended" if name == best else ""))
    pick = input_fn(f"Model number, or Enter for {best}: ").strip()
    model = shown[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(shown) else best
    scope = input_fn("Use the cloud for [1] everything (fastest) or [2] only for tasks, chat stays on this PC? [1]: ").strip() or "1"
    fast = pick_tier(models, provider.get("fast", []), model) if scope != "2" else ""
    deep = pick_tier(models, provider.get("deep", []), model)
    write_env(
        env_path,
        {
            "R2_FAST_MODEL": fast,
            "R2_DEEP_MODEL": deep,
            "R2_BRAIN": "openai",
            "R2_OPENAI_BASE_URL": base.rstrip("/"),
            "R2_OPENAI_API_KEY": key,
            "R2_OPENAI_MODEL": model,
            "R2_CHAT_MODEL": "qwen2.5:1.5b" if scope == "2" else "",
        },
    )
    out(f"Done. R2 will use {model} ({'tasks only' if scope == '2' else 'everything'}). The key was saved to .env and is not shown.")
    if fast or deep:
        out(f"Model tiers: chat -> {fast or 'main model'}, tasks -> {model}, hard requests -> {deep or 'main model'}.")
    out("Restart R2 to use it: tray icon -> Quit, then run: r2 daemon. Check it with: r2 doctor")
    return 0
