"""Tests for the SmartPoli Care Companion Smart Actions & Proactive Care system."""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from r2.brain import FakeBrain
from r2.care_actions import create_app
from r2.care_meds import CareError, CareStore
from r2.care_smart_actions import (
    TRUSTED_RESOURCES,
    SmartActionRunner,
    find_trusted_resource,
    validate_screen,
)
from r2.types import Decision, ToolCall
from walker import KEY


class Clock:
    def __init__(self, hhmm: str = "09:00"):
        self.t = datetime.strptime(f"2026-11-11 {hhmm}", "%Y-%m-%d %H:%M").timestamp()

    def __call__(self) -> float:
        return self.t


def tool(tool_name: str, **args) -> Decision:
    return Decision(tool_call=ToolCall(tool_name, args))


def test_trusted_resource_catalog_contains_only_verified_https_sources():
    assert len(TRUSTED_RESOURCES) >= 7
    for res in TRUSTED_RESOURCES:
        assert res["url"].startswith("https://")
        assert res["title"] and res["source"] and len(res["steps"]) >= 3


@pytest.mark.parametrize(
    "topic,device,expected_id",
    [
        ("inhaler", "metered-dose puffer", "inhaler_mdi"),
        ("inhaler", "dry powder diskus", "inhaler_dpi"),
        ("inhaler", "soft mist respimat", "inhaler_soft_mist"),
        ("I don't know how to use my inhaler", "", "inhaler_general"),
        ("I can't sleep", "", "sleep_hygiene"),
        ("How do I check my blood pressure?", "", "blood_pressure"),
        ("My medicine reminder isn't working", "", "smartpoli_notifications"),
        ("medication_safety", "", "medication_safety"),
    ],
)
def test_find_trusted_resource_matches_intent_and_device(topic, device, expected_id):
    res = find_trusted_resource(topic, device)
    assert res["id"] == expected_id


def test_find_trusted_resource_rejects_unknown_or_arbitrary_urls():
    with pytest.raises(CareError):
        find_trusted_resource("https://evil.example.com/random-youtube-video")
    with pytest.raises(CareError):
        find_trusted_resource("")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("schedule", "schedule"),
        ("meds", "schedule"),
        ("reminders", "reminders"),
        ("troubleshooting", "troubleshooting"),
        ("settings", "troubleshooting"),
        ("caregiver", "caregiver"),
        ("activity", "activity"),
        ("chat", "chat"),
    ],
)
def test_validate_screen_normalizes_valid_names_and_aliases(raw, expected):
    assert validate_screen(raw) == expected


def test_validate_screen_rejects_unknown_screen():
    with pytest.raises(CareError):
        validate_screen("arbitrary_shell")


def test_smart_action_runner_opens_browser_and_settings_via_injected_openers():
    store = CareStore(clock=Clock())
    opened_urls: list[str] = []
    settings_calls: list[bool] = []

    runner = SmartActionRunner(
        store,
        browser_opener=lambda url: (opened_urls.append(url) or True),
        settings_opener=lambda: (settings_calls.append(True) or True),
    )

    r1 = runner.open_trusted_resource("inhaler", "puffer", open_in_browser=True)
    assert r1["ok"] and r1["opened_in_browser"] is True
    assert r1["resource"]["id"] == "inhaler_mdi"
    assert opened_urls == [r1["resource"]["url"]]

    r2 = runner.open_notification_settings()
    assert r2["ok"] and r2["opened_system_settings"] is True
    assert r2["settings_uri"] == "ms-settings:notifications"
    assert len(settings_calls) == 1

    r3 = runner.navigate_screen("meds", "user asked to see schedule")
    assert r3["ok"] and r3["screen"] == "schedule"

    r4 = runner.show_instructions("blood_pressure")
    assert r4["ok"] and r4["resource"]["id"] == "blood_pressure"

    kinds = [a["kind"] for a in store.activity(10)]
    assert "resource_opened" in kinds
    assert "settings_opened" in kinds
    assert "screen_navigated" in kinds
    assert "instructions_shown" in kinds


def test_agent_executes_smart_action_tools_and_sets_helpful_avatar_state():
    store = CareStore(clock=Clock())
    opened_urls: list[str] = []
    runner = SmartActionRunner(store, browser_opener=lambda url: (opened_urls.append(url) or True))

    brain = FakeBrain([
        tool("open_trusted_resource", topic="inhaler", device_type="metered-dose puffer"),
        Decision(final="[mood:helpful] I opened the official NHLBI metered-dose inhaler guide for you. Do you use a spacer with your puffer?"),
        tool("open_system_notification_settings"),
        Decision(final="[mood:helpful] I've opened your notification settings and the troubleshooting checklist."),
        tool("navigate_companion_screen", screen="schedule", reason="check medications"),
        Decision(final="Here is your medication schedule."),
    ])
    app = create_app(api_key=KEY, brain=brain, store=store, smart_actions=runner)
    c = TestClient(app)
    ui = {"X-Care-Token": app.state.ui_token}
    sid = c.post("/chat/start", json={"first_name": "Alex", "channel": "app"}, headers=ui).json()["session"]

    # Turn 1: inhaler resource
    resp1 = c.post("/chat", json={"session": sid, "message": "I don't know how to use my puffer inhaler"}, headers=ui).json()
    assert resp1["ok"] and resp1["mood"] == "helpful" and resp1["avatar_state"] == "helpful"
    ev1 = next(e for e in resp1["events"] if e["type"] == "resource_opened")
    assert ev1["resource_id"] == "inhaler_mdi" and ev1["opened_in_browser"] is True
    assert len(opened_urls) == 1 and "nhlbi.nih.gov" in opened_urls[0]

    # Turn 2: notification settings
    resp2 = c.post("/chat", json={"session": sid, "message": "My medicine reminder isn't working"}, headers=ui).json()
    assert resp2["avatar_state"] == "helpful"
    ev2 = next(e for e in resp2["events"] if e["type"] == "settings_opened")
    assert ev2["settings_uri"] == "ms-settings:notifications" and ev2["screen"] == "troubleshooting"

    # Turn 3: screen navigation promotes neutral mood to helpful
    resp3 = c.post("/chat", json={"session": sid, "message": "Show me my schedule"}, headers=ui).json()
    assert resp3["mood"] == "helpful" and resp3["avatar_state"] == "helpful"
    ev3 = next(e for e in resp3["events"] if e["type"] == "navigate")
    assert ev3["screen"] == "schedule"


def test_propose_and_confirm_new_medication_reminder_workflow():
    store = CareStore(clock=Clock())
    brain = FakeBrain([
        tool("propose_medication_reminder", name="Vitamin D", time="09:30", dose_label="1000 IU"),
        Decision(final="I've prepared a reminder for Vitamin D at 09:30. Please tap Confirm Reminder to add it."),
    ])
    app = create_app(api_key=KEY, brain=brain, store=store)
    c = TestClient(app)
    ui = {"X-Care-Token": app.state.ui_token}
    sid = c.post("/chat/start", json={"first_name": "Alex", "channel": "app"}, headers=ui).json()["session"]

    resp = c.post("/chat", json={"session": sid, "message": "Remind me to take Vitamin D at 09:30"}, headers=ui).json()
    assert resp["avatar_state"] == "reminder"
    ev = next(e for e in resp["events"] if e["type"] == "reminder_pending")
    pid = ev["proposal_id"]

    # Not yet in active schedule before confirmation
    assert "Vitamin D" not in {m["name"] for m in store.schedule()}

    # Confirm the proposal
    conf = c.post(f"/meds/proposals/{pid}/confirm", headers=ui).json()
    assert conf["ok"] and conf["status"] == "confirmed"
    assert "Vitamin D" in {m["name"] for m in store.schedule()}

    # Cannot confirm twice
    assert c.post(f"/meds/proposals/{pid}/confirm", headers=ui).status_code == 422


def test_smart_action_http_endpoints_validate_and_require_auth():
    store = CareStore(clock=Clock())
    app = create_app(api_key=KEY, use_default_brain=False, store=store)
    c = TestClient(app)
    ui = {"X-Care-Token": app.state.ui_token}

    assert c.get("/resources").status_code == 401
    res_list = c.get("/resources", headers=ui).json()
    assert res_list["ok"] and len(res_list["resources"]) >= 7

    opened = c.post("/smart-actions/open-resource", json={"topic": "sleep_hygiene", "open_in_browser": False}, headers=ui).json()
    assert opened["ok"] and opened["resource"]["id"] == "sleep_hygiene"

    nav = c.post("/smart-actions/navigate", json={"screen": "troubleshooting", "reason": "test"}, headers=ui).json()
    assert nav["ok"] and nav["screen"] == "troubleshooting"

    settings = c.post("/smart-actions/open-settings", headers=ui).json()
    assert settings["ok"] and settings["settings_uri"] == "ms-settings:notifications"
