"""SmartPoli Care Companion: Compact, Smooth Floating Desktop AI Healthcare Companion (tkinter + Pillow).

Lives on the user's desktop as a small (84px), sleek, draggable, always-on-top animated character
with 11 expressive states:
  idle, listening, thinking, speaking, reminder, happy, concerned,
  escalating, helpful, error, sleeping

Clicking the character expands a compact (330x420), Windows-11 rounded companion popover with:
- Real Groq-powered multi-turn conversation + compact quick chips
- Voice input (microphone / STT) and spoken responses (pyttsx3 TTS) synchronized with avatar states
- Compact due-reminder strip (Taken / 10m / Missed) & full medication schedule tab
- Proposed new medication reminder confirmation card (Confirm / Cancel)
- Caregiver contact confirmation dialog (Confirm / Cancel, clearly labeled simulation by default)
- Smart Actions resource & step-by-step instruction cards (verified NIH/NHLBI/AHA/MedlinePlus guides,
  one-click browser launch, Windows Notification Settings opener, automatic screen navigation)
- Light & Dark themes, Reduced Motion mode, and persistent screen position (data/care_position.json)
"""
from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
import tkinter as tk
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk

from r2.care_agent import CareAgent
from r2.care_calls import caregiver_script, make_provider
from r2.care_meds import CareError, CareStore
from r2.care_smart_actions import SmartActionRunner

CARE_STATES = (
    "idle",
    "listening",
    "thinking",
    "speaking",
    "reminder",
    "happy",
    "concerned",
    "escalating",
    "helpful",
    "error",
    "sleeping",
)
ANIMATED_STATES = {
    "idle",
    "listening",
    "thinking",
    "speaking",
    "reminder",
    "happy",
    "concerned",
    "escalating",
    "helpful",
}
PHASE_STEPS = 20  # smooth 20-step cycle
SS = 4

KEY = "#010203"
KEY_RGB = (1, 2, 3)

TEAL_BRIGHT = (45, 212, 191, 255)      # #2dd4bf
TEAL_CORE = (13, 148, 136, 255)        # #0d9488
CYAN_GLOW = (56, 189, 248, 255)        # #38bdf8
EMERALD = (52, 211, 153, 255)          # #34d399
AMBER = (251, 191, 36, 255)            # #fbbf24
CORAL = (248, 113, 113, 255)           # #f87171
VIOLET = (167, 139, 250, 255)          # #a78bfa
WHITE = (255, 255, 255, 255)
SLATE_DIM = (148, 163, 184, 255)

THEMES = {
    "dark": {
        "bg": "#0d171c",
        "panel": "#122128",
        "card": "#172b34",
        "card_alt": "#1b333e",
        "border": "#22424f",
        "text": "#f0fdfa",
        "muted": "#94a3b8",
        "accent": "#0d9488",
        "accent_fg": "#ffffff",
        "user_bubble": "#0d9488",
        "user_fg": "#ffffff",
        "bot_bubble": "#172b34",
        "bot_fg": "#e2e8f0",
        "warn_bg": "#3b2106",
        "warn_fg": "#fde68a",
        "danger_bg": "#3f1116",
        "danger_fg": "#fecaca",
        "ok_bg": "#064e3b",
        "ok_fg": "#a7f3d0",
    },
    "light": {
        "bg": "#f4fbfa",
        "panel": "#ffffff",
        "card": "#f1f5f9",
        "card_alt": "#e6fffa",
        "border": "#cbd5e1",
        "text": "#0f172a",
        "muted": "#475569",
        "accent": "#0d9488",
        "accent_fg": "#ffffff",
        "user_bubble": "#0d9488",
        "user_fg": "#ffffff",
        "bot_bubble": "#f1f5f9",
        "bot_fg": "#0f172a",
        "warn_bg": "#fef3c7",
        "warn_fg": "#92400e",
        "danger_bg": "#fee2e2",
        "danger_fg": "#991b1b",
        "ok_bg": "#d1fae5",
        "ok_fg": "#065f46",
    },
}


# ---------------------------------------------------------------------------
# Win32 Desktop Attachment, Rounded Corners & Position Persistence
# ---------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    """Attach to the interactive Default desktop (if launched from a background desktop) and enable High-DPI."""
    try:
        import ctypes
        import sys

        if "pytest" not in sys.modules and sys.platform.startswith("win"):
            u = ctypes.windll.user32
            hd = u.OpenDesktopW("Default", 0, False, 0x01FF)
            if hd:
                u.SetThreadDesktop(hd)
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _style_win32_window(win: tk.Misc, round_corners: bool = False, activate: bool = False) -> None:
    """Ensure a frameless Toplevel is visible, topmost, and has smooth Windows 11 rounded corners."""
    try:
        import ctypes

        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        if round_corners:
            # Windows 11 DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        SW_SHOWNOACTIVATE = 4
        SW_SHOW = 5
        HWND_TOPMOST = -1
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW if activate else SW_SHOWNOACTIVATE)
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | (0 if activate else SWP_NOACTIVATE)
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        if activate:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def load_position(path: Path, default_x: int = 1100, default_y: int = 620) -> tuple[int, int]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return int(data.get("x", default_x)), int(data.get("y", default_y))
    except Exception:
        pass
    return default_x, default_y


def save_position(path: Path, x: int, y: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"x": int(x), "y": int(y)}), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Avatar state resolver
# ---------------------------------------------------------------------------
def resolve_companion_state(
    *,
    listening: bool = False,
    thinking: bool = False,
    speaking: bool = False,
    due_count: int = 0,
    pending_caregiver: bool = False,
    mood: str = "neutral",
    ai_error: str | None = None,
    sleeping: bool = False,
) -> str:
    """Pure function determining the floating character's animation state."""
    if sleeping:
        return "sleeping"
    if listening:
        return "listening"
    if thinking:
        return "thinking"
    if speaking:
        return "speaking"
    if ai_error:
        return "error"
    if pending_caregiver or mood == "escalating":
        return "escalating"
    if mood in ("helpful", "happy", "concerned", "error", "reminder"):
        return mood
    if due_count > 0:
        return "reminder"
    return "idle"


# ---------------------------------------------------------------------------
# Supersampled PIL Mascot Renderer
# ---------------------------------------------------------------------------
@lru_cache(maxsize=32)
def _mascot_body(px: int, bob_step: int = 0) -> Image.Image:
    """Friendly, sleek rounded-bot body with a glowing teal rim, dark visor, and subtle floating bob."""
    w = px * SS
    k = w / 96.0
    bob = 2.2 * math.sin((bob_step / PHASE_STEPS) * 2 * math.pi) * k
    out = Image.new("RGBA", (w, w), (0, 0, 0, 0))

    # Soft ground shadow (scales slightly with floating bob)
    shadow = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    sw_pad = 1.5 * math.sin((bob_step / PHASE_STEPS) * 2 * math.pi) * k
    ImageDraw.Draw(shadow).ellipse(
        [(24 + sw_pad) * k, 80 * k, (72 - sw_pad) * k, 89 * k], fill=(0, 0, 0, 115)
    )
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(2.4 * k)))

    # Gentle antenna / care halo stub at top
    antenna = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    ad = ImageDraw.Draw(antenna)
    ad.rounded_rectangle([45 * k, 7 * k + bob, 51 * k, 17 * k + bob], radius=int(3 * k), fill=(180, 245, 235, 255))
    ad.ellipse([43 * k, 3 * k + bob, 53 * k, 12 * k + bob], fill=TEAL_BRIGHT)
    out = Image.alpha_composite(out, antenna)

    # Outer shell (sleek squircle) with pearl-teal gradient
    shell_mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(shell_mask).rounded_rectangle(
        [14 * k, 15 * k + bob, 82 * k, 79 * k + bob], radius=int(24 * k), fill=255
    )
    shell_grad = ImageOps.colorize(
        Image.linear_gradient("L").resize((w, w)),
        black=(224, 252, 248),
        white=(13, 148, 136),
    ).convert("RGBA")
    shell_grad.putalpha(shell_mask)
    out = Image.alpha_composite(out, shell_grad)

    # Side ear pods
    ears = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    ed = ImageDraw.Draw(ears)
    ed.rounded_rectangle([9 * k, 37 * k + bob, 16 * k, 56 * k + bob], radius=int(3.5 * k), fill=TEAL_CORE)
    ed.rounded_rectangle([80 * k, 37 * k + bob, 87 * k, 56 * k + bob], radius=int(3.5 * k), fill=TEAL_CORE)
    out = Image.alpha_composite(out, ears)

    # Inner dark glass visor
    visor_mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(visor_mask).rounded_rectangle(
        [20 * k, 22 * k + bob, 76 * k, 71 * k + bob], radius=int(18 * k), fill=255
    )
    visor_grad = ImageOps.colorize(
        Image.linear_gradient("L").resize((w, w)),
        black=(18, 40, 49),
        white=(9, 21, 27),
    ).convert("RGBA")
    visor_grad.putalpha(visor_mask)
    out = Image.alpha_composite(out, visor_grad)

    # Top gloss highlight on visor
    gloss = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).ellipse([24 * k, 23 * k + bob, 72 * k, 42 * k + bob], fill=(255, 255, 255, 26))
    out = Image.alpha_composite(out, gloss.filter(ImageFilter.GaussianBlur(1.6 * k)))

    # Small medical care cross on forehead rim
    crest = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    cd = ImageDraw.Draw(crest)
    cd.rounded_rectangle([46.5 * k, 16 * k + bob, 49.5 * k, 21 * k + bob], radius=int(1 * k), fill=WHITE)
    cd.rounded_rectangle([45.5 * k, 17 * k + bob, 50.5 * k, 20 * k + bob], radius=int(1 * k), fill=WHITE)
    return Image.alpha_composite(out, crest)


def _draw_face(draw: ImageDraw.ImageDraw, state: str, k: float, phase: float) -> None:
    """Draw eyes, mouth, and state-specific micro-expressions on the visor."""
    bob = 2.2 * math.sin(phase * 2 * math.pi) if state in ANIMATED_STATES else 0.0
    ey = (43 + bob) * k
    lx, rx = 35 * k, 61 * k
    er = 4.8 * k

    if state == "idle":
        blink = math.sin(phase * 2 * math.pi) < -0.92
        if blink:
            draw.line([(lx - er, ey), (lx + er, ey)], fill=TEAL_BRIGHT, width=max(2, round(2.6 * k)))
            draw.line([(rx - er, ey), (rx + er, ey)], fill=TEAL_BRIGHT, width=max(2, round(2.6 * k)))
        else:
            draw.ellipse([lx - er, ey - er, lx + er, ey + er], fill=TEAL_BRIGHT)
            draw.ellipse([rx - er, ey - er, rx + er, ey + er], fill=TEAL_BRIGHT)
            draw.ellipse([lx - 1.8 * k, ey - 2.6 * k, lx + 1 * k, ey], fill=WHITE)
            draw.ellipse([rx - 1.8 * k, ey - 2.6 * k, rx + 1 * k, ey], fill=WHITE)
        draw.arc([40 * k, (50 + bob) * k, 56 * k, (61 + bob) * k], 20, 160, fill=TEAL_BRIGHT, width=max(2, round(2.6 * k)))

    elif state == "listening":
        r_listen = er * (1.08 + 0.12 * math.sin(phase * 2 * math.pi))
        draw.ellipse([lx - r_listen, ey - r_listen, lx + r_listen, ey + r_listen], fill=CYAN_GLOW)
        draw.ellipse([rx - r_listen, ey - r_listen, rx + r_listen, ey + r_listen], fill=CYAN_GLOW)
        draw.ellipse([lx - 1.8 * k, ey - 1.8 * k, lx + 1.8 * k, ey + 1.8 * k], fill=WHITE)
        draw.ellipse([rx - 1.8 * k, ey - 1.8 * k, rx + 1.8 * k, ey + 1.8 * k], fill=WHITE)
        mw = (4.0 + 1.8 * abs(math.sin(phase * 2 * math.pi))) * k
        draw.ellipse([48 * k - mw, (56 + bob) * k - mw * 0.7, 48 * k + mw, (56 + bob) * k + mw * 0.7], outline=CYAN_GLOW, width=max(2, round(2.3 * k)))

    elif state == "thinking":
        draw.ellipse([lx - er, ey - er - 1.5 * k, lx + er, ey + er - 1.5 * k], fill=TEAL_BRIGHT)
        draw.ellipse([rx - er, ey - er - 1.5 * k, rx + er, ey + er - 1.5 * k], fill=TEAL_BRIGHT)
        for i in range(3):
            pulse = math.sin((phase + i / 3.0) * 2 * math.pi)
            dx = (39 + i * 9) * k
            dy = (58 + bob - 2.5 * max(0.0, pulse)) * k
            dr = (2.1 + 0.9 * max(0.0, pulse)) * k
            draw.ellipse([dx - dr, dy - dr, dx + dr, dy + dr], fill=CYAN_GLOW if pulse > 0 else TEAL_BRIGHT)

    elif state == "speaking":
        draw.ellipse([lx - er, ey - er, lx + er, ey + er], fill=TEAL_BRIGHT)
        draw.ellipse([rx - er, ey - er, rx + er, ey + er], fill=TEAL_BRIGHT)
        open_h = (2.2 + 4.2 * abs(math.sin(phase * 2 * math.pi))) * k
        draw.rounded_rectangle(
            [40 * k, (56 + bob) * k - open_h, 56 * k, (56 + bob) * k + open_h],
            radius=int(3.5 * k),
            fill=TEAL_BRIGHT,
        )

    elif state == "reminder":
        draw.ellipse([lx - er, ey - er, lx + er, ey + er], fill=AMBER)
        draw.ellipse([rx - er, ey - er, rx + er, ey + er], fill=AMBER)
        draw.arc([39 * k, (49 + bob) * k, 57 * k, (62 + bob) * k], 15, 165, fill=AMBER, width=max(2, round(2.6 * k)))
        br = (8.0 + 1.4 * math.sin(phase * 2 * math.pi)) * k
        bx, by = 74 * k, (20 + bob) * k
        draw.ellipse([bx - br, by - br, bx + br, by + br], fill=AMBER)
        draw.line([(bx, by - 3.5 * k), (bx, by + 1 * k)], fill=(20, 20, 20, 255), width=max(2, round(2.2 * k)))
        draw.ellipse([bx - 1.3 * k, by + 2.6 * k, bx + 1.3 * k, by + 5.2 * k], fill=(20, 20, 20, 255))

    elif state == "happy":
        draw.arc([lx - er, ey - er, lx + er, ey + er], 200, 340, fill=EMERALD, width=max(2, round(2.8 * k)))
        draw.arc([rx - er, ey - er, rx + er, ey + er], 200, 340, fill=EMERALD, width=max(2, round(2.8 * k)))
        draw.arc([38 * k, (48 + bob) * k, 58 * k, (63 + bob) * k], 15, 165, fill=EMERALD, width=max(2, round(2.8 * k)))
        bx, by, br = 74 * k, (20 + bob) * k, 8 * k
        draw.ellipse([bx - br, by - br, bx + br, by + br], fill=EMERALD)
        draw.line([(bx - 3.5 * k, by), (bx - 0.8 * k, by + 2.6 * k), (bx + 4 * k, by - 2.6 * k)],
                  fill=(6, 40, 30, 255), width=max(2, round(2.2 * k)))

    elif state == "concerned":
        draw.ellipse([lx - er * 0.9, ey - er * 0.9, lx + er * 0.9, ey + er * 0.9], fill=AMBER)
        draw.ellipse([rx - er * 0.9, ey - er * 0.9, rx + er * 0.9, ey + er * 0.9], fill=AMBER)
        draw.line([(lx - er, ey - er - 2.5 * k), (lx + er, ey - er - 0.8 * k)], fill=AMBER, width=max(2, round(2 * k)))
        draw.line([(rx - er, ey - er - 0.8 * k), (rx + er, ey - er - 2.5 * k)], fill=AMBER, width=max(2, round(2 * k)))
        draw.arc([41 * k, (55 + bob) * k, 55 * k, (63 + bob) * k], 200, 340, fill=AMBER, width=max(2, round(2.4 * k)))

    elif state == "escalating":
        draw.ellipse([lx - er, ey - er, lx + er, ey + er], fill=VIOLET)
        draw.ellipse([rx - er, ey - er, rx + er, ey + er], fill=VIOLET)
        draw.arc([40 * k, (51 + bob) * k, 56 * k, (61 + bob) * k], 20, 160, fill=VIOLET, width=max(2, round(2.4 * k)))
        br = (8.0 + 1.4 * math.sin(phase * 2 * math.pi)) * k
        bx, by = 74 * k, (20 + bob) * k
        draw.ellipse([bx - br, by - br, bx + br, by + br], fill=VIOLET)
        draw.ellipse([bx - 3 * k, by - 3 * k, bx + 3 * k, by + 3 * k], fill=WHITE)

    elif state == "helpful":
        draw.ellipse([lx - er, ey - er, lx + er, ey + er], fill=CYAN_GLOW)
        draw.ellipse([rx - er, ey - er, rx + er, ey + er], fill=CYAN_GLOW)
        draw.ellipse([lx - 1.8 * k, ey - 2.5 * k, lx + 1.2 * k, ey], fill=WHITE)
        draw.ellipse([rx - 1.8 * k, ey - 2.5 * k, rx + 1.2 * k, ey], fill=WHITE)
        draw.arc([39 * k, (49 + bob) * k, 57 * k, (62 + bob) * k], 15, 165, fill=CYAN_GLOW, width=max(2, round(2.6 * k)))
        bx, by = 74 * k, (20 + bob) * k
        reach = (7.2 + 1.6 * abs(math.sin(phase * 2 * math.pi))) * k
        draw.line([(bx - reach, by), (bx + reach, by)], fill=WHITE, width=max(2, round(2 * k)))
        draw.line([(bx, by - reach), (bx, by + reach)], fill=WHITE, width=max(2, round(2 * k)))

    elif state == "error":
        draw.ellipse([lx - er, ey - er, lx + er, ey + er], fill=CORAL)
        draw.ellipse([rx - er, ey - er, rx + er, ey + er], fill=CORAL)
        draw.line([(42 * k, (57 + bob) * k), (54 * k, (57 + bob) * k)], fill=CORAL, width=max(2, round(2.4 * k)))
        bx, by, br = 74 * k, (20 + bob) * k, 8 * k
        draw.ellipse([bx - br, by - br, bx + br, by + br], fill=CORAL)
        draw.line([(bx, by - 3.5 * k), (bx, by + 1 * k)], fill=WHITE, width=max(2, round(2.2 * k)))
        draw.ellipse([bx - 1.2 * k, by + 2.6 * k, bx + 1.2 * k, by + 5 * k], fill=WHITE)

    elif state == "sleeping":
        draw.arc([lx - er, ey - 2.5 * k, lx + er, ey + 4.5 * k], 20, 160, fill=SLATE_DIM, width=max(2, round(2.2 * k)))
        draw.arc([rx - er, ey - 2.5 * k, rx + er, ey + 4.5 * k], 20, 160, fill=SLATE_DIM, width=max(2, round(2.2 * k)))
        draw.line([(43 * k, 56 * k), (53 * k, 56 * k)], fill=SLATE_DIM, width=max(2, round(1.8 * k)))


def render_care_mascot(
    state: str = "idle",
    phase: float = 0.0,
    size: int = 84,
    background: tuple[int, int, int] | None = None,
) -> Image.Image:
    """Render the SmartPoli Care Companion mascot at `size`x`size` using 4x supersampling."""
    if state not in CARE_STATES:
        raise ValueError(f"Unknown companion state {state!r}; expected one of {CARE_STATES}")
    w = size * SS
    step = int(phase * PHASE_STEPS) % PHASE_STEPS if state in ANIMATED_STATES else 0
    face = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    _draw_face(ImageDraw.Draw(face), state, w / 96.0, phase)
    glow = face.filter(ImageFilter.GaussianBlur(2.2 * w / 96.0))
    glow.putalpha(glow.getchannel("A").point(lambda a: min(255, int(a * 1.65))))
    comp = Image.alpha_composite(_mascot_body(size, step), glow)
    comp = Image.alpha_composite(comp, face).resize((size, size), Image.LANCZOS)
    if background is None:
        return comp
    flat = Image.new("RGB", (size, size), background)
    flat.paste(comp, (0, 0), comp)
    return flat


_FRAME_CACHE: dict[tuple[str, int, int], Image.Image] = {}


def get_mascot_frame(state: str, phase: float, size: int = 84) -> Image.Image:
    step = int(phase * PHASE_STEPS) % PHASE_STEPS if state in ANIMATED_STATES else 0
    key = (state, step, size)
    if key not in _FRAME_CACHE:
        _FRAME_CACHE[key] = render_care_mascot(state, step / PHASE_STEPS, size, background=KEY_RGB)
    return _FRAME_CACHE[key]


# ---------------------------------------------------------------------------
# Non-blocking Voice Output (pyttsx3)
# ---------------------------------------------------------------------------
class CompanionVoice:
    """Non-blocking TTS speaker that synchronizes the avatar's `speaking` state."""

    def __init__(self, enabled: bool = True, on_speaking_change: Callable[[bool], None] | None = None):
        self.enabled = enabled
        self.on_speaking_change = on_speaking_change
        self.speaking = False
        self.last_spoken: str = ""
        self._q: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def speak(self, text: str) -> None:
        clean = (text or "").strip()
        if not clean or not self.enabled:
            return
        self.last_spoken = clean
        if os.environ.get("CARE_MUTE_TTS") == "1":
            return
        self._q.put(clean)

    def _set_speaking(self, flag: bool) -> None:
        self.speaking = flag
        if self.on_speaking_change:
            try:
                self.on_speaking_change(flag)
            except Exception:
                pass

    def _worker(self) -> None:
        engine = None
        while True:
            text = self._q.get()
            if text is None:
                break
            if not self.enabled:
                continue
            self._set_speaking(True)
            try:
                if os.environ.get("CARE_MUTE_TTS") == "1":
                    time.sleep(0.05)
                else:
                    if engine is None:
                        import pyttsx3

                        engine = pyttsx3.init()
                        engine.setProperty("rate", 178)
                    engine.say(text)
                    engine.runAndWait()
            except Exception:
                pass
            finally:
                self._set_speaking(False)


# ---------------------------------------------------------------------------
# Compact Floating Desktop Mascot Window
# ---------------------------------------------------------------------------
class FloatingMascot(tk.Toplevel):
    """Sleek, compact (84px), draggable, always-on-top transparent floating character."""

    def __init__(
        self,
        master: tk.Misc,
        on_click: Callable[[], None],
        position_path: Path,
        size: int = 84,
        scale: float = 1.0,
        reduced_motion: bool = False,
    ):
        super().__init__(master)
        self._on_click = on_click
        self.position_path = position_path
        self._px = max(48, int(round(size * scale)))
        self.state_name = "idle"
        self.reduced_motion = reduced_motion
        self._phase = 0.0
        self._photo = None
        self._press_at: tuple[int, int] | None = None
        self._origin: tuple[int, int] = (0, 0)
        self._moved = False

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=KEY)
        try:
            self.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            self,
            width=self._px,
            height=self._px,
            bg=KEY,
            highlightthickness=0,
            cursor="hand2",
        )
        self.canvas.pack()

        sw = max(800, self.winfo_screenwidth())
        sh = max(600, self.winfo_screenheight())
        default_x = max(40, sw - self._px - int(28 * scale))
        default_y = max(60, sh - self._px - int(96 * scale))
        x, y = load_position(position_path, default_x, default_y)
        self.pos_x = max(20, min(x, sw - self._px - 20))
        self.pos_y = max(20, min(y, sh - self._px - 60))
        self.geometry(f"{self._px}x{self._px}+{self.pos_x}+{self.pos_y}")
        self.deiconify()
        self.lift()
        _style_win32_window(self, round_corners=False)

        self.canvas.bind("<ButtonPress-1>", lambda e: self._press(e.x_root, e.y_root))
        self.canvas.bind("<B1-Motion>", lambda e: self._drag_to(e.x_root, e.y_root))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._release())

        self._render()
        self.after(55, self._animate)

    @property
    def pixel_size(self) -> int:
        return self._px

    def set_state(self, state: str) -> None:
        if state not in CARE_STATES:
            raise ValueError(f"Unknown state {state!r}")
        if state != self.state_name:
            self.state_name = state
            self._render()

    def set_reduced_motion(self, enabled: bool) -> None:
        self.reduced_motion = bool(enabled)
        self._render()

    def _render(self) -> None:
        phase = 0.0 if self.reduced_motion else self._phase
        self._photo = ImageTk.PhotoImage(get_mascot_frame(self.state_name, phase, self._px), master=self)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def _animate(self) -> None:
        try:
            if not self.reduced_motion and self.state_name in ANIMATED_STATES:
                self._phase = (self._phase + 1.0 / PHASE_STEPS) % 1.0
                self._render()
            self.after(55, self._animate)
        except tk.TclError:
            pass

    def _press(self, x_root: int, y_root: int) -> None:
        self._press_at = (x_root, y_root)
        self._moved = False
        self._origin = (self.pos_x, self.pos_y)

    def _drag_to(self, x_root: int, y_root: int) -> None:
        if self._press_at is None:
            return
        dx, dy = x_root - self._press_at[0], y_root - self._press_at[1]
        if abs(dx) + abs(dy) > 4:
            self._moved = True
        if self._moved:
            self.pos_x = self._origin[0] + dx
            self.pos_y = self._origin[1] + dy
            self.geometry(f"+{self.pos_x}+{self.pos_y}")

    def _release(self) -> None:
        was_click = self._press_at is not None and not self._moved
        if self._moved:
            save_position(self.position_path, self.pos_x, self.pos_y)
        self._press_at = None
        if was_click:
            self._on_click()


# ---------------------------------------------------------------------------
# Compact, Smooth Expandable SmartPoli Care Companion Window
# ---------------------------------------------------------------------------
class CareCompanionApp:
    """Compact (330x420), rounded floating desktop companion + expandable popover."""

    PANEL_W = 330
    PANEL_H = 420

    def __init__(
        self,
        store: CareStore,
        agent: CareAgent,
        provider=None,
        smart_actions: SmartActionRunner | None = None,
        model_name: str = "openai/gpt-oss-120b",
        position_path: Path | None = None,
        theme: str = "dark",
        voice_enabled: bool = True,
        reduced_motion: bool = False,
        master: tk.Misc | None = None,
    ):
        self.store = store
        self.agent = agent
        self.provider = provider or make_provider()
        self.smart_actions = smart_actions or getattr(agent, "smart_actions", None) or SmartActionRunner(store)
        self.model_name = model_name
        self.position_path = position_path or (Path("data") / "care_position.json")
        self.theme_name = theme if theme in THEMES else "dark"
        self.reduced_motion = reduced_motion
        self.current_screen = "chat"
        self.expanded = False
        self.listening = False
        self.thinking = False
        self.last_mood = "neutral"
        self.last_error: str | None = None
        self.pending_caregiver_req: dict | None = None
        self.pending_reminder_prop: dict | None = None
        self.latest_resource_card: dict | None = None
        self._panel_drag_offset: tuple[int, int] = (0, 0)
        self._turn_queue: "queue.Queue[dict]" = queue.Queue()

        self.owns_root = master is None
        self.root = tk.Tk() if master is None else tk.Toplevel(master)
        if self.owns_root:
            self.root.withdraw()

        self.voice = CompanionVoice(enabled=voice_enabled, on_speaking_change=self._on_speaking_change)
        self.mascot = FloatingMascot(
            self.root,
            on_click=self.toggle_expanded,
            position_path=self.position_path,
            size=84,
            reduced_motion=self.reduced_motion,
        )

        self.session_id = "desktop-companion"
        first_name = self.store.profile()["name"].split()[0]
        self.agent.start(self.session_id, first_name, "app")
        nxt = self.store.next_dose()
        greeting = (
            f"Hi {first_name}! Next: {nxt['name']} at {nxt['time']}. How can I help?"
            if nxt
            else f"Hi {first_name}! How can I help you today?"
        )
        self.agent.sessions[self.session_id].history.append({"role": "assistant", "content": greeting})
        self.chat_messages: list[dict] = [{"role": "assistant", "text": greeting}]

        self.panel = tk.Toplevel(self.root)
        self.panel.withdraw()
        self.panel.overrideredirect(True)
        self.panel.attributes("-topmost", True)
        try:
            self.panel.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        self._build_panel()
        self._sync_avatar_state()
        self.root.after(1500, self._poll_due_reminders)

    def _t(self) -> dict:
        return THEMES[self.theme_name]

    def _panel_drag_start(self, event) -> None:
        self._panel_drag_offset = (event.x_root - self.panel.winfo_x(), event.y_root - self.panel.winfo_y())

    def _panel_drag_move(self, event) -> None:
        nx = event.x_root - self._panel_drag_offset[0]
        ny = event.x_root - self._panel_drag_offset[0], event.y_root - self._panel_drag_offset[1]
        self.panel.geometry(f"+{ny[0]}+{ny[1]}")

    def _build_panel(self) -> None:
        t = self._t()
        for child in self.panel.winfo_children():
            child.destroy()
        self.panel.configure(bg=t["border"])

        outer = tk.Frame(self.panel, bg=t["bg"], padx=10, pady=8)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        # ---- 1. Bottom composer & quick chips (packed FIRST from bottom so they never clip) ----
        input_bar = tk.Frame(outer, bg=t["panel"], padx=4, pady=4)
        input_bar.pack(side="bottom", fill="x", pady=(4, 0))

        self.mic_btn = tk.Button(
            input_bar,
            text="🎤",
            command=self.trigger_voice_input,
            bg=t["card"],
            fg=t["text"],
            bd=0,
            relief="flat",
            padx=6,
            pady=2,
            cursor="hand2",
        )
        self.mic_btn.pack(side="left", padx=(0, 4))

        self.entry = tk.Entry(
            input_bar,
            bg=t["card"],
            fg=t["text"],
            insertbackground=t["text"],
            bd=0,
            relief="flat",
            font=("Segoe UI", 9),
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 4))
        self.entry.bind("<Return>", lambda _: self._submit_entry())

        self.send_btn = tk.Button(
            input_bar,
            text="Send",
            command=self._submit_entry,
            bg=t["accent"],
            fg=t["accent_fg"],
            font=("Segoe UI", 8, "bold"),
            bd=0,
            relief="flat",
            padx=10,
            pady=3,
            cursor="hand2",
        )
        self.send_btn.pack(side="right")

        chips_frame = tk.Frame(outer, bg=t["bg"])
        chips_frame.pack(side="bottom", fill="x", pady=(4, 0))
        for label, prompt in (
            ("Next med", "When is my next medicine?"),
            ("Inhaler", "I don't know how to use my inhaler"),
            ("Sleep", "I can't sleep"),
            ("Unwell", "I'm not feeling well"),
        ):
            tk.Button(
                chips_frame,
                text=label,
                command=lambda p=prompt: self.send_user_message_async(p),
                bg=t["card"],
                fg=t["muted"],
                font=("Segoe UI", 8),
                bd=0,
                relief="flat",
                padx=7,
                pady=2,
                cursor="hand2",
            ).pack(side="left", padx=(0, 4))

        # ---- 2. Top compact header (draggable) ----
        hdr = tk.Frame(outer, bg=t["panel"], padx=8, pady=5)
        hdr.pack(side="top", fill="x", pady=(0, 5))

        title_box = tk.Frame(hdr, bg=t["panel"])
        title_box.pack(side="left", fill="x", expand=True)

        self.title_lbl = tk.Label(
            title_box,
            text="SmartPoli Companion",
            font=("Segoe UI", 9, "bold"),
            bg=t["panel"],
            fg=t["text"],
            anchor="w",
        )
        self.title_lbl.pack(fill="x")

        ai_dot = "● Groq" if self.agent.brain is not None else "○ Offline"
        self.subtitle_lbl = tk.Label(
            title_box,
            text=f"{ai_dot} • {self.mascot.state_name}",
            font=("Segoe UI", 7),
            bg=t["panel"],
            fg=t["muted"],
            anchor="w",
        )
        self.subtitle_lbl.pack(fill="x")

        for w in (hdr, title_box, self.title_lbl, self.subtitle_lbl):
            w.bind("<ButtonPress-1>", self._panel_drag_start)
            w.bind("<B1-Motion>", self._panel_drag_move)

        ctrl = tk.Frame(hdr, bg=t["panel"])
        ctrl.pack(side="right")

        for txt, cmd in (
            ("🔊" if self.voice.enabled else "🔇", self.toggle_voice),
            ("☀" if self.theme_name == "dark" else "🌙", self.toggle_theme),
            ("–", self.collapse),
        ):
            tk.Button(
                ctrl,
                text=txt,
                command=cmd,
                bg=t["card"],
                fg=t["text"],
                font=("Segoe UI", 8),
                bd=0,
                relief="flat",
                padx=5,
                pady=1,
                cursor="hand2",
            ).pack(side="left", padx=1)

        # ---- 3. Compact navigation bar ----
        nav = tk.Frame(outer, bg=t["bg"])
        nav.pack(side="top", fill="x", pady=(0, 5))
        self.nav_buttons: dict[str, tk.Button] = {}
        for scr, label in (
            ("chat", "Chat"),
            ("schedule", "Meds"),
            ("troubleshooting", "Help"),
            ("caregiver", "Caregiver"),
            ("activity", "Log"),
        ):
            active = self.current_screen == scr or (self.current_screen == "reminders" and scr == "schedule")
            btn = tk.Button(
                nav,
                text=label,
                command=lambda s=scr: self.switch_screen(s),
                bg=t["accent"] if active else t["card"],
                fg=t["accent_fg"] if active else t["muted"],
                font=("Segoe UI", 8, "bold" if active else "normal"),
                bd=0,
                relief="flat",
                padx=7,
                pady=2,
                cursor="hand2",
            )
            btn.pack(side="left", padx=(0, 3))
            self.nav_buttons[scr] = btn

        # ---- 4. Compact dynamic alert strip ----
        self.banner_frame = tk.Frame(outer, bg=t["bg"])
        self.banner_frame.pack(side="top", fill="x")
        self._refresh_banners()

        # ---- 5. Main screen content ----
        self.body_frame = tk.Frame(outer, bg=t["bg"])
        self.body_frame.pack(side="top", fill="both", expand=True)
        self._render_current_screen()

    def _refresh_banners(self) -> None:
        if not hasattr(self, "banner_frame") or not self.banner_frame.winfo_exists():
            return
        t = self._t()
        for child in self.banner_frame.winfo_children():
            child.destroy()

        # Compact single-strip due reminder (shows earliest due dose)
        due = [m for m in self.store.schedule() if m["status"] == "due"]
        if due:
            med = due[0]
            extra = f" (+{len(due) - 1})" if len(due) > 1 else ""
            card = tk.Frame(self.banner_frame, bg=t["warn_bg"], padx=6, pady=4)
            card.pack(fill="x", pady=(0, 4))
            top_row = tk.Frame(card, bg=t["warn_bg"])
            top_row.pack(fill="x")
            tk.Label(
                top_row,
                text=f"💊 Due: {med['name']} ({med['time']}){extra}",
                font=("Segoe UI", 8, "bold"),
                bg=t["warn_bg"],
                fg=t["warn_fg"],
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            tk.Button(
                top_row,
                text="✓ Taken",
                command=lambda mid=med["id"]: self.action_mark_taken(mid),
                bg=t["ok_bg"],
                fg=t["ok_fg"],
                font=("Segoe UI", 7, "bold"),
                bd=0,
                relief="flat",
                padx=5,
                pady=1,
                cursor="hand2",
            ).pack(side="right", padx=(2, 0))
            tk.Button(
                top_row,
                text="10m",
                command=lambda mid=med["id"]: self.action_snooze(mid, 10),
                bg=t["card"],
                fg=t["text"],
                font=("Segoe UI", 7),
                bd=0,
                relief="flat",
                padx=5,
                pady=1,
                cursor="hand2",
            ).pack(side="right", padx=(2, 0))
            tk.Button(
                top_row,
                text="Missed",
                command=lambda mid=med["id"]: self.action_missed(mid),
                bg=t["danger_bg"],
                fg=t["danger_fg"],
                font=("Segoe UI", 7),
                bd=0,
                relief="flat",
                padx=5,
                pady=1,
                cursor="hand2",
            ).pack(side="right")

        # Caregiver confirmation strip
        if self.pending_caregiver_req and self.pending_caregiver_req.get("status") == "pending":
            req = self.pending_caregiver_req
            card = tk.Frame(self.banner_frame, bg=t["card_alt"], padx=6, pady=4)
            card.pack(fill="x", pady=(0, 4))
            tk.Label(
                card,
                text=f"🤝 Notify {req['caregiver']}?",
                font=("Segoe UI", 8, "bold"),
                bg=t["card_alt"],
                fg=t["text"],
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            tk.Button(
                card,
                text="Cancel",
                command=lambda rid=req["request_id"]: self.cancel_caregiver_request(rid),
                bg=t["card"],
                fg=t["text"],
                font=("Segoe UI", 7),
                bd=0,
                padx=5,
                pady=1,
            ).pack(side="right", padx=(2, 0))
            tk.Button(
                card,
                text="Confirm",
                command=lambda rid=req["request_id"]: self.confirm_caregiver_request(rid),
                bg=t["accent"],
                fg=t["accent_fg"],
                font=("Segoe UI", 7, "bold"),
                bd=0,
                padx=6,
                pady=1,
            ).pack(side="right")

        # New reminder proposal strip
        if self.pending_reminder_prop and self.pending_reminder_prop.get("status") == "pending":
            prop = self.pending_reminder_prop
            card = tk.Frame(self.banner_frame, bg=t["card_alt"], padx=6, pady=4)
            card.pack(fill="x", pady=(0, 4))
            tk.Label(
                card,
                text=f"⏰ Add {prop['medication']} @ {prop['time']}?",
                font=("Segoe UI", 8, "bold"),
                bg=t["card_alt"],
                fg=t["text"],
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            tk.Button(
                card,
                text="Cancel",
                command=lambda pid=prop["proposal_id"]: self.cancel_reminder_proposal(pid),
                bg=t["card"],
                fg=t["text"],
                font=("Segoe UI", 7),
                bd=0,
                padx=5,
                pady=1,
            ).pack(side="right", padx=(2, 0))
            tk.Button(
                card,
                text="Confirm",
                command=lambda pid=prop["proposal_id"]: self.confirm_reminder_proposal(pid),
                bg=t["accent"],
                fg=t["accent_fg"],
                font=("Segoe UI", 7, "bold"),
                bd=0,
                padx=6,
                pady=1,
            ).pack(side="right")

        # Smart Action verified resource strip
        if self.latest_resource_card:
            rc = self.latest_resource_card
            card = tk.Frame(self.banner_frame, bg=t["card"], padx=6, pady=4)
            card.pack(fill="x", pady=(0, 4))
            top = tk.Frame(card, bg=t["card"])
            top.pack(fill="x")
            tk.Label(
                top,
                text=f"📘 {rc['title'][:34]}",
                font=("Segoe UI", 8, "bold"),
                bg=t["card"],
                fg=t["text"],
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            tk.Button(
                top,
                text="✕",
                command=self._dismiss_resource_card,
                bg=t["card"],
                fg=t["muted"],
                font=("Segoe UI", 7),
                bd=0,
                padx=3,
            ).pack(side="right")
            if rc.get("url"):
                tk.Button(
                    top,
                    text="Open ↗",
                    command=lambda topic=rc.get("resource_id", "inhaler_general"): self.smart_actions.open_trusted_resource(topic, open_in_browser=True),
                    bg=t["accent"],
                    fg=t["accent_fg"],
                    font=("Segoe UI", 7, "bold"),
                    bd=0,
                    padx=5,
                    pady=1,
                ).pack(side="right", padx=2)

    def _dismiss_resource_card(self) -> None:
        self.latest_resource_card = None
        self._refresh_banners()

    def _focus_entry(self) -> None:
        """Keep keyboard focus locked inside the chat entry box so the user never has to click it again."""
        try:
            if self.expanded and hasattr(self, "entry") and self.entry.winfo_exists():
                self.panel.focus_force()
                self.entry.focus_set()
                self.entry.icursor("end")
        except Exception:
            pass

    def _update_chat_text(self) -> None:
        """Update the chat transcript in place without destroying any widget (preserves entry focus)."""
        if not hasattr(self, "chat_text") or not self.chat_text.winfo_exists():
            return
        t = self._t()
        txt = self.chat_text
        txt.configure(state="normal", bg=t["panel"], fg=t["text"])
        txt.delete("1.0", "end")
        txt.tag_configure("user", foreground="#2dd4bf" if self.theme_name == "dark" else "#0f766e", font=("Segoe UI", 9, "bold"))
        txt.tag_configure("bot", foreground=t["text"], font=("Segoe UI", 9))
        txt.tag_configure("steps", foreground=t["muted"], font=("Segoe UI", 8))
        txt.tag_configure("thinking", foreground=t["muted"], font=("Segoe UI", 9, "italic"))
        for msg in self.chat_messages[-12:]:
            is_user = msg["role"] == "user"
            prefix = "You: " if is_user else "SmartPoli: "
            txt.insert("end", prefix + msg["text"] + "\n", "user" if is_user else "bot")
            for idx, step in enumerate(msg.get("steps") or [], 1):
                txt.insert("end", f"  {idx}. {step}\n", "steps")
        if self.thinking:
            txt.insert("end", "SmartPoli: Thinking...\n", "thinking")
        txt.configure(state="disabled")
        txt.see("end")

    def _render_current_screen(self) -> None:
        if not hasattr(self, "body_frame") or not self.body_frame.winfo_exists():
            return
        t = self._t()
        if self.current_screen == "chat" and hasattr(self, "chat_text") and self.chat_text.winfo_exists():
            self._update_chat_text()
            return

        for child in self.body_frame.winfo_children():
            child.destroy()

        if self.current_screen == "chat":
            chat_wrap = tk.Frame(self.body_frame, bg=t["panel"])
            chat_wrap.pack(fill="both", expand=True)
            self.chat_text = tk.Text(
                chat_wrap,
                bg=t["panel"],
                fg=t["text"],
                bd=0,
                wrap="word",
                state="normal",
                font=("Segoe UI", 9),
                padx=8,
                pady=6,
                cursor="arrow",
                spacing1=2,
                spacing3=5,
                takefocus=0,
            )
            self.chat_text.pack(side="left", fill="both", expand=True)
            self.chat_text.bind("<ButtonRelease-1>", lambda _: self._focus_entry())
            self._update_chat_text()

        elif self.current_screen in ("schedule", "reminders"):
            sched_box = tk.Frame(self.body_frame, bg=t["panel"], padx=6, pady=6)
            sched_box.pack(fill="both", expand=True)
            for med in self.store.schedule():
                row = tk.Frame(sched_box, bg=t["card"], padx=6, pady=4)
                row.pack(fill="x", pady=2)
                tk.Label(
                    row,
                    text=f"{med['time']}  {med['name']} ({med['status']})",
                    font=("Segoe UI", 8, "bold"),
                    bg=t["card"],
                    fg=t["text"],
                    anchor="w",
                ).pack(side="left", fill="x", expand=True)
                if med["status"] in ("due", "upcoming", "snoozed"):
                    tk.Button(
                        row,
                        text="✓",
                        command=lambda mid=med["id"]: self.action_mark_taken(mid),
                        bg=t["ok_bg"],
                        fg=t["ok_fg"],
                        font=("Segoe UI", 7, "bold"),
                        bd=0,
                        padx=5,
                    ).pack(side="right", padx=1)
                    tk.Button(
                        row,
                        text="10m",
                        command=lambda mid=med["id"]: self.action_snooze(mid, 10),
                        bg=t["panel"],
                        fg=t["text"],
                        font=("Segoe UI", 7),
                        bd=0,
                        padx=4,
                    ).pack(side="right", padx=1)

        elif self.current_screen == "troubleshooting":
            tr_box = tk.Frame(self.body_frame, bg=t["panel"], padx=8, pady=6)
            tr_box.pack(fill="both", expand=True)
            for step in (
                "1. Allow system Notifications for SmartPoli.",
                "2. Turn off Do Not Disturb / Focus Assist.",
                "3. Match PC & phone time zones.",
            ):
                tk.Label(tr_box, text=step, font=("Segoe UI", 8), bg=t["panel"], fg=t["text"], anchor="w").pack(fill="x", pady=2)
            tk.Button(
                tr_box,
                text="⚙ Notification Settings",
                command=self.action_open_notification_settings,
                bg=t["accent"],
                fg=t["accent_fg"],
                font=("Segoe UI", 8, "bold"),
                bd=0,
                padx=8,
                pady=3,
                cursor="hand2",
            ).pack(anchor="w", pady=(6, 2))

        elif self.current_screen == "caregiver":
            cg_box = tk.Frame(self.body_frame, bg=t["panel"], padx=8, pady=6)
            cg_box.pack(fill="both", expand=True)
            cg = self.store.caregiver()
            if cg:
                tk.Label(
                    cg_box,
                    text=f"{cg['name']} ({cg['relationship']})",
                    font=("Segoe UI", 9, "bold"),
                    bg=t["panel"],
                    fg=t["text"],
                    anchor="w",
                ).pack(fill="x")
                tk.Label(
                    cg_box,
                    text=f"Line: {cg['phone']} • {'Twilio' if self.provider.name == 'twilio' else 'Simulated Demo'}",
                    font=("Segoe UI", 8),
                    bg=t["panel"],
                    fg=t["muted"],
                    anchor="w",
                ).pack(fill="x", pady=(2, 6))
                tk.Button(
                    cg_box,
                    text="Propose Caregiver Check-In",
                    command=lambda: self.propose_caregiver_contact("User requested check-in"),
                    bg=t["accent"],
                    fg=t["accent_fg"],
                    font=("Segoe UI", 8, "bold"),
                    bd=0,
                    padx=8,
                    pady=3,
                    cursor="hand2",
                ).pack(anchor="w")

        elif self.current_screen == "activity":
            act_box = tk.Frame(self.body_frame, bg=t["panel"], padx=6, pady=6)
            act_box.pack(fill="both", expand=True)
            for item in self.store.activity(7):
                tk.Label(
                    act_box,
                    text=f"{item['time']} {item['detail'][:38]}",
                    font=("Segoe UI", 8),
                    bg=t["panel"],
                    fg=t["text"],
                    anchor="w",
                ).pack(fill="x", pady=1)

    # ------------------------------------------------------------------ window management
    def toggle_expanded(self) -> None:
        if self.expanded:
            self.collapse()
        else:
            self.expand()

    def expand(self) -> None:
        self.expanded = True
        mx, my = self.mascot.pos_x, self.mascot.pos_y
        pw, ph = self.PANEL_W, self.PANEL_H
        px = max(16, mx - pw - 10) if mx > pw + 24 else mx + self.mascot.pixel_size + 10
        py = max(16, min(my - ph + self.mascot.pixel_size, max(600, self.mascot.winfo_screenheight()) - ph - 56))
        self.panel.geometry(f"{pw}x{ph}+{px}+{py}")
        self.panel.deiconify()
        self.panel.lift()
        self.panel.attributes("-topmost", True)
        _style_win32_window(self.panel, round_corners=True, activate=True)
        _style_win32_window(self.mascot, round_corners=False, activate=False)
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        self._focus_entry()
        self.root.after(40, self._focus_entry)

    def collapse(self) -> None:
        self.expanded = False
        self.panel.withdraw()
        self._sync_avatar_state()

    def switch_screen(self, screen: str) -> None:
        res = self.smart_actions.navigate_screen(screen)
        self.current_screen = res["screen"]
        if not self.expanded:
            self.expand()
        else:
            self._build_panel()

    def toggle_theme(self) -> None:
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self._build_panel()

    def toggle_voice(self) -> None:
        self.voice.enabled = not self.voice.enabled
        self._build_panel()

    # ------------------------------------------------------------------ state synchronization
    def _on_speaking_change(self, is_speaking: bool) -> None:
        try:
            self.root.after(0, self._sync_avatar_state)
        except Exception:
            pass

    def _sync_avatar_state(self) -> None:
        due_count = sum(1 for m in self.store.schedule() if m["status"] == "due")
        pending_cg = bool(self.pending_caregiver_req and self.pending_caregiver_req.get("status") == "pending")
        state = resolve_companion_state(
            listening=self.listening,
            thinking=self.thinking,
            speaking=self.voice.speaking,
            due_count=due_count,
            pending_caregiver=pending_cg,
            mood=self.last_mood,
            ai_error=self.last_error,
        )
        self.mascot.set_state(state)
        if hasattr(self, "subtitle_lbl") and self.subtitle_lbl.winfo_exists():
            ai_dot = "● Groq" if self.agent.brain is not None else "○ Offline"
            self.subtitle_lbl.configure(text=f"{ai_dot} • {self.mascot.state_name}")

    # ------------------------------------------------------------------ actions & conversation
    def _submit_entry(self) -> None:
        if self.thinking:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.send_user_message_async(text)

    def send_user_message_async(self, text: str) -> None:
        """Non-blocking UI turn: shows user message & Thinking state immediately, runs Groq on a worker thread."""
        clean = (text or "").strip()
        if not clean or self.thinking:
            return
        self.current_screen = "chat"
        self.chat_messages.append({"role": "user", "text": clean})
        self.thinking = True
        self.last_error = None
        if hasattr(self, "send_btn") and self.send_btn.winfo_exists():
            self.send_btn.configure(state="disabled", text="...")
        self._render_current_screen()
        self._sync_avatar_state()
        self._focus_entry()

        def worker() -> None:
            try:
                out = self.agent.turn(self.session_id, clean)
            except Exception as exc:
                out = {
                    "reply": f"Sorry, something went wrong: {exc}",
                    "mood": "concerned",
                    "avatar_state": "error",
                    "ai_error": "unavailable",
                    "events": [],
                }
            self._turn_queue.put(out)

        threading.Thread(target=worker, name="smartpoli-turn", daemon=True).start()
        self.root.after(25, self._poll_turn_queue)

    def _poll_turn_queue(self) -> None:
        try:
            out = self._turn_queue.get_nowait()
        except queue.Empty:
            if self.thinking:
                try:
                    self.root.after(25, self._poll_turn_queue)
                except tk.TclError:
                    pass
            return
        self._apply_turn_result(out)

    def _apply_turn_result(self, out: dict) -> dict:
        self.thinking = False
        self.last_mood = out.get("avatar_state") or out.get("mood", "neutral")
        self.last_error = out.get("ai_error")
        card_steps: list[str] = []

        for ev in out.get("events", []):
            etype = ev.get("type")
            if etype == "caregiver_pending":
                self.pending_caregiver_req = {**ev, "status": "pending"}
            elif etype == "reminder_pending":
                self.pending_reminder_prop = {**ev, "status": "pending"}
            elif etype in ("resource_opened", "instructions"):
                self.latest_resource_card = ev
            elif etype == "settings_opened":
                self.latest_resource_card = {
                    "title": "Windows Notification Settings",
                    "source": "ms-settings:notifications",
                    "steps": ev.get("steps", []),
                }
            elif etype == "navigate":
                target_scr = ev.get("screen", "chat")
                if target_scr in ("schedule", "caregiver", "activity"):
                    self.current_screen = target_scr

        self.chat_messages.append({"role": "assistant", "text": out["reply"], "steps": card_steps})
        if hasattr(self, "send_btn") and self.send_btn.winfo_exists():
            self.send_btn.configure(state="normal", text="Send")
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        self._focus_entry()
        if self.voice.enabled:
            self.voice.speak(out["reply"])
        return out

    def send_user_message(self, text: str) -> dict:
        clean = (text or "").strip()
        if not clean:
            return {}
        self.chat_messages.append({"role": "user", "text": clean})
        self.thinking = True
        self.last_error = None
        self._sync_avatar_state()

        out = self.agent.turn(self.session_id, clean)
        return self._apply_turn_result(out)

    def trigger_voice_input(self, simulated_utterance: str | None = None) -> dict | None:
        self.listening = True
        self._sync_avatar_state()
        utterance = simulated_utterance
        if utterance is None:
            try:
                from r2.voice import collect_utterance

                utterance = collect_utterance()
            except Exception:
                utterance = ""
        self.listening = False
        self._sync_avatar_state()
        if utterance and utterance.strip():
            return self.send_user_message(utterance.strip())
        return None

    def action_mark_taken(self, med_id: int) -> dict:
        res = self.store.mark_taken(med_id)
        self.last_mood = "happy"
        self.chat_messages.append({"role": "assistant", "text": res["message"]})
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        return res

    def action_snooze(self, med_id: int, minutes: int = 10) -> dict:
        res = self.store.snooze(med_id, minutes)
        self.last_mood = "neutral"
        self.chat_messages.append({"role": "assistant", "text": res["message"]})
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        return res

    def action_missed(self, med_id: int) -> dict:
        res = self.store.log_missed(med_id)
        self.last_mood = "concerned"
        msg = f"{res['message']} Check your leaflet or ask your pharmacist if unsure."
        self.chat_messages.append({"role": "assistant", "text": msg})
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        return res

    def action_open_notification_settings(self) -> dict:
        res = self.smart_actions.open_notification_settings()
        self.last_mood = "helpful"
        self.latest_resource_card = {
            "title": "Windows Notification Settings",
            "source": "ms-settings:notifications",
            "steps": res["steps"],
        }
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        return res

    def propose_caregiver_contact(self, reason: str) -> dict:
        req = self.store.create_contact_request(reason)
        self.pending_caregiver_req = {
            "type": "caregiver_pending",
            "request_id": req["id"],
            "caregiver": req["caregiver"],
            "relationship": req["relationship"],
            "reason": req["reason"],
            "status": "pending",
        }
        self.last_mood = "escalating"
        self._refresh_banners()
        self._sync_avatar_state()
        return req

    def confirm_caregiver_request(self, request_id: int) -> dict:
        req = self.store.get_contact_request(request_id)
        if not req or req["status"] != "pending":
            raise CareError("No pending caregiver request to confirm.")
        cg = self.store.caregiver()
        if not cg or not cg["authorized"]:
            self.store.update_contact_request(request_id, "failed", result="no authorization")
            raise CareError("Caregiver is not authorized.")
        self.store.update_contact_request(request_id, "confirmed")
        result = self.provider.place_call(cg["phone"], caregiver_script(cg["relationship"]))
        self.store.update_contact_request(
            request_id,
            "completed" if result.ok else "failed",
            provider=result.provider,
            provider_ref=result.reference,
            result=result.status,
        )
        if self.pending_caregiver_req and self.pending_caregiver_req.get("request_id") == request_id:
            self.pending_caregiver_req["status"] = "completed" if result.ok else "failed"
        pub = result.public()
        self.last_mood = "happy" if result.ok else "error"
        self.chat_messages.append({"role": "assistant", "text": f"{cg['name']}: {pub['label']} (ref {pub['reference']})."})
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        return {"ok": result.ok, "request_id": request_id, "caregiver": cg["name"], **pub}

    def cancel_caregiver_request(self, request_id: int) -> dict:
        self.store.update_contact_request(request_id, "cancelled")
        if self.pending_caregiver_req and self.pending_caregiver_req.get("request_id") == request_id:
            self.pending_caregiver_req["status"] = "cancelled"
        self.last_mood = "neutral"
        self.chat_messages.append({"role": "assistant", "text": "Cancelled caregiver contact request."})
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        return {"ok": True, "request_id": request_id, "status": "cancelled"}

    def confirm_reminder_proposal(self, proposal_id: int) -> dict:
        res = self.store.confirm_medication_proposal(proposal_id)
        if self.pending_reminder_prop and self.pending_reminder_prop.get("proposal_id") == proposal_id:
            self.pending_reminder_prop["status"] = "confirmed"
        self.last_mood = "happy"
        self.chat_messages.append({"role": "assistant", "text": res["message"]})
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        return res

    def cancel_reminder_proposal(self, proposal_id: int) -> dict:
        res = self.store.cancel_medication_proposal(proposal_id)
        if self.pending_reminder_prop and self.pending_reminder_prop.get("proposal_id") == proposal_id:
            self.pending_reminder_prop["status"] = "cancelled"
        self.last_mood = "neutral"
        self.chat_messages.append({"role": "assistant", "text": "Cancelled reminder proposal."})
        self._refresh_banners()
        self._render_current_screen()
        self._sync_avatar_state()
        return res

    def _poll_due_reminders(self) -> None:
        try:
            newly_due = self.store.due_reminders()
            if newly_due:
                med = newly_due[0]
                msg = f"Reminder: time for {med['name']} ({med['dose_label']}, {med['time']})."
                self.chat_messages.append({"role": "assistant", "text": msg})
                self.last_mood = "reminder"
                self._refresh_banners()
                self._render_current_screen()
                self._sync_avatar_state()
                if self.voice.enabled:
                    self.voice.speak(msg)
            self.root.after(15000, self._poll_due_reminders)
        except tk.TclError:
            pass

    def close(self) -> None:
        try:
            self.panel.destroy()
        except Exception:
            pass
        try:
            self.mascot.destroy()
        except Exception:
            pass
        if self.owns_root:
            try:
                self.root.destroy()
            except Exception:
                pass


def launch_desktop_companion(db_path: Path | str | None = None) -> int:
    """Launch the standalone floating desktop companion connected to Groq."""
    from r2.care_actions import create_app
    from r2.config import load_dotenv

    enable_dpi_awareness()
    load_dotenv()
    os.environ.setdefault("SMARTPOLI_API_KEY", "local-companion-key")
    app = create_app(db_path=db_path or (Path(os.environ.get("R2_DATA_DIR", "data")) / "care.db"))
    companion = CareCompanionApp(
        store=app.state.store,
        agent=app.state.agent,
        provider=app.state.provider,
        smart_actions=app.state.smart_actions,
        model_name=app.state.model,
    )
    companion.expand()
    companion.root.after(200, companion.expand)
    companion.root.mainloop()
    return 0
