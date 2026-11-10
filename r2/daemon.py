from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx

from r2.config import Settings, load_dotenv
from r2.overlay import ensure_token

CREATE_NO_WINDOW = 0x08000000
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

log = logging.getLogger("r2")


def is_loopback(host: str) -> bool:
    return host in _LOOPBACK


def port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex((host, port)) != 0


def ollama_up(url: str) -> bool:
    try:
        return httpx.get(url.rstrip("/") + "/api/tags", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def find_ollama() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.exists():
            return str(candidate)
    return None


def ensure_ollama(
    settings: Settings,
    probe=ollama_up,
    finder=find_ollama,
    launcher=subprocess.Popen,
    wait: float = 15.0,
) -> str:
    if settings.brain != "ollama":
        return "Ollama not needed (cloud brain)"
    if probe(settings.ollama_url):
        return "Ollama already running"
    exe = finder()
    if exe is None:
        return "Ollama is not installed (https://ollama.com/download)"
    env = {
        **os.environ,
        "OLLAMA_MAX_LOADED_MODELS": os.environ.get("OLLAMA_MAX_LOADED_MODELS", "2"),
        "OLLAMA_FLASH_ATTENTION": os.environ.get("OLLAMA_FLASH_ATTENTION", "1"),
    }
    launcher(
        [exe, "serve"],
        creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    deadline = time.time() + wait
    while time.time() < deadline:
        if probe(settings.ollama_url):
            return "Ollama started"
        time.sleep(0.1)
    return f"Ollama was launched but did not respond within {wait:g}s"


def warm_models(settings: Settings, post=httpx.post) -> str:
    """Load the local models into memory at startup so the first message is not slow. The context
    size must match what real requests ask for, or Ollama would reload the model anyway. Models
    that a cloud brain handles are not loaded (that would only waste memory)."""
    wanted: list[tuple[str, int]] = []
    if settings.brain in ("ollama", "openai") and settings.chat_model:
        wanted.append((settings.chat_model, 2048))
    if settings.brain == "ollama" and settings.model not in [m for m, _ in wanted]:
        wanted.append((settings.model, 4096))
    if not wanted:
        return "Model warm-up skipped"

    def options(ctx: int) -> dict:
        opts = {"num_ctx": ctx}
        if settings.gpu_layers is not None:
            opts["num_gpu"] = settings.gpu_layers
        return opts

    results = []
    for model, ctx in wanted:
        try:
            post(
                settings.ollama_url.rstrip("/") + "/api/generate",
                json={"model": model, "keep_alive": settings.keep_alive, "options": options(ctx)},
                timeout=300,
            )
            results.append(f"Warmed up {model}")
        except Exception as exc:
            results.append(f"Could not warm up {model}: {exc}")
    return "; ".join(results)


def make_icon_image(size: int = 64):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 3, size - 3), fill=(76, 141, 255, 255))
    draw.text((size * 0.28, size * 0.36), "R2", fill=(255, 255, 255, 255))
    return img


def _setup_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(data_dir / "r2.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main() -> int:
    import pystray
    import uvicorn

    from r2.runtime import build_runtime
    from r2.server import create_app

    load_dotenv()
    settings = Settings.from_env()
    _setup_logging(settings.data_dir)

    if not is_loopback(settings.host) and os.environ.get("R2_ALLOW_LAN") != "1":
        log.error("Refusing to bind %s: R2 controls this PC and must listen on localhost only "
                  "(use Tailscale to reach it). Set R2_ALLOW_LAN=1 to override.", settings.host)
        return 2
    if not port_free(settings.host, settings.port):
        log.error("Port %s is already in use; is R2 already running?", settings.port)
        return 1

    rt = build_runtime(settings)
    server = uvicorn.Server(
        uvicorn.Config(create_app(rt), host=settings.host, port=settings.port, log_config=None, log_level="warning")
    )
    threading.Thread(target=server.run, name="r2-server", daemon=True).start()
    def start_models() -> None:
        log.info(ensure_ollama(settings))
        log.info(warm_models(settings))
        from r2.tools import apps

        log.info("Indexed %d Start-menu apps", len(apps.installed_apps()))

    threading.Thread(target=start_models, name="r2-ollama", daemon=True).start()
    log.info("R2 server listening on http://%s:%s", settings.host, settings.port)

    base = (os.environ.get("R2_PUBLIC_URL") or f"http://localhost:{settings.port}").rstrip("/")
    shutdown = threading.Event()
    overlay_proc: dict = {"p": None}
    voice_holder: dict = {"v": None}

    def open_ui(icon=None, item=None):
        code = rt.auth.new_pairing_code()
        webbrowser.open(f"http://localhost:{settings.port}/?code={code}")

    def new_code(icon, item):
        code = rt.auth.new_pairing_code()
        icon.notify(f"Code {code} (10 min). Phone link: {base}/?code={code}", "R2 pairing")

    def toggle_pause(icon, item):
        rt.tasks.paused = not rt.tasks.paused
        icon.update_menu()

    def show_overlay(icon=None, item=None):
        ensure_token(rt.auth, settings.data_dir)
        proc = overlay_proc["p"]
        if proc is None or proc.poll() is not None:
            overlay_proc["p"] = subprocess.Popen([sys.executable, "-m", "r2.overlay"], creationflags=CREATE_NO_WINDOW)
        else:  # already running (maybe hidden): ask it to appear
            (settings.data_dir / "overlay.show").write_text("1", encoding="utf-8")

    def toggle_mute(icon, item):
        voice_holder["v"].muted = not voice_holder["v"].muted
        icon.update_menu()

    def quit_app(icon, item):
        shutdown.set()
        if activity_logger is not None:
            activity_logger.stop()
        rt.tasks.stop()
        server.should_exit = True
        proc = overlay_proc["p"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
        rt.registry.close()
        icon.stop()

    if settings.voice:
        try:
            from r2.voice import VoiceUnavailable, build_real_voice, run_task_and_wait

            def speak_via_voice(text: str) -> None:
                voice_holder["v"].speak(text)

            voice_holder["v"] = build_real_voice(
                settings,
                run_task=lambda text: run_task_and_wait(rt.tasks, text, speak_via_voice, on_sentence=speak_via_voice),
                stop_task=rt.tasks.stop,
            )
            threading.Thread(
                target=voice_holder["v"].run_forever, args=(shutdown,), name="r2-voice", daemon=True
            ).start()
            log.info("Voice assistant started (wake model: %s)", settings.wake_model or "none, tray 'Listen now' only")
        except VoiceUnavailable as exc:
            log.warning("Voice disabled: %s", exc)
    activity_logger = None
    if settings.learn:
        from r2.activity import ActivityLogger

        activity_logger = ActivityLogger(rt.memory)
        activity_logger.start()
        log.info("Habit learning is ON (records only app names; wipe with: r2 forget)")
    if settings.overlay:
        show_overlay()

    items = [
        pystray.MenuItem("Open R2", show_overlay, default=True),
        pystray.MenuItem("Open in browser", open_ui),
        pystray.MenuItem("New pairing code", new_code),
        pystray.MenuItem("Pause", toggle_pause, checked=lambda item: rt.tasks.paused),
        pystray.MenuItem("Stop current task", lambda icon, item: rt.tasks.stop()),
    ]
    if voice_holder["v"] is not None:
        items += [
            pystray.MenuItem("Listen now", lambda icon, item: voice_holder["v"].listen_now()),
            pystray.MenuItem("Mute microphone", toggle_mute, checked=lambda item: voice_holder["v"].muted),
        ]
    items += [pystray.Menu.SEPARATOR, pystray.MenuItem("Quit", quit_app)]
    menu = pystray.Menu(*items)
    pystray.Icon("R2", make_icon_image(), "R2 assistant", menu).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
