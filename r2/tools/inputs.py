from __future__ import annotations

from r2.types import Risk, RiskInfo

_NAV_KEYS = {
    "tab", "shift+tab", "up", "down", "left", "right",
    "pageup", "pagedown", "home", "end", "escape", "esc",
}
_ENTER_KEYS = {"enter", "return"}


def _pg():
    import pyautogui

    return pyautogui


def _set_clipboard(text: str) -> None:
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    root.destroy()


def _norm(keys: str) -> str:
    return "+".join(p.strip().lower() for p in str(keys).split("+"))


def classify_key(args: dict) -> RiskInfo:
    keys = _norm(args.get("keys", ""))
    if keys in _NAV_KEYS:
        return RiskInfo(Risk.SAFE)
    if keys in _ENTER_KEYS:
        return RiskInfo(Risk.CONFIRM, "submit")
    return RiskInfo(Risk.CONFIRM, "keys")


def classify_click(args: dict) -> RiskInfo:
    return RiskInfo(Risk.CONFIRM, "click")


def type_text(text: str) -> str:
    pg = _pg()
    if text.isascii():
        pg.write(text, interval=0.02)
    else:
        _set_clipboard(text)
        pg.hotkey("ctrl", "v")
    return f"Typed {len(text)} characters"


def scroll(amount: int) -> str:
    _pg().scroll(int(amount))
    return f"Scrolled {amount}"


def click(x: int, y: int) -> str:
    pg = _pg()
    width, height = pg.size()
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(f"({x}, {y}) is outside the {width}x{height} screen")
    pg.click(x, y)
    return f"Clicked ({x}, {y})"


def press_key(keys: str) -> str:
    parts = _norm(keys).split("+")
    pg = _pg()
    if len(parts) == 1:
        pg.press(parts[0])
    else:
        pg.hotkey(*parts)
    return f"Pressed {'+'.join(parts)}"
