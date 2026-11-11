"""Tests for the medication schedule, reminders, activity log and caregiver records (real SQLite, injected clock)."""
from datetime import datetime

import pytest

from r2.care_meds import MAX_NOTIFICATIONS, RENOTIFY_MINUTES, CareError, CareStore


class Clock:
    def __init__(self, hhmm="07:00", day="2026-11-11"):
        self.t = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").timestamp()

    def __call__(self):
        return self.t

    def at(self, hhmm, day="2026-11-11"):
        self.t = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").timestamp()

    def advance(self, minutes):
        self.t += minutes * 60


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def store(clock):
    return CareStore(clock=clock)


def test_seed_is_synthetic_and_schedule_is_ordered(store):
    s = store.schedule()
    assert [m["time"] for m in s] == ["08:00", "13:00", "20:00"]
    assert all(m["status"] == "upcoming" for m in s)
    assert "Fictional" in store.profile()["note"]


def test_no_reminder_before_scheduled_time_then_due_at_time(store, clock):
    assert store.due_reminders() == []
    clock.at("08:00")
    due = store.due_reminders()
    assert [d["name"] for d in due] == ["Examplol"] and due[0]["attempt"] == 1
    assert next(m for m in store.schedule() if m["name"] == "Examplol")["status"] == "due"


def test_reminder_is_not_repeated_until_renotify_interval_and_stops_after_max(store, clock):
    clock.at("08:00")
    assert len(store.due_reminders()) == 1
    clock.advance(1)
    assert store.due_reminders() == []  # not yet time to nag again
    shown = 1
    for _ in range(10):
        clock.advance(RENOTIFY_MINUTES + 1)
        shown += len(store.due_reminders())
    assert shown == MAX_NOTIFICATIONS
    # it is still pending, and was NOT silently marked missed
    assert next(m for m in store.schedule() if m["name"] == "Examplol")["status"] == "due"
    assert "missed" not in " ".join(a["kind"] for a in store.activity())


def test_mark_taken_updates_schedule_stops_reminders_and_is_idempotent(store, clock):
    clock.at("08:05")
    store.due_reminders()
    r = store.mark_taken("examplol")
    assert r["ok"] and not r["already"]
    assert next(m for m in store.schedule() if m["name"] == "Examplol")["status"] == "taken"
    clock.advance(30)
    assert store.due_reminders() == []
    assert store.mark_taken("Examplol")["already"] is True
    assert any(a["kind"] == "taken" for a in store.activity())


def test_snooze_delays_reminder_then_it_fires_again(store, clock):
    clock.at("08:00")
    store.due_reminders()
    r = store.snooze("Examplol", 10)
    assert r["until"] == "08:10"
    assert next(m for m in store.schedule() if m["name"] == "Examplol")["status"] == "snoozed"
    clock.advance(5)
    assert store.due_reminders() == []
    clock.advance(6)
    assert [d["name"] for d in store.due_reminders()] == ["Examplol"]


@pytest.mark.parametrize("minutes", [0, -5, 181, 10.5, "ten"])
def test_snooze_rejects_bad_durations(store, minutes):
    with pytest.raises(CareError):
        store.snooze("Examplol", minutes)


def test_cannot_snooze_a_dose_that_is_already_taken(store, clock):
    clock.at("08:10")
    store.mark_taken("Examplol")
    with pytest.raises(CareError):
        store.snooze("Examplol", 10)


def test_log_missed_records_only_and_stops_reminders(store, clock):
    clock.at("13:30")
    store.due_reminders()
    r = store.log_missed("Demostatin")
    assert "missed dose of Demostatin" in r["message"]
    assert "take" not in r["message"].lower().replace("taken", "")  # no advice about what to do next
    assert next(m for m in store.schedule() if m["name"] == "Demostatin")["status"] == "missed"
    assert store.due_reminders() == []


def test_unknown_and_ambiguous_medicine_names_are_refused(store):
    with pytest.raises(CareError):
        store.mark_taken("Aspirinium")
    store.add_medication("Examplol XR", "09:00")
    with pytest.raises(CareError):
        store.mark_taken("exam")  # ambiguous: Examplol / Examplol XR


def test_add_medication_and_set_reminder_time_validate_input(store, clock):
    with pytest.raises(CareError):
        store.add_medication("x", "08:00")
    with pytest.raises(CareError):
        store.add_medication("Valid Name", "25:99")
    with pytest.raises(CareError):
        store.add_medication("Bad;Name--", "08:00")
    store.add_medication("Vitaminol", "14:05")
    clock.at("14:05")
    assert "Vitaminol" in [d["name"] for d in store.due_reminders()]
    r = store.set_reminder_time("Vitaminol", "15:30")
    assert r["time"] == "15:30" and "Only the reminder time changed" in r["message"]
    clock.at("14:20")
    assert "Vitaminol" not in [d["name"] for d in store.due_reminders()]
    clock.at("15:30")
    assert "Vitaminol" in [d["name"] for d in store.due_reminders()]


def test_next_dose_today_and_tomorrow(store, clock):
    clock.at("09:00")
    assert store.next_dose()["name"] == "Examplol"  # 08:00 dose is still due, so it is surfaced first
    store.mark_taken("Examplol")
    assert store.next_dose()["name"] == "Demostatin"
    clock.at("23:00")
    for m in store.schedule():
        store.mark_taken(m["name"])
    nxt = store.next_dose()
    assert nxt["status"] == "tomorrow" and nxt["name"] == "Examplol"


def test_schedule_resets_each_calendar_day(store, clock):
    clock.at("08:00")
    store.mark_taken("Examplol")
    clock.at("08:01", day="2026-11-12")
    assert next(m for m in store.schedule() if m["name"] == "Examplol")["status"] == "due"


def test_caregiver_contact_requires_authorization_and_is_only_a_proposal(store):
    req = store.create_contact_request("feeling dizzy and asked for company")
    assert req["status"] == "pending" and req["relationship"] == "son"
    assert store.get_contact_request(req["id"])["status"] == "pending"  # nothing contacted yet
    store.set_caregiver_authorization(False)
    with pytest.raises(CareError):
        store.create_contact_request("anything")


def test_contact_request_status_history_is_recorded(store):
    req = store.create_contact_request("asked for help")
    store.update_contact_request(req["id"], "confirmed")
    store.update_contact_request(req["id"], "completed", provider="simulation", provider_ref="SIM-1", result="simulated")
    got = store.get_contact_request(req["id"])
    assert got["status"] == "completed" and got["provider"] == "simulation"
    kinds = [a["kind"] for a in store.activity()]
    assert "caregiver_confirmed" in kinds and "caregiver_completed" in kinds


def test_persistence_across_restarts(tmp_path, clock):
    path = tmp_path / "care.db"
    a = CareStore(path, clock=clock)
    clock.at("08:00")
    a.mark_taken("Examplol")
    b = CareStore(path, clock=clock)
    assert next(m for m in b.schedule() if m["name"] == "Examplol")["status"] == "taken"
    assert len(b.schedule()) == 3  # seed not duplicated
