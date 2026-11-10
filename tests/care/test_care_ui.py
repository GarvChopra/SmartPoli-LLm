"""End-to-end UI, API, Groq integration, error handling, and responsive layout tests for SmartPoli Care Navigator."""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from r2.brain import OpenAIBrain
from r2.care_actions import GROQ_BASE_URL, create_app, make_groq_brain
from r2.config import load_dotenv
from walker import KEY


def test_care_ui_api_flow_end_to_end(client):
    html = client.get("/")
    assert html.status_code == 200 and "SmartPoli Care Navigator" in html.text
    h = client.get("/health").json()
    assert h["provider"] == "groq"
    # Start in general Q&A mode
    s1 = client.post("/chat/start", json={"first_name": "Sam", "category": "account_help"}).json()
    sid = s1["session"]
    assert "Sam" in s1["reply"]
    # Grounded answer
    ans = client.post("/chat", json={"session": sid, "message": "How late can a reminder arrive?"}).json()
    assert "5 minutes late" in ans["reply"]
    # Unsupported abstention -> escalation menu
    unk = client.post("/chat", json={"session": sid, "message": "Does this work on Apple Watch?"}).json()
    assert "don't have a reliable answer" in unk["reply"]
    cb = client.post("/chat", json={"session": sid, "message": "Callback morning"}).json()
    assert re.search(r"CB-\d{4}", cb["reply"])
    # Trigger 3-step troubleshooting -> ticket
    client.post("/chat", json={"session": sid, "message": "I'm not getting reminders"})
    client.post("/chat", json={"session": sid, "message": "Yes"})
    client.post("/chat", json={"session": sid, "message": "Yes"})
    t = client.post("/chat", json={"session": sid, "message": "Yes"}).json()
    assert re.search(r"SP-\d{4}", t["reply"]) and "don't skip or change doses" in t["reply"].lower()
    # Emergency stop
    em = client.post("/chat", json={"session": sid, "message": "I have chest pain"}).json()
    assert em["emergency"] is True and "911" in em["reply"] and "1-800-222-1222" in em["reply"]


def test_groq_openai_brain_multi_turn_memory_tool_calls_and_error_handling():
    """Verify Groq OpenAIBrain wire protocol: multi-turn history, tool calling, 429 rate-limit, and 500 fallback."""
    seen_payloads: list[dict] = []
    mode = {"state": "normal"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{GROQ_BASE_URL}/chat/completions"
        assert request.headers.get("authorization") == "Bearer gsk_test_secret_key"
        body = json.loads(request.content.decode("utf-8"))
        seen_payloads.append(body)

        if mode["state"] == "rate_limit":
            return httpx.Response(429, json={"error": {"message": "Rate limit reached"}})
        if mode["state"] == "timeout":
            raise httpx.ReadTimeout("Groq request timed out")

        last_user = [m["content"] for m in body["messages"] if m["role"] == "user"][-1].lower()
        if "please open a ticket now" in last_user:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "create_support_ticket",
                                            "arguments": json.dumps(
                                                {
                                                    "category": "reminder_issue",
                                                    "summary": "User checked settings, still missing reminders",
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"Groq grounded reply (turns in memory={len(body['messages'])})",
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    groq_brain, model_name = make_groq_brain(
        api_key="gsk_test_secret_key",
        model="llama-3.3-70b-versatile",
        client=httpx.Client(transport=transport),
    )
    assert isinstance(groq_brain, OpenAIBrain) and model_name == "llama-3.3-70b-versatile"

    c = TestClient(create_app(api_key=KEY, brain=groq_brain))
    assert c.get("/health").json()["ai_connected"] is True

    sid = c.post("/chat/start", json={"first_name": "Maya", "category": "other"}).json()["session"]
    # Turn 1
    r1 = c.post("/chat", json={"session": sid, "message": "How many reminders can I get per day?"}).json()
    assert "Groq grounded reply" in r1["reply"]
    # Turn 2: verify Turn 1 user & assistant messages were included in Groq conversation memory
    r2 = c.post("/chat", json={"session": sid, "message": "And what if I miss one of them?"}).json()
    assert "Groq grounded reply" in r2["reply"]
    roles_sent = [m["role"] for m in seen_payloads[-1]["messages"]]
    assert roles_sent == ["system", "assistant", "user", "assistant", "user"]

    # Turn 3: Groq invokes create_support_ticket tool
    r3 = c.post("/chat", json={"session": sid, "message": "Please open a ticket now"}).json()
    assert re.search(r"SP-\d{4}", r3["reply"]) and r3["reference"].startswith("SP-")

    # Turn 4: Groq returns HTTP 429 rate limit -> graceful message, secret key never leaked
    mode["state"] = "rate_limit"
    r4 = c.post("/chat", json={"session": sid, "message": "Why is it slow?"}).json()
    assert r4.get("ai_error") == "rate_limit" and "rate-limited" in r4["reply"]
    assert "gsk_test_secret_key" not in r4["reply"]

    # Turn 5: Groq times out -> falls back cleanly to KB answer without crashing
    mode["state"] = "timeout"
    r5 = c.post("/chat", json={"session": sid, "message": "What are your support hours?"}).json()
    assert "8 AM to 6 PM Pacific" in r5["reply"]


def test_care_ui_desktop_and_mobile_in_headless_browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(api_key=KEY, use_default_brain=False)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(40):
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

            for vp in [{"width": 1280, "height": 800}, {"width": 390, "height": 844}]:
                page = browser.new_page(viewport=vp)
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/")
                assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
                page.click("#startBtn")
                assert "Please choose" in page.inner_text("#preErr")
                page.fill("#firstName", "Sam")
                page.select_option("#category", "reminder_issue")
                page.click("#startBtn")
                page.wait_for_selector("#chatCard:not([hidden])")
                assert "notifications" in page.inner_text("#log").lower()
                for _ in range(3):
                    page.locator("#quickBar button").first.click()
                page.locator(".msg.ticket").wait_for()
                assert "SP-" in page.inner_text(".msg.ticket")
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


def test_live_groq_five_multi_turn_conversations_when_key_configured():
    """Runs 5 real multi-turn conversations against live Groq API when GROQ_API_KEY is present in env/.env."""
    load_dotenv()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not groq_key:
        pytest.skip("GROQ_API_KEY is not set in environment or .env yet")

    c = TestClient(create_app(api_key=KEY, groq_api_key=groq_key))
    assert c.get("/health").json()["ai_connected"] is True

    # Conversation 1: Grounded Q&A + contextual follow-up
    s1 = c.post("/chat/start", json={"first_name": "Alex", "category": "missed_dose_logging"}).json()["session"]
    a1 = c.post("/chat", json={"session": s1, "message": "How do I log a missed dose and how long can I edit it?"}).json()["reply"]
    assert "24" in a1
    a2 = c.post("/chat", json={"session": s1, "message": "Should I take that missed pill right now?"}).json()["reply"]
    assert any(w in a2.lower() for w in ("pharmacist", "care team", "doctor", "medical", "advise", "advice"))

    # Conversation 2: Unexpected / unsupported questions -> honest abstention
    s2 = c.post("/chat/start", json={"first_name": "Jordan", "category": "other"}).json()["session"]
    b1 = c.post("/chat", json={"session": s2, "message": "How much does SmartPoli cost per month and does it work on Apple Watch?"}).json()["reply"]
    assert any(w in b1.lower() for w in ("don't", "do not", "not have", "no information", "knowledge base", "help@smartpoli.example", "callback"))

    # Conversation 3: Adaptive conversational troubleshooting + topic switch + ticket creation
    s3 = c.post("/chat/start", json={"first_name": "Taylor", "category": "reminder_issue"}).json()["session"]
    d1 = c.post("/chat", json={"session": s3, "message": "Notifications are already allowed and Do Not Disturb is off."}).json()["reply"]
    assert "time" in d1.lower() or "zone" in d1.lower()
    d2 = c.post("/chat", json={"session": s3, "message": "Wait, how late can a reminder normally arrive?"}).json()["reply"]
    assert "5" in d2 or "five" in d2.lower()
    d3 = c.post("/chat", json={"session": s3, "message": "My phone time zone matches my account too, still no reminders."}).json()["reply"]
    assert re.search(r"SP-\d{4}", d3)

    # Conversation 4: Human escalation -> callback booking
    s4 = c.post("/chat/start", json={"first_name": "Morgan", "category": "account_help"}).json()["session"]
    e1 = c.post("/chat", json={"session": s4, "message": "I want to talk to a human agent"}).json()["reply"]
    assert "callback" in e1.lower() or "help@smartpoli.example" in e1.lower()
    e2 = c.post("/chat", json={"session": s4, "message": "Book a callback for the afternoon please"}).json()["reply"]
    assert re.search(r"CB-\d{4}", e2)

    # Conversation 5: Emergency interception mid-conversation
    s5 = c.post("/chat/start", json={"first_name": "Casey", "category": "other"}).json()["session"]
    c.post("/chat", json={"session": s5, "message": "What are your support hours?"})
    f2 = c.post("/chat", json={"session": s5, "message": "I took too many pills and have chest pain"}).json()
    assert f2.get("emergency") is True and "911" in f2["reply"]
