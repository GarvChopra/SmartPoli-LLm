import pytest

from r2.tools import files, inputs, apps, shell
from r2.types import Risk


# ---------- files ----------
def test_find_files_matches_case_insensitively_and_limits(tmp_path):
    (tmp_path / "Report.PDF").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "notes.txt").write_text("x")
    (tmp_path / "sub" / "other.pdf").write_text("x")
    out = files.find_files(str(tmp_path), "*.pdf")
    lines = out.splitlines()
    assert lines[0] == "2 matches:"
    lines = lines[1:]
    assert len(lines) == 2
    assert any(l.endswith("Report.PDF") for l in lines)
    limited = files.find_files(str(tmp_path), "*.pdf", limit=1).splitlines()
    assert limited[0] == "1 match (stopped at the limit of 1):" and len(limited) == 2
    assert files.find_files(str(tmp_path), "*.zip") == "No matches"


def test_write_file_creates_and_overwrites(tmp_path):
    p = tmp_path / "a" / "b.txt"
    msg = files.write_file(str(p), "hello")
    assert p.read_text() == "hello"
    assert "5 chars" in msg
    files.write_file(str(p), "bye")
    assert p.read_text() == "bye"


def test_delete_file_removes_files_but_never_directories(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("x")
    files.delete_file(str(p))
    assert not p.exists()
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        files.delete_file(str(d))
    assert d.exists()


# ---------- shell ----------
def test_run_shell_returns_output_and_exit_code():
    out = shell.run_shell("echo hello")
    assert "hello" in out
    assert "exit code 0" in out
    assert "exit code 3" in shell.run_shell("exit 3")


def test_run_shell_times_out():
    with pytest.raises(TimeoutError):
        shell.run_shell("ping -n 6 127.0.0.1", timeout=1)


@pytest.mark.parametrize(
    "cmd",
    [
        "format C:",
        "FORMAT d: /q",
        "diskpart",
        "rmdir /s /q C:\\",
        "rd /s C:\\Windows",
        "Remove-Item -Recurse -Force C:\\",
        "reg delete HKLM\\Software\\X /f",
        "bcdedit /set safeboot minimal",
        "Set-MpPreference -DisableRealtimeMonitoring $true",
        "netsh advfirewall set allprofiles state off",
    ],
)
def test_destructive_shell_commands_are_blocked(cmd):
    assert shell.classify_shell({"command": cmd}).risk is Risk.BLOCKED


@pytest.mark.parametrize("cmd", ["dir", "git status", "echo format later", "rmdir /s /q build"])
def test_other_shell_commands_need_confirmation(cmd):
    info = shell.classify_shell({"command": cmd})
    assert info.risk is Risk.CONFIRM and info.action_type == "shell"


# ---------- apps ----------
@pytest.mark.parametrize("name", ["notepad", "Google Chrome", "calc.exe"])
def test_plain_app_names_are_safe(name):
    assert apps.classify_app({"name": name}).risk is Risk.SAFE


@pytest.mark.parametrize(
    "name",
    ["C:\\Users\\x\\evil.exe", "..\\evil", "run.bat", "x.ps1", "powershell", "cmd", "regedit",
     "a & calc", "x|y", "setup.msi"],
)
def test_risky_app_names_need_confirmation(name):
    assert apps.classify_app({"name": name}).risk is Risk.CONFIRM




# ---------- inputs ----------
class FakePG:
    def __init__(self):
        self.calls = []

    def write(self, text, interval=0):
        self.calls.append(("write", text))

    def scroll(self, n):
        self.calls.append(("scroll", n))

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))

    def press(self, key):
        self.calls.append(("press", key))

    def size(self):
        return (1920, 1080)


@pytest.fixture
def pg(monkeypatch):
    fake = FakePG()
    monkeypatch.setattr(inputs, "_pg", lambda: fake)
    return fake


def test_type_text_ascii_uses_write(pg):
    inputs.type_text("hello")
    assert pg.calls == [("write", "hello")]


def test_type_text_non_ascii_uses_clipboard_paste(pg, monkeypatch):
    pasted = []
    monkeypatch.setattr(inputs, "_set_clipboard", lambda t: pasted.append(t))
    inputs.type_text("नमस्ते")
    assert pasted == ["नमस्ते"]
    assert pg.calls == [("hotkey", ("ctrl", "v"))]


def test_scroll_and_click(pg):
    inputs.scroll(-3)
    inputs.click(10, 20)
    assert pg.calls == [("scroll", -3), ("click", 10, 20)]


def test_click_outside_screen_is_rejected(pg):
    with pytest.raises(ValueError):
        inputs.click(5000, 5)
    assert pg.calls == []


def test_press_key_single_and_combo(pg):
    inputs.press_key("tab")
    inputs.press_key("ctrl+l")
    assert pg.calls == [("press", "tab"), ("hotkey", ("ctrl", "l"))]


@pytest.mark.parametrize("keys", ["tab", "shift+tab", "down", "pagedown", "escape", "Tab"])
def test_navigation_keys_are_safe(keys):
    assert inputs.classify_key({"keys": keys}).risk is Risk.SAFE


@pytest.mark.parametrize("keys", ["enter", "Return"])
def test_enter_counts_as_submit(keys):
    info = inputs.classify_key({"keys": keys})
    assert info.risk is Risk.CONFIRM and info.action_type == "submit"


@pytest.mark.parametrize("keys", ["win+r", "ctrl+alt+delete", "alt+f4", "f5"])
def test_other_keys_need_confirmation(keys):
    info = inputs.classify_key({"keys": keys})
    assert info.risk is Risk.CONFIRM and info.action_type == "keys"


def test_click_is_always_confirm():
    assert inputs.classify_click({"x": 1, "y": 2}).action_type == "click"
