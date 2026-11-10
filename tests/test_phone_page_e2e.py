import socket
import threading
import time

import pytest
import uvicorn

from r2.brain import FakeBrain
from r2.config import Settings
from r2.runtime import build_runtime
from r2.server import create_app
from r2.tools import ToolRegistry, ToolSpec
from r2.types import Decision, Risk, RiskInfo, ToolCall

NO_ARGS = {"type": "object", "properties": {}}


@pytest.fixture
def live_server(tmp_path):
    reg = ToolRegistry()
    reg.register(
        ToolSpec("submit_form", "Submit", {"type": "object", "properties": {}},
                 lambda: "submitted", RiskInfo(Risk.CONFIRM, "submit"))
    )
    script = [
        Decision(tool_call=ToolCall("submit_form", {"note": "<img src=x onerror=window.__xss=1>"})),
        Decision(final="all <b>done</b>"),
    ]
    rt = build_runtime(
        Settings(data_dir=tmp_path, db_path=tmp_path / "t.db"), brain=FakeBrain(script), registry=reg
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(rt), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    yield rt, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_pair_send_approve_in_a_real_browser(live_server):
    sync_api = pytest.importorskip("playwright.sync_api")
    rt, url = live_server
    code = rt.auth.new_pairing_code()
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome available: {exc}")
        page = browser.new_page(viewport={"width": 390, "height": 844})  # phone size
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(f"{url}/?code={code}")
        assert page.locator("#code").input_value() == code
        page.click("#pairBtn")
        page.wait_for_selector("#app:not([hidden])")

        page.fill("#msg", "fill the form")
        page.click("#send")
        page.wait_for_selector("#approval:not([hidden])")
        assert page.inner_text("#apTool") == "submit_form"
        # the hostile text is shown as text, never run as HTML
        assert "<img src=x" in page.inner_text("#apArgs")
        assert page.evaluate("() => window.__xss") is None
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")

        page.click("#apYes")
        page.wait_for_selector("#resultCard:not([hidden])")
        page.locator("#resAnswer", has_text="all").wait_for()
        assert page.inner_text("#resAnswer") == "all <b>done</b>"
        assert page.locator("#resAnswer b").count() == 0
        page.wait_for_function("() => document.getElementById('chip').textContent === 'idle'")  # status polls every 0.4-1.5 s

        page.click("#signout")
        page.wait_for_selector("#pair:not([hidden])")
        # failed console errors should be none other than the expected 401s from signed-out polling
        assert not [e for e in errors if "401" not in e and "Unauthorized" not in e]
        browser.close()
