"""Web preview page (GET /) in a real headless browser, desktop and mobile, with a scripted model for determinism.
Real Groq conversations are in test_care_live_groq.py."""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from r2.brain import FakeBrain
from r2.care_actions import create_app
from r2.care_meds import CareStore
from r2.types import Decision
from walker import KEY


def test_web_preview_desktop_and_mobile_in_headless_browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    brain = FakeBrain([Decision(final="[mood:neutral] Your next medicine is Examplol at 08:00.")] * 4)
    app = create_app(api_key=KEY, brain=brain, store=CareStore())
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(60):
        if srv.started:
            break
        time.sleep(0.05)
    try:
        with sync_api.sync_playwright() as p:
            browser = None
            for kw in ({"channel": "msedge", "headless": True}, {"channel": "chrome", "headless": True}, {"headless": True}):
                try:
                    browser = p.chromium.launch(**kw)
                    break
                except Exception:
                    continue
            if browser is None:
                pytest.skip("no Chromium/Edge/Chrome browser available")
            for vp in ({"width": 1280, "height": 800}, {"width": 390, "height": 844}):
                page = browser.new_page(viewport=vp)
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/")
                assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
                page.click("#startBtn")
                assert "Please choose" in page.inner_text("#preErr")
                page.select_option("#category", "other")
                page.click("#startBtn")
                page.wait_for_selector("#chatCard:not([hidden])")
                assert "Alex" in page.inner_text("#log") or "Hi" in page.inner_text("#log")
                page.fill("#msgInput", "when is my next medicine?")
                page.click("#sendBtn")
                page.locator(".msg.bot", has_text="Examplol at 08:00").wait_for()
                page.fill("#msgInput", "I have chest pain")
                page.click("#sendBtn")
                page.locator(".msg.emergency").wait_for()
                assert "911" in page.inner_text(".msg.emergency")
                assert not errors
                page.close()
            browser.close()
    finally:
        srv.should_exit = True
        th.join(timeout=3)
