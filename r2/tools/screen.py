from __future__ import annotations

from itertools import islice
from pathlib import Path

SCREEN_DIR = Path("data") / "screens"


def screenshot(path: str | None = None) -> str:
    import mss
    import mss.tools

    target = Path(path) if path else SCREEN_DIR / "latest.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    grabber = getattr(mss, "MSS", None) or mss.mss
    with grabber() as sct:
        shot = sct.grab(sct.monitors[1])
        mss.tools.to_png(shot.rgb, shot.size, output=str(target))
    return f"Saved screenshot to {target} ({shot.size[0]}x{shot.size[1]})"


def _active_title() -> str:
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def list_windows() -> str:
    from pywinauto import Desktop

    active = _active_title()
    lines = []
    for win in Desktop(backend="uia").windows():
        title = win.window_text().strip()
        if title:
            lines.append(("* " if title == active else "  ") + title)
    return "\n".join(lines) or "No windows"


def format_elements(items: list[tuple[str, str]], limit: int = 60) -> str:
    named = [(kind, name) for kind, name in items if name and name.strip()]
    if not named:
        return "No named UI elements found"
    lines = [f"{kind}: {name.strip()}" for kind, name in named[:limit]]
    if len(named) > limit:
        lines.append(f"...[{len(named) - limit} more]")
    return "\n".join(lines)


def ui_elements(limit: int = 60) -> str:
    """Named controls of the active window, read from the Windows accessibility tree."""
    from pywinauto import Desktop

    active = _active_title()
    for win in Desktop(backend="uia").windows():
        if win.window_text() == active:
            items = [
                (c.element_info.control_type or "Control", c.window_text())
                for c in islice(win.iter_descendants(), 600)
            ]
            return f"Window: {active}\n" + format_elements(items, limit)
    return "No active window found"
