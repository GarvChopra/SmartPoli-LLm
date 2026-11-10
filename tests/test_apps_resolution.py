"""open_app must find the app first and only claim success when something really launched.

Regression: it used to run `cmd /c start <name>` blindly, so for a Microsoft Store app such as
WhatsApp Windows showed "cannot find 'whatsapp'" while R2 answered "WhatsApp has been opened"."""
import pytest

from r2.agent import Agent
from r2.brain import FakeBrain
from r2.memory import Memory
from r2.router import fast_command
from r2.tools import ToolRegistry, ToolSpec, apps
from r2.types import Decision, Risk, RiskInfo

START_APPS = [
    ("WhatsApp", "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"),
    ("Notepad", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
    ("Google Chrome", "Chrome"),
    ("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    ("Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"),
    ("Visual Studio Code", "Microsoft.VisualStudioCode"),
]


def opener(apps_list=START_APPS, which=lambda name: None):
    launched = []

    def run(name):
        return apps.open_app(
            name,
            installed=lambda: apps_list,
            which=which,
            launcher=lambda cmd, **kw: launched.append(cmd),
        ), launched

    return run


# ---------- finding the app ----------
@pytest.mark.parametrize(
    "typed,expected",
    [
        ("whatsapp", "WhatsApp"),
        ("WHATSAPP", "WhatsApp"),
        ("whatsap", "WhatsApp"),  # a typo
        ("chrome", "Google Chrome"),
        ("calc", "Calculator"),
        ("code", "Visual Studio Code"),
        ("visual studio code", "Visual Studio Code"),
        ("spotify", "Spotify"),
    ],
)
def test_find_app_matches_names_loosely(typed, expected):
    assert apps.find_app(typed, START_APPS)[0] == expected


def test_find_app_prefers_the_closest_name():
    both = [("Notepad++", "np++"), ("Notepad", "np")]
    assert apps.find_app("notepad", both)[0] == "Notepad"


@pytest.mark.parametrize("typed", ["zzzzqx", "", "a"])
def test_find_app_returns_none_when_nothing_fits(typed):
    assert apps.find_app(typed, START_APPS) is None


# ---------- launching ----------
def test_store_and_start_menu_apps_launch_through_the_apps_folder():
    out, launched = opener()("whatsapp")
    assert out == "Opened WhatsApp"
    assert launched == [["explorer.exe", "shell:AppsFolder\\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"]]


def test_plain_executables_on_the_path_still_open():
    out, launched = opener([], which=lambda n: r"C:\Windows\System32\ipconfig.exe" if n == "ipconfig" else None)("ipconfig")
    assert out == "Opened ipconfig" and launched == [[r"C:\Windows\System32\ipconfig.exe"]]


def test_an_app_that_is_not_installed_raises_and_launches_nothing():
    run = opener()
    with pytest.raises(FileNotFoundError) as err:
        run("definitelynotanapp")
    assert "No app called 'definitelynotanapp'" in str(err.value)


def test_not_found_message_suggests_similar_apps():
    with pytest.raises(FileNotFoundError, match="Calculator"):
        opener()("calculater app")  # close to Calculator but not matched as a name


def test_shell_characters_are_refused_before_any_lookup():
    with pytest.raises(ValueError):
        opener()("calc & del x")


def test_the_lookup_is_cached_so_opening_is_instant(monkeypatch):
    calls = []
    monkeypatch.setattr(apps, "_query_start_apps", lambda: calls.append(1) or START_APPS)
    monkeypatch.setattr(apps, "_cache", {"apps": None, "at": 0.0})
    assert apps.installed_apps() == START_APPS
    assert apps.installed_apps() == START_APPS
    assert len(calls) == 1


def test_a_failing_lookup_returns_an_empty_list_instead_of_crashing(monkeypatch):
    def boom():
        raise OSError("powershell missing")

    monkeypatch.setattr(apps, "_query_start_apps", boom)
    monkeypatch.setattr(apps, "_cache", {"apps": None, "at": 0.0})
    assert apps.installed_apps() == []


def test_parse_start_apps_output():
    text = "WhatsApp|5319275A.WhatsApp!App\nGoogle Chrome|Chrome\n\nbroken line\nNo Id|\n"
    assert apps.parse_start_apps(text) == [("WhatsApp", "5319275A.WhatsApp!App"), ("Google Chrome", "Chrome")]


# ---------- instant "open X" without asking the model ----------
@pytest.mark.parametrize(
    "text,name",
    [
        ("open whatsapp", "whatsapp"),
        ("Open WhatsApp.", "WhatsApp"),
        ("please open the calculator", "calculator"),
        ("can you launch google chrome", "google chrome"),
        ("hey r2, open spotify", "spotify"),
        ("start notepad", "notepad"),
    ],
)
def test_simple_open_commands_skip_the_model(text, name):
    assert fast_command(text) == ("open_app", {"name": name})


@pytest.mark.parametrize(
    "text",
    [
        "open notes.txt",
        "open c:/users/x",
        "open chrome and search for cats",
        "open youtube then play a song",
        "start a timer for ten minutes",
        "run ipconfig",
        "open",
        "what does open mean",
        "open the file in my downloads folder please",
    ],
)
def test_anything_more_complicated_goes_to_the_model(text):
    assert fast_command(text) is None


# ---------- agent integration ----------
@pytest.fixture
def mem(tmp_path):
    return Memory(tmp_path / "t.db")


def registry_with(open_app):
    reg = ToolRegistry()
    reg.register(ToolSpec("open_app", "Open", {"type": "object", "properties": {}}, open_app, RiskInfo(Risk.SAFE)))
    return reg


def test_open_command_is_answered_instantly_without_the_model(mem):
    brain = FakeBrain([Decision(final="should not be used")])
    tokens = []
    agent = Agent(brain, registry_with(lambda name: f"Opened {name.title()}"), mem, lambda c: False, smart_routing=True)
    result = agent.run("open whatsapp", on_token=tokens.append)
    assert (result.status, result.answer, result.steps) == ("done", "Opened Whatsapp.", 1)
    assert brain.calls == [] and tokens == ["Opened Whatsapp."]
    assert mem.list_actions()[0]["tool"] == "open_app" and mem.list_actions()[0]["approval"] == "auto"
    assert mem.recent_messages(2)[-1] == {"role": "assistant", "content": "Opened Whatsapp."}


def test_when_the_app_is_not_found_the_model_gets_a_chance(mem):
    def missing(name):
        raise FileNotFoundError(f"No app called '{name}' is installed")

    brain = FakeBrain([Decision(final="I opened YouTube in the browser instead.")])
    agent = Agent(brain, registry_with(missing), mem, lambda c: False, smart_routing=True)
    result = agent.run("open youtube")
    assert result.answer == "I opened YouTube in the browser instead."
    assert len(brain.calls) == 1


def test_a_denied_open_is_final_and_does_not_retry_through_the_model(mem):
    reg = ToolRegistry()
    reg.register(
        ToolSpec("open_app", "Open", {"type": "object", "properties": {}}, lambda name: "x", RiskInfo(Risk.CONFIRM, "app"))
    )
    brain = FakeBrain([Decision(final="retry")])
    result = Agent(brain, reg, mem, lambda c: False, smart_routing=True).run("open powershell")
    assert brain.calls == [] and result.answer.startswith("DENIED")


def test_fast_path_is_only_used_with_smart_routing(mem):
    brain = FakeBrain([Decision(final="model answer")])
    agent = Agent(brain, registry_with(lambda name: "Opened"), mem, lambda c: False)  # routing off
    assert agent.run("open notepad").answer == "model answer"


@pytest.mark.parametrize("typed", ["vs code", "vscode", "VS Code"])
def test_common_nicknames_resolve(typed):
    assert apps.find_app(typed, START_APPS)[0] == "Visual Studio Code"
