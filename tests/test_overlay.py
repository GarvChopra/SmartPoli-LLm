import json

import httpx
import pytest

from r2.auth import AuthStore
from r2.overlay import OverlayClient, OverlayWindow, describe, ensure_token, transcript_lines


def client_for(handler):
    return OverlayClient("http://localhost:8765", "tok123", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_status_sends_the_token_and_returns_json():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["path"] = request.url.path
        return httpx.Response(200, json={"running": False})

    assert client_for(handler).status() == {"running": False}
    assert seen == {"auth": "Bearer tok123", "path": "/api/status"}


def test_status_is_none_when_r2_is_not_running_or_token_rejected():
    def refuse(request):
        raise httpx.ConnectError("down")

    assert client_for(refuse).status() is None
    assert client_for(lambda r: httpx.Response(401, json={"detail": "x"})).status() is None


def test_send_returns_none_on_success_and_the_reason_on_failure():
    bodies = []

    def ok(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"task_id": "a"})

    assert client_for(ok).send("open notepad") is None
    assert bodies == [{"message": "open notepad"}]
    busy = client_for(lambda r: httpx.Response(409, json={"detail": "a task is already running"}))
    assert busy.send("x") == "a task is already running"
    down = client_for(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert "not running" in down.send("x").lower()


def test_approve_and_stop_hit_the_right_endpoints():
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"ok": True})

    c = client_for(handler)
    assert c.approve("abc", True) is True
    assert c.stop() is True
    assert calls == [("/api/approve", {"id": "abc", "approve": True}), ("/api/stop", None)]


def test_describe_states():
    assert describe(None)[0] == "R2 is not running"
    assert describe({"running": False, "paused": False, "pending": None, "result": None})[0] == "Idle"
    assert describe({"running": True, "paused": False, "pending": None, "result": None})[0] == "Working..."
    assert describe({"running": False, "paused": True, "pending": None, "result": None})[0] == "Paused"
    waiting = describe({"running": True, "paused": False, "pending": {"tool": "run_shell", "args": {"command": "dir"}}, "result": None})
    assert waiting[0] == "Needs your approval" and "run_shell" in waiting[1]
    done = describe({"running": False, "paused": False, "pending": None, "result": {"status": "done", "answer": "ok!", "steps": 1}})
    assert done == ("Idle", "ok!")


def test_ensure_token_creates_reuses_and_replaces_revoked(tmp_path):
    auth = AuthStore(tmp_path / "tokens.json")
    first = ensure_token(auth, tmp_path)
    assert auth.verify(first) and (tmp_path / "overlay.token").read_text().strip() == first
    assert ensure_token(auth, tmp_path) == first  # reused, no token pile-up
    assert auth.count() == 1
    auth.revoke_all()
    second = ensure_token(auth, tmp_path)
    assert second != first and auth.verify(second)


def test_chat_returns_recent_messages_or_empty_when_unreachable():
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert client_for(lambda r: httpx.Response(200, json=msgs)).chat() == msgs

    def down(request):
        raise httpx.ConnectError("down")

    assert client_for(down).chat() == []


def test_transcript_lines_label_speakers_and_show_streaming_text():
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "tool", "content": "ignored"}]
    assert transcript_lines(msgs, "") == [("user", "You: hi"), ("assistant", "R2: hello")]
    live = transcript_lines(msgs, "typing so far")
    assert live[-1] == ("partial", "R2: typing so far")


def test_transcript_while_working_shows_the_request_before_it_is_saved():
    assert transcript_lines([], "", working_on="open notepad") == [
        ("user", "You: open notepad"), ("partial", "R2: ...")]


class FakeClient:
    def __init__(self):
        self.sent = []
        self.messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"}]
        self.state = {"running": False, "paused": False, "pending": None, "result": None, "partial": ""}

    def status(self):
        return self.state

    def chat(self):
        return self.messages

    def send(self, text):
        self.sent.append(text)

    def approve(self, i, ok):
        return True

    def stop(self):
        return True


def make_window(fake, **kw):
    import time

    tk = pytest.importorskip("tkinter")
    last = None
    for _ in range(5):  # Tk on Windows Store Python occasionally fails to locate tk.tcl on a repeat start
        try:
            win = OverlayWindow(fake, poll_ms=10_000, **kw)
            win.withdraw()
            return win
        except tk.TclError as exc:
            last = exc
            time.sleep(0.3)
    pytest.skip(f"no usable Tk here: {last}")


def test_window_sends_text_and_shows_the_conversation():
    fake = FakeClient()
    win = make_window(fake)
    try:
        win.entry.insert(0, "open notepad")
        win.submit()
        assert fake.sent == ["open notepad"]
        assert win.entry.get() == ""
        win.refresh()
        text = win.transcript.get("1.0", "end")
        assert "You: hello" in text and "R2: hi there" in text
        assert win.headline["text"] == "Idle"
    finally:
        win.destroy()


def test_window_streams_partial_text_and_does_not_flicker_when_nothing_changed():
    fake = FakeClient()
    win = make_window(fake)
    try:
        fake.state = {"running": True, "paused": False, "pending": None, "result": None,
                      "partial": "thinking out loud", "message": "tell me something"}
        win.refresh()
        assert "R2: thinking out loud" in win.transcript.get("1.0", "end")
        renders = win.render_count
        win.refresh()
        assert win.render_count == renders  # same content, no redraw
    finally:
        win.destroy()


def test_escape_hides_instead_of_quitting_and_flag_file_brings_it_back(tmp_path):
    flag = tmp_path / "overlay.show"
    win = make_window(FakeClient(), show_flag=flag)
    try:
        win.deiconify()
        win.hide()
        assert win.state() == "withdrawn"
        flag.write_text("1")
        win.check_show_flag()
        assert win.state() == "normal" and not flag.exists()
        win.toggle()
        assert win.state() == "withdrawn"
        win.toggle()
        assert win.state() == "normal"
    finally:
        win.destroy()


def test_input_box_and_buttons_are_visible_inside_the_window_even_with_a_long_chat():
    fake = FakeClient()
    fake.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"line {i} " * 12} for i in range(40)]
    win = make_window(fake)
    try:
        win.deiconify()
        win.update()
        height = win.winfo_height()
        for name, widget in (("entry", win.entry), ("approve", win.approve_btn), ("stop", win.stop_btn)):
            bottom = widget.winfo_rooty() + widget.winfo_height() - win.winfo_rooty()
            assert widget.winfo_ismapped(), name
            assert 0 <= bottom <= height, f"{name} is cut off: bottom {bottom} > window height {height}"
        assert win.entry.winfo_height() >= 20  # a usable text box, not a sliver
    finally:
        win.destroy()


def test_companion_mirrors_the_status_and_celebrates_a_finished_task():
    fake = FakeClient()
    win = make_window(fake)
    clock = {"t": 1000.0}
    win.clock = lambda: clock["t"]
    try:
        base = {"running": False, "paused": False, "pending": None, "result": None, "partial": ""}
        fake.state = base
        win.refresh()
        assert win.companion.state_name == "idle"  # chat hidden
        win.deiconify()
        win.refresh()
        assert win.companion.state_name == "focused"  # chat open
        win.entry.insert(0, "hel")
        win.refresh()
        assert win.companion.state_name == "curious"
        win.entry.delete(0, "end")
        fake.state = {**base, "running": True, "message": "do it"}
        win.refresh()
        assert win.companion.state_name == "thinking"
        fake.state = {**base, "running": True, "partial": "Ope", "message": "do it"}
        win.refresh()
        assert win.companion.state_name == "working"
        fake.state = {**base, "running": True, "pending": {"id": "a", "tool": "run_shell", "args": {}}}
        win.refresh()
        assert win.companion.state_name == "alert"
        fake.state = {**base, "result": {"status": "done", "answer": "ok", "steps": 2}}
        win.refresh()
        assert win.companion.state_name == "happy"  # an action just got done
        clock["t"] += 6
        win.refresh()
        assert win.companion.state_name == "focused"  # back to rest
        fake.state = None
        win.refresh()
        assert win.companion.state_name == "sleeping"  # R2 is not running
    finally:
        win.destroy()


def test_failed_and_chatty_outcomes_get_their_own_faces():
    fake = FakeClient()
    win = make_window(fake)
    win.clock = lambda: 1000.0
    try:
        base = {"running": False, "paused": False, "pending": None, "partial": ""}
        for result, face in (
            ({"status": "failed", "answer": "x", "steps": 1}, "confused"),
            ({"status": "done", "answer": "hi", "steps": 0}, "supportive"),
        ):
            win._was_running = False
            win._recent_until = 0.0
            fake.state = {**base, "running": True}
            win.refresh()
            fake.state = {**base, "result": result}
            win.refresh()
            assert win.companion.state_name == face
    finally:
        win.destroy()


def test_clicking_the_companion_toggles_the_chat_and_it_stays_visible_when_the_chat_hides():
    win = make_window(FakeClient())
    try:
        assert win.state() == "withdrawn"
        win.companion._press(0, 0)
        win.companion._release()
        assert win.state() == "normal"
        win.hide()
        assert win.state() == "withdrawn"
        win.update()
        assert win.companion.winfo_viewable()  # the floating shield is still on screen
    finally:
        win.destroy()


def test_chat_window_is_compact_and_grows_with_the_screen_scale():
    win = make_window(FakeClient(), scale=1.0)
    try:
        win.deiconify()
        win.update()  # a hidden window reports 1x1, so show it before measuring
        assert win.geometry().split("+")[0] == "330x400"
        assert win.companion.pixel_size == 84
    finally:
        win.destroy()
    big = make_window(FakeClient(), scale=1.5)
    try:
        big.deiconify()
        big.update()
        assert big.geometry().split("+")[0] == "495x600"
        assert big.companion.pixel_size == 126
    finally:
        big.destroy()


def test_a_badge_shows_whether_replies_come_from_this_pc_or_the_cloud():
    win = make_window(FakeClient(), brain_label="cloud")
    try:
        assert win.badge["text"] == "cloud"
    finally:
        win.destroy()
    plain = make_window(FakeClient())
    try:
        assert plain.badge["text"] == ""
    finally:
        plain.destroy()
