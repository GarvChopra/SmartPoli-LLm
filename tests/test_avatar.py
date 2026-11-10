"""The R2 companion: a floating graphite-and-silver shield whose blue LED "face" shows what R2 is doing
(idle, focused, thinking, curious, happy, confused, working, sleeping, alert, supportive)."""
import time

import pytest

from r2.avatar import STATES, Companion, avatar_state, get_frame, outcome_state, render_avatar

IDLE = {"running": False, "paused": False, "pending": None, "result": None, "partial": ""}


# ---------- which face to show ----------
def test_the_ten_states_of_the_design_exist():
    assert set(STATES) == {
        "idle", "focused", "thinking", "curious", "happy", "confused", "working", "sleeping", "alert", "supportive",
    }


def test_state_follows_what_r2_is_doing():
    assert avatar_state(None) == "sleeping"  # R2 is not running
    assert avatar_state(IDLE) == "idle"
    assert avatar_state(IDLE, chat_open=True) == "focused"
    assert avatar_state(IDLE, typing=True) == "curious"
    assert avatar_state({**IDLE, "paused": True}) == "sleeping"
    assert avatar_state({**IDLE, "running": True}) == "thinking"  # reading the request
    assert avatar_state({**IDLE, "running": True, "partial": "Hel"}) == "working"  # answering
    pending = {"id": "a", "tool": "run_shell", "args": {}}
    assert avatar_state({**IDLE, "running": True, "pending": pending}) == "alert"


def test_a_just_finished_outcome_beats_idle_but_not_live_work():
    assert avatar_state(IDLE, recent="happy") == "happy"
    assert avatar_state(IDLE, recent="confused", chat_open=True, typing=True) == "confused"
    assert avatar_state({**IDLE, "running": True}, recent="happy") == "thinking"


def test_outcomes_map_to_moods():
    assert outcome_state({"status": "done", "steps": 0}) == "supportive"  # a friendly chat reply
    assert outcome_state({"status": "done", "steps": 2}) == "happy"  # an action got done
    for status in ("failed", "stopped", "step_limit"):
        assert outcome_state({"status": status, "steps": 1}) == "confused"
    assert outcome_state(None) is None


# ---------- drawing: crisp, smooth, anti-aliased ----------
def test_render_returns_an_image_of_the_requested_size():
    for size in (42, 84, 168):
        img = render_avatar("idle", size=size)
        assert img.size == (size, size) and img.mode == "RGBA"


def test_the_shield_has_smooth_edges_gradients_and_a_see_through_surround():
    img = render_avatar("idle", size=168)
    assert img.getpixel((0, 0))[3] == 0  # outside the shield is transparent
    assert img.getpixel((84, 60))[3] == 255  # the body is solid
    colors = img.getcolors(maxcolors=100000)
    assert len(colors) > 200  # gradients and anti-aliased edges, not a few flat jagged colours


def test_edges_blend_smoothly_instead_of_jumping():
    img = render_avatar("idle", size=168)
    row = [img.getpixel((x, 60))[3] for x in range(0, 40)]
    partial = [a for a in row if 0 < a < 255]
    assert len(partial) >= 1  # at least one soft, partly transparent edge pixel


def test_composited_on_a_key_colour_it_is_rgb_with_that_colour_around_the_shield():
    img = render_avatar("idle", size=84, background=(1, 2, 3))
    assert img.mode == "RGB" and img.getpixel((0, 0)) == (1, 2, 3)


@pytest.mark.parametrize("state", STATES)
def test_every_state_renders(state):
    assert render_avatar(state, size=84).size == (84, 84)


def test_every_state_looks_different():
    seen = {render_avatar(state, size=84).tobytes() for state in STATES}
    assert len(seen) == len(STATES)


@pytest.mark.parametrize("state", ["thinking", "working", "alert"])
def test_busy_faces_animate(state):
    assert render_avatar(state, phase=0.0).tobytes() != render_avatar(state, phase=0.5).tobytes()


def test_the_sleeping_light_breathes():
    assert render_avatar("sleeping", phase=0.25).tobytes() != render_avatar("sleeping", phase=0.75).tobytes()


def test_resting_faces_are_still():
    assert render_avatar("idle", phase=0.0).tobytes() == render_avatar("idle", phase=0.5).tobytes()


def test_unknown_state_is_rejected():
    with pytest.raises(ValueError):
        render_avatar("angry")


def test_frames_are_cached_so_animation_is_cheap():
    a = get_frame("thinking", 0.01, 84)
    b = get_frame("thinking", 0.02, 84)  # same animation step
    assert a is b
    assert get_frame("thinking", 0.5, 84) is not a
    assert get_frame("idle", 0.0, 84) is get_frame("idle", 0.9, 84)  # still faces ignore the phase


# ---------- the floating window ----------
def tk_root():
    tk = pytest.importorskip("tkinter")
    last = None
    for _ in range(5):  # Tk on Windows Store Python occasionally fails to locate tk.tcl on a repeat start
        try:
            root = tk.Tk()
            root.withdraw()
            return root
        except tk.TclError as exc:
            last = exc
            time.sleep(0.3)
    pytest.skip(f"no usable Tk here: {last}")


def test_companion_changes_face_and_rejects_unknown_ones():
    root = tk_root()
    try:
        c = Companion(root, on_click=lambda: None)
        c.withdraw()
        assert c.state_name == "sleeping"
        c.set_state("happy")
        assert c.state_name == "happy"
        with pytest.raises(ValueError):
            c.set_state("angry")
        c.destroy()
    finally:
        root.destroy()


def test_companion_grows_with_the_screen_scale_so_it_stays_crisp_on_high_dpi_screens():
    root = tk_root()
    try:
        c = Companion(root, on_click=lambda: None, size=84, scale=1.5)
        c.withdraw()
        assert int(c.canvas["width"]) == 126 and int(c.canvas["height"]) == 126
        c.destroy()
    finally:
        root.destroy()


def test_a_click_opens_the_chat_but_a_drag_only_moves_the_companion():
    root = tk_root()
    clicks = []
    try:
        c = Companion(root, on_click=lambda: clicks.append(1))
        c.withdraw()
        c._press(500, 500)
        c._release()
        assert clicks == [1]
        c._press(500, 500)
        c._drag_to(560, 540)
        c._release()
        assert clicks == [1]  # it was dragged, not clicked
        c.destroy()
    finally:
        root.destroy()
