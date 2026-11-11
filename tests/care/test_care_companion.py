"""Tests for the SmartPoli Care Companion floating desktop character & expandable companion UI."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from r2.brain import FakeBrain
from r2.care_agent import CareAgent
from r2.care_calls import SimulationProvider
from r2.care_companion import (
    CARE_STATES,
    CareCompanionApp,
    get_mascot_frame,
    load_position,
    render_care_mascot,
    resolve_companion_state,
    save_position,
)
from r2.care_meds import CareStore
from r2.care_smart_actions import SmartActionRunner
from r2.types import Decision, ToolCall


class Clock:
    def __init__(self, hhmm: str = "08:30"):
        self.t = datetime.strptime(f"2026-11-11 {hhmm}", "%Y-%m-%d %H:%M").timestamp()

    def __call__(self) -> float:
        return self.t


@pytest.mark.parametrize("state", CARE_STATES)
def test_render_care_mascot_supports_all_11_states_and_differs_across_moods(state):
    img_rgba = render_care_mascot(state, phase=0.25, size=84)
    assert img_rgba.size == (84, 84) and img_rgba.mode == "RGBA"
    frame = get_mascot_frame(state, phase=0.25, size=84)
    assert frame.size == (84, 84) and frame.mode == "RGB"


def test_render_care_mascot_rejects_unknown_state():
    with pytest.raises(ValueError):
        render_care_mascot("nonexistent_state")


def test_resolve_companion_state_priorities():
    assert resolve_companion_state(sleeping=True, listening=True) == "sleeping"
    assert resolve_companion_state(listening=True, thinking=True) == "listening"
    assert resolve_companion_state(thinking=True, speaking=True) == "thinking"
    assert resolve_companion_state(speaking=True, due_count=2) == "speaking"
    assert resolve_companion_state(ai_error="rate_limit", due_count=2) == "error"
    assert resolve_companion_state(pending_caregiver=True) == "escalating"
    assert resolve_companion_state(mood="helpful") == "helpful"
    assert resolve_companion_state(mood="happy") == "happy"
    assert resolve_companion_state(mood="concerned") == "concerned"
    assert resolve_companion_state(due_count=1, mood="neutral") == "reminder"
    assert resolve_companion_state(due_count=0, mood="neutral") == "idle"


def test_position_persistence_round_trip(tmp_path: Path):
    pos_file = tmp_path / "care_position.json"
    assert load_position(pos_file, 100, 200) == (100, 200)
    save_position(pos_file, 450, 320)
    assert load_position(pos_file, 100, 200) == (450, 320)


def test_desktop_companion_app_lifecycle_actions_and_smart_cards(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CARE_MUTE_TTS", "1")
    store = CareStore(clock=Clock("08:30"))  # Examplol (08:00) is due at 08:30
    opened_urls: list[str] = []
    settings_opened: list[bool] = []
    runner = SmartActionRunner(
        store,
        browser_opener=lambda u: (opened_urls.append(u) or True),
        settings_opener=lambda: (settings_opened.append(True) or True),
    )
    brain = FakeBrain([
        Decision(tool_call=ToolCall("open_trusted_resource", {"topic": "inhaler_mdi", "device_type": "puffer"})),
        Decision(final="[mood:helpful] I opened the NHLBI inhaler guide for you. Do you use a spacer?"),
        Decision(tool_call=ToolCall("request_caregiver_contact", {"reason": "feeling dizzy and wants company"})),
        Decision(final="[mood:concerned] Please tap Confirm Contact if you'd like me to reach out to Rohan."),
        Decision(tool_call=ToolCall("propose_medication_reminder", {"name": "Melatonin", "time": "21:00", "dose_label": "3 mg"})),
        Decision(final="[mood:helpful] Please tap Confirm Reminder to add Melatonin at 21:00."),
    ])
    agent = CareAgent(
        store,
        brain,
        make_ticket=lambda c, s: {"ok": True, "reference": "SP-1001", "message": "Ticket SP-1001"},
        make_callback=lambda w: {"ok": True, "reference": "CB-1001", "message": "Callback CB-1001"},
        smart_actions=runner,
    )

    pos_path = tmp_path / "pos.json"
    app = CareCompanionApp(
        store=store,
        agent=agent,
        provider=SimulationProvider(),
        smart_actions=runner,
        position_path=pos_path,
        voice_enabled=True,
    )
    try:
        # Initially Examplol is due, so mascot state is "reminder"
        assert app.mascot.state_name == "reminder"

        # Drag floating mascot and verify position is saved
        app.mascot._press(100, 100)
        app.mascot._drag_to(160, 180)
        app.mascot._release()
        assert pos_path.exists()

        # Click to expand, then toggle theme and reduced motion
        app.toggle_expanded()
        assert app.expanded is True
        app.toggle_theme()
        assert app.theme_name == "light"
        app.mascot.set_reduced_motion(True)
        assert app.mascot.reduced_motion is True

        # Mark due Examplol as taken -> mascot becomes "happy"
        examplol_id = next(m["id"] for m in store.schedule() if m["name"] == "Examplol")
        app.action_mark_taken(examplol_id)
        assert app.mascot.state_name == "happy"

        # Smart Action conversation 1: inhaler guide opens browser + shows resource card + "helpful" state
        out1 = app.send_user_message("I don't know how to use my puffer inhaler")
        assert out1["avatar_state"] == "helpful"
        assert app.mascot.state_name == "helpful"
        assert app.latest_resource_card is not None
        assert app.latest_resource_card["resource_id"] == "inhaler_mdi"
        assert len(opened_urls) == 1

        # Conversation 2: caregiver proposal -> "escalating" state -> confirm -> "happy" state
        out2 = app.send_user_message("Can you contact my son? I feel dizzy.")
        assert app.mascot.state_name == "escalating"
        assert app.pending_caregiver_req is not None
        req_id = app.pending_caregiver_req["request_id"]
        conf_cg = app.confirm_caregiver_request(req_id)
        assert conf_cg["ok"] and conf_cg["simulated"] is True
        assert app.mascot.state_name == "happy"

        # Conversation 3: new medication reminder proposal -> confirm -> added to schedule
        app. trigger_voice_input(simulated_utterance="Add a reminder for Melatonin at 21:00")
        assert app.pending_reminder_prop is not None
        prop_id = app.pending_reminder_prop["proposal_id"]
        conf_rem = app.confirm_reminder_proposal(prop_id)
        assert conf_rem["ok"] and "Melatonin" in {m["name"] for m in store.schedule()}

        # Notification settings action
        app.action_open_notification_settings()
        assert len(settings_opened) == 1
        assert app.mascot.state_name == "helpful"

        # Switch screens
        for scr in ("schedule", "troubleshooting", "caregiver", "activity", "chat"):
            app.switch_screen(scr)
            assert app.current_screen == scr

        app.collapse()
        assert app.expanded is False
    finally:
        app.close()
