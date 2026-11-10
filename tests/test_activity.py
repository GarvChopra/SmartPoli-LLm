import time

import pytest

from r2.activity import ActivityLogger, current_app
from r2.agent import Agent
from r2.brain import FakeBrain
from r2.memory import Memory
from r2.tools import ToolRegistry
from r2.types import Decision


@pytest.fixture
def mem(tmp_path):
    return Memory(tmp_path / "t.db")


def at(day, hour, minute=30):
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


NOW = at(20, 9, 45)  # a Tuesday morning, 09:45


def test_usual_apps_for_this_hour_most_frequent_first(mem):
    for day in (10, 11, 12, 13, 14):
        mem.log_activity("chrome.exe", ts=at(day, 9))
        mem.log_activity("chrome.exe", ts=at(day, 9, 50))
    for day in (10, 11, 12):
        mem.log_activity("code.exe", ts=at(day, 9, 10))
    assert mem.usual_apps(9, now=NOW) == ["chrome.exe", "code.exe"]


def test_rare_apps_other_hours_and_old_history_are_ignored(mem):
    mem.log_activity("spotify.exe", ts=at(10, 9))
    mem.log_activity("spotify.exe", ts=at(11, 9))  # only twice: not a habit yet
    for day in (10, 11, 12, 13):
        mem.log_activity("steam.exe", ts=at(day, 21))  # a different hour
    for day in (1, 2, 3, 4):
        mem.log_activity("notepad.exe", ts=at(day, 9) - 90 * 86400)  # months ago
    assert mem.usual_apps(9, now=NOW) == []


def test_usual_apps_respects_the_limit(mem):
    for app in ("a.exe", "b.exe", "c.exe", "d.exe"):
        for day in (10, 11, 12):
            mem.log_activity(app, ts=at(day, 9))
    assert len(mem.usual_apps(9, limit=2, now=NOW)) == 2


def test_forgetting_wipes_the_activity_log_too(mem):
    for day in (10, 11, 12):
        mem.log_activity("chrome.exe", ts=at(day, 9))
    mem.clear_history()
    assert mem.usual_apps(9, now=NOW) == []


# ---------- the logger ----------
def test_sample_once_logs_the_foreground_app(mem):
    logger = ActivityLogger(mem, sampler=lambda: "chrome.exe")
    logger.sample_once()
    assert mem._db.execute("select app from activity").fetchall()[0][0] == "chrome.exe"


@pytest.mark.parametrize("name", [None, "", "python.exe", "pythonw.exe", "LockApp.exe", "SearchHost.exe"])
def test_sample_once_ignores_r2_itself_and_system_noise(mem, name):
    ActivityLogger(mem, sampler=lambda: name).sample_once()
    assert mem._db.execute("select count(*) from activity").fetchone()[0] == 0


def test_sampler_errors_never_escape(mem):
    def boom():
        raise OSError("no foreground window")

    ActivityLogger(mem, sampler=boom).sample_once()  # must not raise


def test_background_thread_samples_and_stops(mem):
    logger = ActivityLogger(mem, sampler=lambda: "code.exe", interval=0.02)
    logger.start()
    time.sleep(0.2)
    logger.stop()
    count = mem._db.execute("select count(*) from activity").fetchone()[0]
    assert count >= 2
    time.sleep(0.1)
    assert mem._db.execute("select count(*) from activity").fetchone()[0] == count


def test_current_app_is_a_plain_exe_name_or_none():
    name = current_app()
    assert name is None or (isinstance(name, str) and name.lower().endswith(".exe") and "\\" not in name)


# ---------- the agent uses it ----------
def test_prompts_mention_what_the_user_usually_does_at_this_time(mem):
    for day in (10, 11, 12):
        mem.log_activity("chrome.exe", ts=at(day, 9))
    brain = FakeBrain([Decision(final="ok")])
    agent = Agent(brain, ToolRegistry(), mem, lambda c: False)
    agent.clock = lambda: NOW
    agent.run("do the usual")
    assert "usually uses: chrome.exe" in brain.calls[0][0]["content"]


def test_no_habit_line_when_nothing_has_been_learned(mem):
    brain = FakeBrain([Decision(final="ok")])
    agent = Agent(brain, ToolRegistry(), mem, lambda c: False)
    agent.clock = lambda: NOW
    agent.run("do the usual")
    assert "usually uses" not in brain.calls[0][0]["content"]
