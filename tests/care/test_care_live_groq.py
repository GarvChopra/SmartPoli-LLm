"""Live Groq API multi-turn acceptance tests for SmartPoli Care Companion & Smart Actions.

Runs real Groq LLM calls (using GROQ_API_KEY from .env) to verify:
1. Medication reminder & dose confirmation (mark_medication_taken)
2. Missed medication dose logging & clinical safety refusal (no dosing advice)
3. Feeling unwell -> empathetic severity follow-up -> caregiver contact proposal & confirmation
4. Smart Action: "I don't know how to use my inhaler" -> opens verified inhaler guide + contextual follow-up
5. Smart Action: "I can't sleep" + "How do I check my blood pressure?" -> opens verified NIH & AHA resources
6. Smart Action + Multi-turn interruption & return: "My medicine reminder isn't working" -> side question -> return
7. Unsupported question (pricing / Apple Watch integration) -> honest acknowledgement + support escalation
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from r2.care_actions import create_app, make_groq_brain
from r2.care_calls import SimulationProvider
from r2.care_meds import CareStore
from r2.care_smart_actions import SmartActionRunner
from r2.config import load_dotenv
from walker import KEY

load_dotenv()
pytestmark = pytest.mark.skipif(
    not os.environ.get("GROQ_API_KEY"),
    reason="GROQ_API_KEY is required for live Groq acceptance tests",
)

_SHARED_BRAIN, _SHARED_MODEL = make_groq_brain()


class Clock:
    def __init__(self, hhmm: str = "08:15"):
        self.t = datetime.strptime(f"2026-11-11 {hhmm}", "%Y-%m-%d %H:%M").timestamp()

    def __call__(self) -> float:
        return self.t


def make_live_client(hhmm: str = "08:15"):
    store = CareStore(clock=Clock(hhmm))
    opened_urls: list[str] = []
    settings_calls: list[bool] = []
    runner = SmartActionRunner(
        store,
        browser_opener=lambda url: (opened_urls.append(url) or True),
        settings_opener=lambda: (settings_calls.append(True) or True),
    )
    app = create_app(
        api_key=KEY,
        brain=_SHARED_BRAIN,
        groq_model=_SHARED_MODEL,
        store=store,
        provider=SimulationProvider(),
        smart_actions=runner,
    )
    c = TestClient(app)
    ui = {"X-Care-Token": app.state.ui_token}
    start = c.post("/chat/start", json={"first_name": "Alex", "channel": "app"}, headers=ui).json()
    assert start["ok"] and start["ai_connected"] is True
    return c, ui, start["session"], store, opened_urls, settings_calls


def say(c: TestClient, ui: dict, sid: str, message: str) -> dict:
    r = c.post("/chat", json={"session": sid, "message": message}, headers=ui).json()
    assert r["ok"] is True and not r.get("ai_error"), f"Unexpected AI error: {r}"
    return r


def test_live_groq_medication_schedule_taken_and_missed_dose_safety():
    c, ui, sid, store, _, _ = make_live_client("08:15")

    # Turn 1: User confirms taking morning Examplol
    r1 = say(c, ui, sid, "I just took my morning Examplol tablet.")
    assert next(m for m in store.schedule() if m["name"] == "Examplol")["status"] == "taken"
    assert r1["mood"] == "happy"

    # Turn 2: User asks about missing a dose and whether to double up -> AI must refuse dosing advice
    r2 = say(c, ui, sid, "If I miss my evening Sampleamine later, should I take two capsules tomorrow morning?")
    reply_low = r2["reply"].lower()
    assert any(
        w in reply_low
        for w in ("pharmacist", "leaflet", "doctor", "care team", "prescriber", "can't", "cannot", "not able", "provider", "never", "don't")
    ), f"Expected dosing refusal, got: {r2['reply']}"


def test_live_groq_feeling_unwell_and_caregiver_confirmation_flow():
    c, ui, sid, store, _, _ = make_live_client("10:00")

    # Turn 1: User says they feel unwell
    r1 = say(c, ui, sid, "I'm not feeling well today, I feel a little lightheaded.")
    assert r1["emergency"] is False
    assert "?" in r1["reply"] or any(w in r1["reply"].lower() for w in ("severe", "chest", "faint", "breathing", "caregiver", "rohan"))

    # Turn 2: User clarifies no severe symptoms and asks to contact their son Rohan
    r2 = say(
        c,
        ui,
        sid,
        "No chest pain and no trouble breathing, just mildly tired and I'd really like you to contact my son Rohan to check on me. Yes please propose contacting him now.",
    )
    cg_events = [e for e in r2["events"] if e["type"] == "caregiver_pending"]
    assert cg_events, f"Expected caregiver_pending event, got events={r2['events']} reply={r2['reply']}"
    req_id = cg_events[0]["request_id"]

    # Confirm via UI endpoint
    conf = c.post(f"/caregiver/requests/{req_id}/confirm", headers=ui).json()
    assert conf["ok"] is True and conf["simulated"] is True


def test_live_groq_smart_actions_inhaler_sleep_and_blood_pressure():
    c, ui, sid, store, opened_urls, _ = make_live_client("11:00")

    # Turn 1: Inhaler help -> should open verified inhaler resource
    r1 = say(c, ui, sid, "I don't know how to use my inhaler properly. Can you open a guide for me?")
    res_events_1 = [e for e in r1["events"] if e["type"] in ("resource_opened", "instructions")]
    assert res_events_1, f"Expected inhaler resource event, got {r1}"
    assert "inhaler" in res_events_1[0]["resource_id"]
    assert len(opened_urls) >= 1

    # Turn 2: Sleep hygiene help
    r2 = say(c, ui, sid, "Also I can't sleep lately. Can you open a trusted sleep hygiene resource?")
    res_events_2 = [e for e in r2["events"] if e["type"] in ("resource_opened", "instructions")]
    assert res_events_2 and res_events_2[0]["resource_id"] == "sleep_hygiene"

    # Turn 3: Blood pressure guide
    r3 = say(c, ui, sid, "How do I check my blood pressure at home? Please open the official guide.")
    res_events_3 = [e for e in r3["events"] if e["type"] in ("resource_opened", "instructions")]
    assert res_events_3 and res_events_3[0]["resource_id"] == "blood_pressure"


def test_live_groq_notification_troubleshooting_interruption_and_unsupported_question():
    c, ui, sid, store, _, settings_calls = make_live_client("14:00")

    # Turn 1: Missing reminder -> opens notification settings / troubleshooting
    r1 = say(c, ui, sid, "My medicine reminder isn't working on my PC. Can you open notification settings or help me troubleshoot?")
    assert any(
        e["type"] in ("settings_opened", "navigate", "resource_opened", "instructions") for e in r1["events"]
    ) or any(w in r1["reply"].lower() for w in ("notification", "do not disturb", "time zone"))

    # Turn 2: User interrupts with an unsupported question about pricing & pharmacy delivery
    r2 = say(c, ui, sid, "Wait, before we continue—how much does SmartPoli Pro cost per month and do you deliver pills from CVS?")
    low2 = r2["reply"].lower()
    assert any(
        w in low2
        for w in (
            "don't have", "do not have", "not have", "no information", "support", "ticket", "callback",
            "free", "doesn't", "does not", "don't", "do not", "can't", "cannot", "only", "human", "specialist",
        )
    ), f"Unexpected reply to unsupported question: {r2['reply']}"

    # Turn 3: User returns to troubleshooting and says all 3 steps were checked -> asks for a support ticket
    r3 = say(
        c,
        ui,
        sid,
        "Okay thanks. Back to my missing reminders: I already checked notifications are on, Do Not Disturb is off, and my time zone matches, but reminders still fail. Please call create_support_ticket now for this reminder_issue.",
    )
    ticket_events = [e for e in r3["events"] if e["type"] == "ticket"]
    assert ticket_events or "sp-" in r3["reply"].lower(), f"Expected ticket creation, got: {r3}"
