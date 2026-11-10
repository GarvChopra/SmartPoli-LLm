"""The R2 companion: a small floating shield (graphite body, silver edge) whose glowing blue
"face" shows what R2 is doing. The ten moods come from the user's own design sheet:
idle, focused, thinking, curious, happy, confused, working, sleeping, alert, supportive.

`avatar_state` and `outcome_state` decide the mood (pure functions). `render_avatar` paints it with
Pillow at 4x resolution and scales it down, which gives smooth anti-aliased edges, gradients and a soft
glow (tkinter's own canvas drawing is jagged). `Companion` is the frameless always-on-top window."""
from __future__ import annotations

import math
import tkinter as tk
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageTk

STATES = (
    "idle", "focused", "thinking", "curious", "happy", "confused",
    "working", "sleeping", "alert", "supportive",
)
ANIMATED = {"thinking", "working", "alert", "sleeping"}
PHASE_STEPS = 12  # animation frames per cycle (also the cache granularity)

KEY = "#010203"  # this colour is made see-through, so only the shield shows on the desktop
KEY_RGB = (1, 2, 3)
LED = (82, 209, 255, 255)
WHITE = (255, 255, 255, 255)
AMBER = (245, 165, 36, 255)
AMBER_DIM = (122, 82, 18, 255)
SS = 4  # supersampling factor

_OUTER = [(12, 12), (72, 12), (76, 20), (76, 48), (66, 64), (42, 76), (18, 64), (8, 48), (8, 20)]
_INNER = [(17, 17), (67, 17), (70, 23), (70, 47), (62, 59), (42, 69), (22, 59), (14, 47), (14, 23)]


# ---------------------------------------------------------------------------
# Which mood to show
# ---------------------------------------------------------------------------
def avatar_state(status: dict | None, chat_open: bool = False, typing: bool = False, recent: str | None = None) -> str:
    if status is None:
        return "sleeping"  # R2 is not running
    if status.get("pending"):
        return "alert"  # needs your approval
    if status.get("paused"):
        return "sleeping"
    if status.get("running"):
        return "working" if status.get("partial") else "thinking"
    if recent:
        return recent
    if typing:
        return "curious"
    return "focused" if chat_open else "idle"


def outcome_state(result: dict | None) -> str | None:
    """The mood for a task that just finished."""
    if not result:
        return None
    if result.get("status") == "done":
        return "happy" if result.get("steps", 0) > 0 else "supportive"
    return "confused"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _chaikin(points: list[tuple[float, float]], rounds: int = 3) -> list[tuple[float, float]]:
    """Round the corners of a closed polygon (corner cutting)."""
    for _ in range(rounds):
        cut = []
        for i, p in enumerate(points):
            q = points[(i + 1) % len(points)]
            cut.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
            cut.append((0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]))
        points = cut
    return points


def _mask(width: int, polygon: list[tuple[float, float]], k: float) -> Image.Image:
    mask = Image.new("L", (width, width), 0)
    ImageDraw.Draw(mask).polygon(_chaikin([(x * k, y * k) for x, y in polygon]), fill=255)
    return mask


def _gradient(width: int, top: tuple, bottom: tuple) -> Image.Image:
    return ImageOps.colorize(Image.linear_gradient("L").resize((width, width)), black=top, white=bottom).convert("RGBA")


@lru_cache(maxsize=16)
def _base(px: int) -> Image.Image:
    """The shield itself (the same for every mood), at SS x resolution."""
    width = px * SS
    k = width / 84
    out = Image.new("RGBA", (width, width), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (width, width), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse([20 * k, 72 * k, 64 * k, 82 * k], fill=(0, 0, 0, 140))
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(2.2 * k)))

    outer = _mask(width, _OUTER, k)  # anodised silver edge, lit from above
    silver = _gradient(width, (226, 232, 241), (98, 106, 120))
    silver.putalpha(outer)
    out = Image.alpha_composite(out, silver)

    inner = _mask(width, _INNER, k)  # matte graphite body
    graphite = _gradient(width, (58, 63, 73), (17, 19, 24))
    graphite.putalpha(inner)
    out = Image.alpha_composite(out, graphite)

    gloss = Image.new("L", (width, width), 0)  # soft sheen across the upper body
    ImageDraw.Draw(gloss).ellipse([12 * k, 4 * k, 72 * k, 40 * k], fill=30)
    gloss = ImageChops.multiply(gloss.filter(ImageFilter.GaussianBlur(3 * k)), inner)
    out = Image.alpha_composite(out, Image.merge("RGBA", (*(Image.new("L", (width, width), 255),) * 3, gloss)))

    rim = Image.new("RGBA", (width, width), (0, 0, 0, 0))  # thin dark seam between edge and body
    ImageDraw.Draw(rim).line(_chaikin([(x * k, y * k) for x, y in _INNER]) + [_chaikin([(x * k, y * k) for x, y in _INNER])[0]],
                             fill=(8, 9, 12, 190), width=max(1, int(1.1 * k)))
    return Image.alpha_composite(out, rim)


def _line(draw: ImageDraw.ImageDraw, points, width: float, color) -> None:
    """A polyline with round ends and joints."""
    draw.line(points, fill=color, width=max(1, round(width)), joint="curve")
    r = width / 2
    for x, y in (points[0], points[-1]):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def _glyph(draw: ImageDraw.ImageDraw, state: str, k: float, phase: float) -> None:
    def pts(*flat):
        return [(flat[i] * k, flat[i + 1] * k) for i in range(0, len(flat), 2)]

    w = 3.2 * k
    if state == "idle":
        _line(draw, pts(30, 40, 54, 40), w, LED)
    elif state == "focused":
        _line(draw, pts(24, 40, 60, 40), 8 * k, (30, 90, 117, 120))
        _line(draw, pts(26, 40, 58, 40), w, LED)
    elif state == "thinking":
        for i in range(3):
            angle = (phase + i / 3) * 2 * math.pi
            x, y = (42 + 11 * math.cos(angle)) * k, (40 + 11 * math.sin(angle)) * k
            r = (2.2 + 1.2 * ((i + 1) / 3)) * k
            draw.ellipse([x - r, y - r, x + r, y + r], fill=LED)
    elif state == "curious":
        _line(draw, pts(31, 46, 38, 33, 45, 43, 53, 30), w, LED)
    elif state == "happy":
        draw.arc([31 * k, 28 * k, 53 * k, 50 * k], 25, 155, fill=LED, width=round(w))
    elif state == "confused":
        wave = [((28 + 27 * t / 24) * k, (40 + 4.5 * math.sin(t / 24 * 2.5 * math.pi)) * k) for t in range(25)]
        _line(draw, wave, w, LED)
    elif state == "working":
        flare = 4 + 5 * math.sin(phase * math.pi) ** 2
        _line(draw, pts(24, 40, 56, 40), w, LED)
        _line(draw, pts(58 - flare, 40, 58 + flare, 40), 2 * k, WHITE)
        _line(draw, pts(58, 40 - flare, 58, 40 + flare), 2 * k, WHITE)
    elif state == "sleeping":
        color = AMBER if math.sin(phase * 2 * math.pi) > 0 else AMBER_DIM  # slow breathing
        _line(draw, pts(42, 30, 42, 50), w, color)
    elif state == "alert":
        reach = 9 + 4 * math.sin(phase * math.pi) ** 2
        for i in range(8):  # an eight-point star
            angle = i * math.pi / 4
            length = reach if i % 2 == 0 else reach * 0.55
            end = (42 + length * math.cos(angle), 40 + length * math.sin(angle))
            _line(draw, pts(42, 40, *end), 2 * k, LED if i % 2 == 0 else (191, 234, 255, 255))
    elif state == "supportive":
        draw.arc([26 * k, 24 * k, 58 * k, 54 * k], 25, 155, fill=(30, 90, 117, 140), width=round(8 * k))
        draw.arc([26 * k, 24 * k, 58 * k, 54 * k], 25, 155, fill=LED, width=round(w))


def render_avatar(state: str, phase: float = 0.0, size: int = 84, background: tuple | None = None) -> Image.Image:
    """The shield with the given mood, `size` pixels square. With `background` it is composited on
    that colour (RGB); otherwise it is RGBA with a transparent surround."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    width = size * SS
    face = Image.new("RGBA", (width, width), (0, 0, 0, 0))
    _glyph(ImageDraw.Draw(face), state, width / 84, phase)
    glow = face.filter(ImageFilter.GaussianBlur(2.6 * width / 84))
    glow.putalpha(glow.getchannel("A").point(lambda a: min(255, int(a * 1.9))))
    image = Image.alpha_composite(_base(size), glow)
    image = Image.alpha_composite(image, face).resize((size, size), Image.LANCZOS)
    if background is None:
        return image
    flat = Image.new("RGB", (size, size), background)
    flat.paste(image, (0, 0), image)
    return flat


_frames: dict = {}


def get_frame(state: str, phase: float, size: int) -> Image.Image:
    """A cached frame on the see-through key colour; animation steps are quantised so a handful of
    frames per face is all that is ever drawn."""
    step = int(phase * PHASE_STEPS) % PHASE_STEPS if state in ANIMATED else 0
    key = (state, step, size)
    if key not in _frames:
        _frames[key] = render_avatar(state, step / PHASE_STEPS, size, background=KEY_RGB)
    return _frames[key]


# ---------------------------------------------------------------------------
# The floating window
# ---------------------------------------------------------------------------
class Companion(tk.Toplevel):
    """Frameless, always on top, see-through around the shield. Click it to open or close the chat;
    drag it to move it. `scale` is the screen's DPI scale, so it stays the same physical size and
    crisp on high-DPI displays."""

    def __init__(self, master, on_click, size: int = 84, scale: float = 1.0):
        super().__init__(master)
        self._on_click = on_click
        self._px = max(24, int(round(size * scale)))
        self.state_name = "sleeping"
        self._phase = 0.0
        self._photo = None
        self._press_at: tuple[int, int] | None = None
        self._moved = False
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=KEY)
        try:
            self.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(self, width=self._px, height=self._px, bg=KEY, highlightthickness=0, cursor="hand2")
        self.canvas.pack()
        self.geometry(
            f"{self._px}x{self._px}+{self.winfo_screenwidth() - self._px - int(24 * scale)}"
            f"+{self.winfo_screenheight() - self._px - int(64 * scale)}"
        )
        self.canvas.bind("<ButtonPress-1>", lambda e: self._press(e.x_root, e.y_root))
        self.canvas.bind("<B1-Motion>", lambda e: self._drag_to(e.x_root, e.y_root))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._release())
        self._render()
        self.after(120, self._animate)

    @property
    def pixel_size(self) -> int:
        return self._px

    def set_state(self, name: str) -> None:
        if name not in STATES:
            raise ValueError(f"unknown state {name!r}")
        if name != self.state_name:
            self.state_name = name
            self._render()

    def _render(self) -> None:
        self._photo = ImageTk.PhotoImage(get_frame(self.state_name, self._phase, self._px), master=self)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def _animate(self) -> None:
        try:
            if self.state_name in ANIMATED:
                self._phase = (self._phase + 1 / PHASE_STEPS) % 1.0
                self._render()
            self.after(120, self._animate)
        except tk.TclError:  # the window was closed
            pass

    def _press(self, x_root: int, y_root: int) -> None:
        self._press_at, self._moved = (x_root, y_root), False
        self._origin = (self.winfo_x(), self.winfo_y())

    def _drag_to(self, x_root: int, y_root: int) -> None:
        if self._press_at is None:
            return
        dx, dy = x_root - self._press_at[0], y_root - self._press_at[1]
        if abs(dx) + abs(dy) > 4:
            self._moved = True
        if self._moved:
            self.geometry(f"+{self._origin[0] + dx}+{self._origin[1] + dy}")

    def _release(self) -> None:
        was_click = self._press_at is not None and not self._moved
        self._press_at = None
        if was_click:
            self._on_click()
