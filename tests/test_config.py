from pathlib import Path

from r2.config import Settings


def test_defaults():
    s = Settings()
    assert s.model == "qwen2.5:3b"
    assert s.max_steps == 25
    assert s.ollama_url == "http://localhost:11434"
    assert s.db_path == Path("r2.db")


def test_from_env(monkeypatch):
    monkeypatch.setenv("R2_MODEL", "llama3.2:3b")
    monkeypatch.setenv("R2_MAX_STEPS", "7")
    monkeypatch.setenv("R2_DB_PATH", "x.db")
    monkeypatch.setenv("R2_OLLAMA_URL", "http://example:1234")
    s = Settings.from_env()
    assert s.model == "llama3.2:3b"
    assert s.max_steps == 7
    assert s.db_path == Path("x.db")
    assert s.ollama_url == "http://example:1234"
