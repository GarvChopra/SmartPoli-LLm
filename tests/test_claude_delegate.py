"""Using Claude Code from R2: the explicit phrase is the permission, Stop reaches the subprocess, and the
tool only exists when Claude Code is installed and enabled."""
import threading

import pytest

from r2 import runctx
from r2.agent import Agent
from r2.brain import FakeBrain
from r2.config import Settings
from r2.doctor import run_checks
from r2.gate import Gate, Verdict
from r2.memory import Memory
from r2.router import fast_command, select_tools
from r2.tools import ToolRegistry, ToolSpec, full_registry
from r2.types import Decision, Risk, RiskInfo

DELEGATE = RiskInfo(Risk.CONFIRM, "delegate")


# ---------- permission: only your own words ----------
def test_delegating_asks_by_default():
    assert Gate("check my downloads").evaluate(DELEGATE) is Verdict.ASK


@pytest.mark.parametrize(
    "text",
    [
        "ask claude to summarise my downloads",
        "Ask Claude: open gmail",
        "claude, check my inbox",
        "use claude to find the tax pdf",
        "tell claude to look at example.com",
        "hey r2, ask claude to read my notes",
    ],
)
def test_saying_ask_claude_is_the_permission_to_delegate(text):
    assert Gate(text).evaluate(DELEGATE) is Verdict.ALLOW


@pytest.mark.parametrize(
    "text",
    ["don't ask claude to open it", "never use claude for this", "what is claude", "claude is great", "i asked claude yesterday"],
)
def test_mentioning_or_refusing_claude_is_not_permission(text):
    assert Gate(text).evaluate(DELEGATE) is Verdict.ASK


def test_a_web_page_cannot_grant_it_because_grants_only_come_from_the_users_message():
    assert Gate("summarise this page").evaluate(DELEGATE) is Verdict.ASK  # page text never reaches Gate


# ---------- instant routing ----------
def test_ask_claude_commands_are_routed_straight_to_the_tool():
    assert fast_command("ask claude to summarize my downloads folder") == (
        "ask_claude", {"task": "summarize my downloads folder", "browser": False})
    assert fast_command("use claude to find my tax pdf") == ("ask_claude", {"task": "find my tax pdf", "browser": False})
    assert fast_command("claude, summarise the notes in documents") == (
        "ask_claude", {"task": "summarise the notes in documents", "browser": False})


@pytest.mark.parametrize(
    "text",
    [
        "ask claude to open gmail and read my unread mail",
        "Ask Claude to go to example.com and tell me the page title",
        "claude: check the website for the price",
        "ask claude to log in to the portal and download the report",
    ],
)
def test_browser_tasks_turn_the_browser_on(text):
    assert fast_command(text)[1]["browser"] is True


@pytest.mark.parametrize("text", ["ask claude", "claude,", "what does claude code do", "is claude better than gpt"])
def test_other_sentences_about_claude_are_left_to_normal_handling(text):
    assert fast_command(text) is None


def test_the_tool_is_offered_to_the_model_when_you_mention_claude():
    def schema(name):
        return {"type": "function", "function": {"name": name, "description": "d", "parameters": {"type": "object"}}}

    names = {s["function"]["name"] for s in select_tools("could claude help with this", [schema(n) for n in ("ask_claude", "list_dir", "browser_click")])}
    assert "ask_claude" in names


# ---------- the agent ----------
@pytest.fixture
def mem(tmp_path):
    return Memory(tmp_path / "t.db")


def claude_registry(func):
    reg = ToolRegistry()
    reg.register(ToolSpec("ask_claude", "Ask Claude Code", {"type": "object", "properties": {}}, func, DELEGATE))
    return reg


class Approver:
    def __init__(self):
        self.calls = []

    def __call__(self, call):
        self.calls.append(call.name)
        return False


def test_ask_claude_runs_without_a_prompt_and_its_answer_is_shown_as_is(mem):
    approver = Approver()
    agent = Agent(FakeBrain([Decision(final="unused")]), claude_registry(lambda task, browser=False: f"ANSWER[{task}|{browser}]"),
                  mem, approver, smart_routing=True)
    result = agent.run("ask claude to go to example.com and report the title")
    assert result.status == "done" and result.answer == "ANSWER[go to example.com and report the title|True]"
    assert approver.calls == [] and mem.list_actions()[0]["approval"] == "granted"


def test_a_failure_is_reported_directly_and_not_retried_by_the_small_model(mem):
    def boom(task, browser=False):
        raise RuntimeError("Claude Code failed (exit 1): Not logged in. Open a terminal, run claude, then /login.")

    brain = FakeBrain([Decision(final="should not be used")])
    result = Agent(brain, claude_registry(boom), mem, Approver(), smart_routing=True).run("ask claude to read my notes")
    assert result.status == "failed" and "/login" in result.answer
    assert brain.calls == []


def test_without_the_phrase_it_still_needs_approval(mem):
    approver = Approver()
    reg = claude_registry(lambda task, browser=False: "x")
    agent = Agent(FakeBrain([Decision(tool_call=__import__("r2.types", fromlist=["ToolCall"]).ToolCall("ask_claude", {"task": "t"})),
                             Decision(final="declined")]), reg, mem, approver)
    agent.run("please check my notes")
    assert approver.calls == ["ask_claude"]


def test_the_stop_button_reaches_tools_through_the_run_context(mem):
    seen = {}

    def tool(task, browser=False):
        seen["stop"] = runctx.current_stop.get()
        return "ok"

    stop = threading.Event()
    Agent(FakeBrain([]), claude_registry(tool), mem, Approver(), smart_routing=True).run("ask claude to do it", stop=stop)
    assert seen["stop"] is stop
    assert runctx.current_stop.get() is None  # restored afterwards


# ---------- the registry and settings ----------
def test_the_tool_exists_only_when_claude_code_is_installed(tmp_path):
    mem = Memory(tmp_path / "t.db")
    without = full_registry(mem, data_dir=tmp_path, claude_finder=lambda: None)
    assert without.get("ask_claude") is None
    with_ = full_registry(mem, data_dir=tmp_path, claude_finder=lambda: "C:/npm/claude.cmd")
    spec = with_.get("ask_claude")
    assert spec is not None and spec.risk == DELEGATE
    assert set(spec.parameters["properties"]) == {"task", "browser"} and spec.parameters["required"] == ["task"]
    with_.close()
    without.close()


def test_settings_for_claude_code(monkeypatch, tmp_path):
    s = Settings()
    assert s.claude is True
    monkeypatch.setenv("R2_CLAUDE", "0")
    assert Settings.from_env().claude is False
    monkeypatch.setenv("R2_CLAUDE", "1")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("R2_CLAUDE_DIRS", f"{a};{b}")
    assert Settings.from_env().claude_dirs == (str(a), str(b))
    monkeypatch.setenv("R2_CLAUDE_DIRS", "")
    assert Settings.from_env().claude_dirs == ()


def test_default_shared_folders_exist_on_this_pc_and_are_the_usual_three():
    for folder in Settings().claude_dirs:
        assert folder.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] in {"Downloads", "Documents", "Desktop"}


# ---------- doctor ----------
def doctor(settings, version):
    return {c.name: c for c in run_checks(settings, http_get=lambda *a, **k: (_ for _ in ()).throw(OSError()), env={},
                                          has_module=lambda m: True, has_browser=lambda: True, claude_version=lambda: version)}


def test_doctor_reports_claude_code():
    ok = doctor(Settings(brain="cloud", anthropic_api_key="k"), "2.1.296")
    assert ok["Claude Code"].status == "ok" and "2.1.296" in ok["Claude Code"].detail
    missing = doctor(Settings(brain="cloud", anthropic_api_key="k"), None)
    assert missing["Claude Code"].status == "warn" and "ask claude" in missing["Claude Code"].detail.lower()
    off = doctor(Settings(brain="cloud", anthropic_api_key="k", claude=False), "2.1.296")
    assert "Claude Code" not in off
