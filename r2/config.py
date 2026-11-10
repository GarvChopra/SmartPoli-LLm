from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _gpu_layers(value: str) -> int | None:
    value = value.strip().lower()
    return None if value in ("", "auto") else int(value)


def _default_claude_dirs() -> tuple[str, ...]:
    home = Path.home()
    return tuple(str(p) for p in (home / "Downloads", home / "Documents", home / "Desktop") if p.is_dir())


def _claude_dirs(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return _default_claude_dirs()
    return tuple(part.strip() for part in raw.split(";") if part.strip())


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("r2.db")
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen2.5:3b"
    max_steps: int = 25
    brain: str = "ollama"  # "ollama" | "openai" (any OpenAI-compatible host) | "cloud" (Anthropic)
    cloud_model: str = "claude-sonnet-5-5"
    anthropic_api_key: str = field(default="", repr=False)
    openai_base_url: str = ""  # any OpenAI-compatible provider (Groq, Gemini, OpenRouter, ...)
    openai_api_key: str = field(default="", repr=False)
    openai_model: str = ""  # the main model for tasks (Nemotron Super class)
    fast_model: str = ""  # small hosted model for plain conversation (Nemotron Nano class); "" = off
    deep_model: str = ""  # biggest hosted model for hard, many-step requests (Nemotron Ultra class); "" = off
    profile: str = ""  # "demo" = hosted public demo: safe tools only, rate limited, fixed access code
    demo_code: str = field(default="", repr=False)
    demo_tasks_per_hour: int = 30
    private_local: bool = True  # requests containing secrets stay on this PC when a local model is running
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = Path("data")
    headless: bool = False
    voice: bool = False
    overlay: bool = True  # the desktop chat window is the main interface
    wake_model: str = ""  # path or built-in name of an openWakeWord model; empty = tray "Listen now" only
    whisper_model: str = "base"
    chat_model: str = "qwen2.5:1.5b"  # fast model for plain conversation; "" = use the main model
    gpu_layers: int | None = 0  # 0 = CPU only (no video-memory contention); None = let Ollama decide
    claude: bool = True  # let R2 hand tasks to Claude Code when you say "ask claude to ..."
    claude_dirs: tuple[str, ...] = field(default_factory=_default_claude_dirs)  # folders Claude Code may read
    learn: bool = False  # opt-in: learn which apps you use at which hour
    keep_alive: str = "30m"  # how long Ollama keeps a model in memory after use

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            db_path=Path(env("R2_DB_PATH", "r2.db")),
            ollama_url=env("R2_OLLAMA_URL", "http://localhost:11434"),
            model=env("R2_MODEL", "qwen2.5:3b"),
            max_steps=int(env("R2_MAX_STEPS", "25")),
            brain=env("R2_BRAIN", "ollama"),
            cloud_model=env("R2_CLOUD_MODEL", "claude-sonnet-5-5"),
            anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
            openai_base_url=env("R2_OPENAI_BASE_URL", ""),
            openai_api_key=env("R2_OPENAI_API_KEY", ""),
            openai_model=env("R2_OPENAI_MODEL", ""),
            fast_model=env("R2_FAST_MODEL", ""),
            deep_model=env("R2_DEEP_MODEL", ""),
            private_local=_flag(env("R2_PRIVATE_LOCAL", "1")),
            profile=env("R2_PROFILE", "").strip().lower(),
            demo_code=env("R2_DEMO_CODE", "").strip(),
            demo_tasks_per_hour=int(env("R2_DEMO_TASKS_PER_HOUR", "30")),
            host=env("R2_HOST", "127.0.0.1"),
            port=int(env("R2_PORT", "8765")),
            data_dir=Path(env("R2_DATA_DIR", "data")),
            headless=_flag(env("R2_HEADLESS", "")),
            voice=_flag(env("R2_VOICE", "")),
            overlay=_flag(env("R2_OVERLAY", "1")),
            wake_model=env("R2_WAKE_MODEL", ""),
            whisper_model=env("R2_WHISPER_MODEL", "base"),
            chat_model=env("R2_CHAT_MODEL", "qwen2.5:1.5b"),
            keep_alive=env("R2_KEEP_ALIVE", "30m"),
            learn=_flag(env("R2_LEARN", "")),
            claude=_flag(env("R2_CLAUDE", "1")),
            claude_dirs=_claude_dirs(os.environ.get("R2_CLAUDE_DIRS")),
            gpu_layers=_gpu_layers(env("R2_GPU_LAYERS", "0")),
        )


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(path=".env") -> None:
    """Minimal .env reader. Real environment variables win over the file."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if _KEY.match(key):
            os.environ.setdefault(key, value)
