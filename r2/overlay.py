"""R2's desktop window: a small always-on-top chat that floats over your work.

Type to it, watch the answer appear word by word, approve or deny risky actions, press Stop.
Esc hides it (it keeps running); Ctrl+Alt+R brings it back from anywhere. It is a thin client
of the local R2 server, so it has no power of its own. The logic (OverlayClient, describe,
transcript_lines, ensure_token) is separate from the tkinter view so it can be tested."""
from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import httpx

from r2.auth import AuthStore
from r2.avatar import Companion, avatar_state, outcome_state
from r2.config import Settings, load_dotenv

BG, PANEL, FG, MUTED, ACCENT, DANGER, WARN = (
    "#14171d", "#1d2129", "#e8eaf0", "#9aa3b5", "#4c8dff", "#e5484d", "#f5a524")
HOTKEY_NAME = "Ctrl+Alt+R"


def enable_dpi_awareness() -> None:
    """Draw at the screen's real resolution. Without this, Windows renders the window small and then
    stretches it, which makes text and the shield blurry on high-DPI screens."""
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def ensure_token(auth: AuthStore, data_dir) -> str:
    """The overlay's own device token, kept in data/overlay.token and reused between runs."""
    path = Path(data_dir) / "overlay.token"
    try:
        token = path.read_text(encoding="utf-8").strip()
        if auth.verify(token):
            return token
    except OSError:
        pass
    token = auth.redeem(auth.new_pairing_code())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return token


class OverlayClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client or httpx.Client(timeout=3)

    def _request(self, method: str, path: str, **kw):
        return self._client.request(method, self._base + path, headers=self._headers, **kw)

    def status(self) -> dict | None:
        try:
            r = self._request("GET", "/api/status")
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def chat(self) -> list[dict]:
        try:
            r = self._request("GET", "/api/chat")
            return r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            return []

    def send(self, text: str) -> str | None:
        """None on success, otherwise a short reason to show the user."""
        try:
            r = self._request("POST", "/api/tasks", json={"message": text})
        except httpx.HTTPError:
            return "R2 is not running"
        if r.status_code == 202:
            return None
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        return detail if isinstance(detail, str) else f"Error {r.status_code}"

    def approve(self, approval_id: str, ok: bool) -> bool:
        try:
            return self._request("POST", "/api/approve", json={"id": approval_id, "approve": ok}).status_code == 200
        except httpx.HTTPError:
            return False

    def stop(self) -> bool:
        try:
            return self._request("POST", "/api/stop").status_code == 200
        except httpx.HTTPError:
            return False


def describe(status: dict | None) -> tuple[str, str]:
    if status is None:
        return "R2 is not running", "Start it from the tray, or run: r2 daemon"
    pending = status.get("pending")
    if pending:
        return "Needs your approval", f"{pending['tool']} {pending.get('args', {})}"[:200]
    if status.get("paused"):
        return "Paused", "Resume from the tray menu"
    if status.get("running"):
        return "Working...", (status.get("message") or "")[:200]
    result = status.get("result")
    return "Idle", (result["answer"][:200] if result else "")


def transcript_lines(messages: list[dict], partial: str, working_on: str | None = None) -> list[tuple[str, str]]:
    """(style, text) lines for the chat view. While R2 is answering, the words so far are
    shown as a live line (or "..." before the first word arrives)."""
    lines: list[tuple[str, str]] = []
    for m in messages:
        if m.get("role") == "user":
            lines.append(("user", f"You: {m['content']}"))
        elif m.get("role") == "assistant":
            lines.append(("assistant", f"R2: {m['content']}"))
    if partial or working_on:
        if working_on and not (messages and messages[-1].get("role") == "user" and messages[-1]["content"] == working_on):
            lines.append(("user", f"You: {working_on}"))
        lines.append(("partial", f"R2: {partial or '...'}"))
    return lines


def start_hotkey(events: "queue.Queue[str]") -> bool:
    """Register Ctrl+Alt+R system-wide (Windows). Presses are put on `events`.
    Returns False if another program already owns the combination."""
    import ctypes
    from ctypes import wintypes

    ready: queue.Queue[bool] = queue.Queue()

    def loop() -> None:
        user32 = ctypes.windll.user32
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312
        ok = bool(user32.RegisterHotKey(None, 0xA11, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("R")))
        ready.put(ok)
        if not ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                events.put("toggle")

    threading.Thread(target=loop, name="r2-hotkey", daemon=True).start()
    return ready.get(timeout=5)


class OverlayWindow(tk.Tk):
    def __init__(self, client, poll_ms: int = 1000, show_flag: Path | None = None, hotkey_events=None,
                 brain_label: str = "", scale: float | None = None):
        super().__init__()
        self.scale = scale if scale is not None else max(1.0, self.winfo_fpixels("1i") / 96)
        self.client = client
        self.poll_ms = poll_ms
        self.show_flag = show_flag
        self.hotkey_events = hotkey_events
        self.render_count = 0
        self.clock = time.time
        self._last_status = None
        self._was_running = False
        self._recent: str | None = None
        self._recent_until = 0.0
        self._last_lines: list | None = None
        self._pending_id: str | None = None
        self.title("R2")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.97)
        self.configure(bg=BG)
        S = self.scale
        width, height = int(330 * S), int(400 * S)  # a compact window
        shield = int(84 * S)
        x = self.winfo_screenwidth() - width - int(24 * S)
        y = max(int(24 * S), self.winfo_screenheight() - height - shield - int(76 * S))  # above the floating shield
        self.geometry(f"{width}x{height}+{x}+{y}")

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(bar, text="R2", bg=BG, fg=ACCENT, font=("Segoe UI", 10, "bold")).pack(side="left")
        self.headline = tk.Label(bar, text="...", bg=BG, fg=FG, font=("Segoe UI", 9, "bold"))
        self.headline.pack(side="left", padx=10)
        tk.Button(bar, text="–", command=self.hide, bg=BG, fg=MUTED, bd=0, activebackground=BG,
                  font=("Segoe UI", 11)).pack(side="right")
        self.badge = tk.Label(bar, text=brain_label, bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.badge.pack(side="right", padx=6)
        for widget in (bar, self.headline):  # drag the window by its title bar
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        body = tk.Frame(self, bg=BG)
        self.transcript = tk.Text(body, bg=PANEL, fg=FG, bd=0, wrap="word", state="disabled",
                                  font=("Segoe UI", 9), padx=8, pady=6, cursor="arrow", spacing3=4)
        scroll = tk.Scrollbar(body, command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.transcript.pack(side="left", fill="both", expand=True)
        self.transcript.tag_configure("user", foreground=ACCENT)
        self.transcript.tag_configure("assistant", foreground=FG)
        self.transcript.tag_configure("partial", foreground=MUTED)

        self.detail = tk.Label(self, text="", bg=BG, fg=MUTED, justify="left", wraplength=int(300 * S), anchor="w",
                               font=("Segoe UI", 8))

        actions = tk.Frame(self, bg=BG)
        self.approve_btn = tk.Button(actions, text="Approve", command=lambda: self._decide(True),
                                     bg=ACCENT, fg="white", bd=0, state="disabled", padx=10)
        self.approve_btn.pack(side="left", padx=2)
        self.deny_btn = tk.Button(actions, text="Deny", command=lambda: self._decide(False),
                                  bg="#2a2f3a", fg=FG, bd=0, state="disabled", padx=10)
        self.deny_btn.pack(side="left", padx=2)
        self.stop_btn = tk.Button(actions, text="Stop", command=self.client.stop, bg="#2a2f3a", fg=DANGER, bd=0, padx=10)
        self.stop_btn.pack(side="right", padx=2)

        row = tk.Frame(self, bg=BG)
        self.entry = tk.Entry(row, bg=PANEL, fg=FG, insertbackground=FG, bd=0, font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=8)
        tk.Button(row, text="Send", command=self.submit, bg=ACCENT, fg="white", bd=0, padx=12).pack(
            side="left", padx=(6, 0), ipady=3)
        self.entry.bind("<Return>", lambda e: self.submit())
        self.bind("<Escape>", lambda e: self.hide())

        self.companion = Companion(self, on_click=self.toggle, scale=self.scale)
        self.entry.bind("<KeyRelease>", lambda e: self._update_companion(self._last_status))

        # Pack the rows that must always be visible FIRST (from the bottom up). The chat area is
        # packed last and only takes the space that is left, so a long conversation can never push
        # the text box and buttons out of the window.
        row.pack(side="bottom", fill="x", padx=12, pady=10)
        actions.pack(side="bottom", fill="x", padx=12, pady=(4, 0))
        self.detail.pack(side="bottom", fill="x", padx=14, pady=(6, 0))
        body.pack(side="top", fill="both", expand=True, padx=12)

        self.refresh()
        self.after(self.poll_ms, self._tick)
        self.after(100, self._pump)

    # ---- show / hide ----
    def hide(self) -> None:
        self.withdraw()

    def show(self) -> None:
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(50, self._focus_entry)

    def _focus_entry(self) -> None:
        try:
            self.focus_force()
            self.entry.focus_set()
        except tk.TclError:
            pass

    def toggle(self) -> None:
        self.hide() if self.state() == "normal" else self.show()

    def check_show_flag(self) -> None:
        """The daemon asks us to appear by creating a flag file (we run in a separate process)."""
        if self.show_flag is not None and self.show_flag.exists():
            try:
                self.show_flag.unlink()
            except OSError:
                pass
            self.show()

    # ---- behaviour ----
    def submit(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        error = self.client.send(text)
        self.entry.delete(0, "end")
        if error:
            self.detail.config(text=error, fg=DANGER)
        else:
            self.refresh()

    def refresh(self) -> bool:
        """Redraw from the server's state. Returns True while a task is running."""
        status = self.client.status()
        self._update_companion(status)
        headline, detail = describe(status)
        self.headline.config(text=headline, fg=WARN if headline == "Needs your approval" else FG)
        pending = (status or {}).get("pending")
        running = bool((status or {}).get("running"))
        self.detail.config(text=detail if pending or status is None else "", fg=MUTED)
        self._pending_id = pending["id"] if pending else None
        state = "normal" if pending else "disabled"
        self.approve_btn.config(state=state)
        self.deny_btn.config(state=state)
        self.stop_btn.config(state="normal" if running else "disabled")

        lines = transcript_lines(
            self.client.chat(),
            (status or {}).get("partial", ""),
            (status or {}).get("message") if running else None,
        )
        if lines != self._last_lines:
            self._last_lines = lines
            self.render_count += 1
            self.transcript.config(state="normal")
            self.transcript.delete("1.0", "end")
            for style, text in lines:
                self.transcript.insert("end", text + "\n", style)
            self.transcript.config(state="disabled")
            self.transcript.see("end")
        return running

    def _update_companion(self, status) -> None:
        running = bool((status or {}).get("running"))
        if self._was_running and not running and status and status.get("result"):
            self._recent = outcome_state(status["result"])  # celebrate (or look puzzled) for a few seconds
            self._recent_until = self.clock() + 5
        self._was_running = running
        self._last_status = status
        recent = self._recent if self.clock() < self._recent_until else None
        self.companion.set_state(
            avatar_state(status, chat_open=self.state() == "normal", typing=bool(self.entry.get().strip()), recent=recent)
        )

    def _decide(self, ok: bool) -> None:
        if self._pending_id:
            self.client.approve(self._pending_id, ok)
            self.refresh()

    def _tick(self) -> None:
        running = self.refresh()
        self.after(400 if running else self.poll_ms, self._tick)

    def _pump(self) -> None:
        self.check_show_flag()
        if self.hotkey_events is not None:
            try:
                while True:
                    self.hotkey_events.get_nowait()
                    self.toggle()
            except queue.Empty:
                pass
        self.after(150, self._pump)

    def _drag_start(self, event) -> None:
        self._dx, self._dy = event.x_root - self.winfo_x(), event.y_root - self.winfo_y()

    def _drag_move(self, event) -> None:
        self.geometry(f"+{event.x_root - self._dx}+{event.y_root - self._dy}")


def main() -> int:
    load_dotenv()
    settings = Settings.from_env()
    token_file = settings.data_dir / "overlay.token"
    if not token_file.exists():
        print("The overlay needs the R2 daemon to be running once first (it creates data/overlay.token).")
        return 1
    client = OverlayClient(f"http://localhost:{settings.port}", token_file.read_text(encoding="utf-8").strip())
    events: queue.Queue = queue.Queue()
    if not start_hotkey(events):
        print(f"Note: {HOTKEY_NAME} is already used by another program; use the tray icon to open R2.")
    enable_dpi_awareness()  # must happen before the first window is created
    label = "cloud" if settings.brain in ("openai", "cloud") else "local"
    window = OverlayWindow(client, show_flag=settings.data_dir / "overlay.show", hotkey_events=events, brain_label=label)
    window.after(200, window.show)
    window.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
