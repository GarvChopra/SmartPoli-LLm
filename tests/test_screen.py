import pytest

from r2.tools import screen


def test_screenshot_writes_a_png(tmp_path):
    target = tmp_path / "shot.png"
    try:
        out = screen.screenshot(str(target))
    except Exception as exc:  # no desktop session available
        pytest.skip(f"cannot capture screen here: {exc}")
    assert target.exists() and target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert str(target) in out


def test_screenshot_defaults_to_latest_png(tmp_path, monkeypatch):
    monkeypatch.setattr(screen, "SCREEN_DIR", tmp_path)
    try:
        screen.screenshot()
    except Exception as exc:
        pytest.skip(f"cannot capture screen here: {exc}")
    assert (tmp_path / "latest.png").exists()


def test_format_elements_limits_and_skips_blank_names():
    items = [("Button", "OK"), ("Edit", ""), ("Edit", "Name"), ("Text", "Hello")]
    out = screen.format_elements(items, limit=2)
    assert out.splitlines() == ["Button: OK", "Edit: Name", "...[1 more]"]
    assert screen.format_elements([], limit=5) == "No named UI elements found"


def test_list_windows_returns_text():
    try:
        out = screen.list_windows()
    except Exception as exc:
        pytest.skip(f"UI automation unavailable: {exc}")
    assert isinstance(out, str)
