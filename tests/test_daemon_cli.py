import socket
import subprocess
from pathlib import Path

import pytest

from r2 import cli, daemon
from r2.auth import AuthStore
from r2.config import Settings

ROOT = Path(__file__).resolve().parent.parent


# ---------- cli ----------
@pytest.fixture
def data_env(tmp_path, monkeypatch):
    monkeypatch.setenv("R2_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("R2_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("R2_PUBLIC_URL", raising=False)
    monkeypatch.setenv("R2_PORT", "8765")
    return tmp_path


def test_pair_prints_link_with_a_code_that_redeems(data_env, capsys):
    assert cli.main(["pair"]) == 0
    out = capsys.readouterr().out
    link = next(w for w in out.split() if w.startswith("http://localhost:8765/?code="))
    code = link.split("code=")[1]
    assert AuthStore(data_env / "tokens.json").redeem(code)


def test_pair_uses_public_url_when_set(data_env, capsys, monkeypatch):
    monkeypatch.setenv("R2_PUBLIC_URL", "https://pc.tail1234.ts.net/")
    cli.main(["pair"])
    assert "https://pc.tail1234.ts.net/?code=" in capsys.readouterr().out


def test_pair_prints_a_qr_code(data_env, capsys):
    cli.main(["pair"])
    out = capsys.readouterr().out
    assert "\u2588" in out or "\u2580" in out or "##" in out  # block characters of the ASCII QR


def test_revoke_clears_every_token(data_env, capsys):
    store = AuthStore(data_env / "tokens.json")
    token = store.redeem(store.new_pairing_code())
    assert cli.main(["revoke"]) == 0
    assert not AuthStore(data_env / "tokens.json").verify(token)


def test_ask_still_works_with_a_missing_model_server(data_env, capsys, monkeypatch):
    monkeypatch.setenv("R2_OLLAMA_URL", "http://127.0.0.1:9")  # nothing listens here
    assert cli.main(["ask", "hello"]) == 1
    assert "Ollama" in capsys.readouterr().out


def test_unknown_command_is_an_argparse_error(capsys):
    with pytest.raises(SystemExit):
        cli.main(["frobnicate"])


# ---------- daemon ----------
def test_port_free_detects_a_listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    assert daemon.port_free("127.0.0.1", port) is False
    s.close()
    assert daemon.port_free("127.0.0.1", port) is True


def test_ensure_ollama_branches():
    ollama = Settings()
    cloud = Settings(brain="cloud")
    launched = []

    def launcher(cmd, **kw):
        launched.append(cmd)

    assert "not needed" in daemon.ensure_ollama(cloud, probe=lambda url: False, finder=lambda: None, launcher=launcher)
    assert "already running" in daemon.ensure_ollama(ollama, probe=lambda url: True, finder=lambda: "x", launcher=launcher)
    assert "not installed" in daemon.ensure_ollama(ollama, probe=lambda url: False, finder=lambda: None, launcher=launcher)
    assert launched == []
    state = {"up": False}

    def probe(url):
        return state["up"]

    envs = []

    def launching(cmd, **kw):
        launched.append(cmd)
        envs.append(kw.get("env", {}))
        state["up"] = True

    assert "started" in daemon.ensure_ollama(
        ollama, probe=probe, finder=lambda: "C:/ollama.exe", launcher=launching, wait=0.5
    )
    assert launched == [["C:/ollama.exe", "serve"]]
    assert envs[0]["OLLAMA_MAX_LOADED_MODELS"] == "2"  # chat model + tool model stay loaded together
    assert envs[0]["OLLAMA_FLASH_ATTENTION"] == "1"  # faster prompt reading


def test_ensure_ollama_reports_failure_to_come_up():
    msg = daemon.ensure_ollama(
        Settings(), probe=lambda url: False, finder=lambda: "x", launcher=lambda cmd, **kw: None, wait=0.2
    )
    assert "did not respond" in msg


def test_tray_icon_image_is_square_rgba():
    img = daemon.make_icon_image(64)
    assert img.size == (64, 64) and img.mode == "RGBA"


# ---------- auto-start scripts ----------
@pytest.mark.parametrize("name", ["install-autostart.ps1", "uninstall-autostart.ps1"])
def test_powershell_scripts_parse_cleanly(name):
    script = ROOT / "scripts" / name
    assert script.exists()
    cmd = (
        "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}',[ref]$null,[ref]$e); if ($e.Count -gt 0) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_install_script_runs_the_daemon_at_logon_without_admin():
    text = (ROOT / "scripts" / "install-autostart.ps1").read_text(encoding="utf-8")
    assert "-AtLogOn" in text
    assert "r2.daemon" in text
    assert "pythonw.exe" in text
    assert "-RunLevel Limited" in text
    assert "-RestartCount" in text


def test_loopback_check():
    assert daemon.is_loopback("127.0.0.1") and daemon.is_loopback("localhost")
    assert not daemon.is_loopback("0.0.0.0") and not daemon.is_loopback("192.168.1.4")


def test_warm_models_preloads_both_models_with_the_context_size_real_requests_use():
    sent = []

    def post(url, json=None, timeout=None):
        sent.append((url, json))

        class R:
            status_code = 200

        return R()

    msg = daemon.warm_models(
        Settings(model="qwen2.5:7b", chat_model="qwen2.5:1.5b", keep_alive="30m"), post=post
    )
    assert "qwen2.5:1.5b" in msg and "qwen2.5:7b" in msg
    url = "http://localhost:11434/api/generate"
    # same num_ctx as the real requests, otherwise Ollama would reload the model on first use
    assert sent == [
        (url, {"model": "qwen2.5:1.5b", "keep_alive": "30m", "options": {"num_ctx": 2048, "num_gpu": 0}}),
        (url, {"model": "qwen2.5:7b", "keep_alive": "30m", "options": {"num_ctx": 4096, "num_gpu": 0}}),
    ]


def test_warm_models_skips_when_not_applicable_and_never_raises():
    assert "skipped" in daemon.warm_models(Settings(brain="cloud"), post=lambda *a, **k: 1 / 0)
    assert "skipped" in daemon.warm_models(Settings(brain="openai", chat_model=""), post=lambda *a, **k: 1 / 0)

    def boom(*a, **k):
        raise RuntimeError("ollama down")

    assert "could not" in daemon.warm_models(Settings(), post=boom).lower()


def test_forget_wipes_history_and_learned_habits_but_keeps_facts(data_env, capsys):
    import time as _t

    from r2.memory import Memory

    mem = Memory(data_env / "t.db")
    mem.add_message("user", "open notepad")
    mem.log_activity("chrome.exe", ts=_t.time())
    mem.save_fact("my name is Garv", "user")
    assert cli.main(["forget"]) == 0
    again = Memory(data_env / "t.db")
    assert again.recent_messages(5) == []
    assert again._db.execute("select count(*) from activity").fetchone()[0] == 0
    assert [f.text for f in again.list_facts()] == ["my name is Garv"]
    assert "forgot" in capsys.readouterr().out.lower()
