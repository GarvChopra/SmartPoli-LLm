"""The Privacy ledger and Skills panels in a real Chrome: they render, show server text as text (no HTML
injection), approve a proposed skill, and cause no console errors."""
import socket
import threading
import time

import pytest
import uvicorn

from r2.brain import FakeBrain
from r2.config import Settings
from r2.privacy import EgressLedger
from r2.runtime import build_runtime
from r2.server import create_app
from r2.skills import SkillStore
from r2.tools import ToolRegistry
from r2.types import Decision


@pytest.fixture
def live_server(tmp_path):
    ledger = EgressLedger(tmp_path / "egress.jsonl")
    ledger.append({"route": "deep", "model": "nvidia/ultra", "reason": "request asks for planning or analysis", "left_pc": True, "fallback": False})
    ledger.append({"host": "api.tokenfactory.nebius.com", "model": "nvidia/ultra", "messages": 3, "bytes": 812,
                   "masked": {"EMAIL": 2}, "preview": "<img src=x onerror=window.__xss=1>", "outcome": "HTTP 200"})
    ledger.append({"route": "local", "model": "qwen2.5:3b", "reason": "sensitive data found (CARD); kept on this PC", "left_pc": False, "fallback": False})
    SkillStore(tmp_path, bundled=None).propose("pack-bag", "Make a packing list <b>x</b>", "List the items.")
    rt = build_runtime(
        Settings(data_dir=tmp_path, db_path=tmp_path / "t.db"), brain=FakeBrain([Decision(final="hi")]), registry=ToolRegistry()
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
    yield rt, f"http://127.0.0.1:{port}", tmp_path
    server.should_exit = True
    thread.join(5)


def test_ledger_and_skills_panels_in_a_real_browser(live_server):
    sync_api = pytest.importorskip("playwright.sync_api")
    rt, url, data_dir = live_server
    code = rt.auth.new_pairing_code()
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome available: {exc}")
        page = browser.new_page(viewport={"width": 390, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(f"{url}/?code={code}")
        page.click("#pairBtn")
        page.wait_for_selector("#app:not([hidden])")

        page.locator("#ledgerSummary", has_text="1 request(s) sent").wait_for()
        summary = page.inner_text("#ledgerSummary")
        assert "812 bytes" in summary and "2 EMAIL" in summary and "1 of 2 routed requests stayed on this PC" in summary
        rows = page.inner_text("#ledgerRows")
        assert "stayed on this PC" in rows and "left this PC" in rows and "sent to api.tokenfactory.nebius.com" in rows
        assert page.evaluate("() => window.__xss") is None  # preview text is shown as text, never run

        assert page.locator("#skillsList", has_text="pack-bag").count() == 0  # not active yet
        page.locator("#proposedList", has_text="pack-bag").wait_for()
        assert page.locator("#proposedList b").count() == 0  # the description's <b> is text, not markup
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")

        page.locator("#proposedList button", has_text="Approve").click()
        page.locator("#skillsList", has_text="pack-bag").wait_for()
        assert SkillStore(data_dir, bundled=None).get("pack-bag") is not None
        assert page.locator("#proposedList li").count() == 0

        assert not [e for e in errors if "401" not in e and "Unauthorized" not in e]
        browser.close()


def test_proposals_show_their_instructions_and_the_demo_notice_follows_the_profile(live_server):
    sync_api = pytest.importorskip("playwright.sync_api")
    rt, url, data_dir = live_server
    code = rt.auth.new_pairing_code()
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome available: {exc}")
        page = browser.new_page(viewport={"width": 390, "height": 800})
        page.goto(f"{url}/?code={code}")
        page.click("#pairBtn")
        page.wait_for_selector("#app:not([hidden])")
        page.locator("#proposedList pre", has_text="List the items.").wait_for()
        assert not page.is_visible("#demoNote")  # this server is not the demo profile
        browser.close()
