from r2 import cli
from r2.config import Settings
from r2.memory import Memory
from r2.memory_files import export_facts, import_facts
from r2.policy import DEMO_TOOLS, load_disabled, save_disabled
from r2.tools import full_registry


def registry(tmp_path):
    return full_registry(Memory(tmp_path / "m.db"), tmp_path, headless=True, claude_finder=lambda: None)


# ---------- tool allow-list ----------
def test_disabled_tools_are_removed_entirely(tmp_path):
    save_disabled(tmp_path, {"run_shell", "delete_file"})
    reg = registry(tmp_path)
    assert reg.get("run_shell") is None and reg.get("delete_file") is None
    assert reg.get("read_file") is not None
    assert "run_shell" not in {s["function"]["name"] for s in reg.schemas()}


def test_a_broken_policy_file_changes_nothing(tmp_path):
    (tmp_path / "policy.json").write_text("{not json", encoding="utf-8")
    assert load_disabled(tmp_path) == set()
    assert registry(tmp_path).get("run_shell") is not None


def test_demo_profile_keeps_only_safe_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("R2_PROFILE", "demo")
    assert set(registry(tmp_path).names()) == set(DEMO_TOOLS)


def test_tools_cli_lists_disables_and_enables(tmp_path, capsys):
    settings = Settings(data_dir=tmp_path, claude=False)
    assert cli.cmd_tools(settings, "disable", "run_shell") == 0
    assert load_disabled(tmp_path) == {"run_shell"}
    cli.cmd_tools(settings, "list", None)
    assert "[off] run_shell" in capsys.readouterr().out
    assert cli.cmd_tools(settings, "enable", "run_shell") == 0
    assert load_disabled(tmp_path) == set()
    assert cli.cmd_tools(settings, "disable", "no_such_tool") == 1


# ---------- readable memory ----------
def test_export_then_import_round_trips_and_adds_new_lines(tmp_path):
    memory = Memory(tmp_path / "m.db")
    a = memory.save_fact("Prefers tea", "agent")
    path = tmp_path / "MEMORY.md"
    assert export_facts(memory, path) == 1 and f"- [{a}] Prefers tea" in path.read_text(encoding="utf-8")
    path.write_text(path.read_text(encoding="utf-8") + "- Lives in Delhi\n", encoding="utf-8")
    report = import_facts(memory, path)
    assert (report.added, report.changed, report.removed) == (1, 0, 0)
    assert sorted(f.text for f in memory.list_facts()) == ["Lives in Delhi", "Prefers tea"]
    assert import_facts(memory, path).summary().startswith("Added 0, changed 0")  # importing twice adds nothing


def test_editing_a_line_replaces_the_fact_and_deleting_needs_prune(tmp_path):
    memory = Memory(tmp_path / "m.db")
    a = memory.save_fact("Prefers tea", "agent")
    b = memory.save_fact("Works at Acme", "agent")
    path = tmp_path / "MEMORY.md"
    path.write_text(f"# x\n- [{a}] Prefers coffee\n", encoding="utf-8")  # a edited, b deleted from the file
    report = import_facts(memory, path)
    assert report.changed == 1 and report.removed == 0
    assert {f.text for f in memory.list_facts()} == {"Prefers coffee", "Works at Acme"}  # b survives without --prune
    pruned = import_facts(memory, path, prune=True)
    assert pruned.removed == 1 and {f.text for f in memory.list_facts()} == {"Prefers coffee"}


def test_secrets_in_the_file_are_refused(tmp_path):
    memory = Memory(tmp_path / "m.db")
    path = tmp_path / "MEMORY.md"
    path.write_text("- my password is hunter2\n- Likes jazz\n", encoding="utf-8")
    report = import_facts(memory, path)
    assert report.added == 1 and len(report.refused) == 1
    assert [f.text for f in memory.list_facts()] == ["Likes jazz"]


def test_memory_cli(tmp_path, capsys):
    settings = Settings(data_dir=tmp_path)
    (tmp_path).mkdir(exist_ok=True)
    assert cli.cmd_memory(settings, "import", False) == 1  # nothing exported yet
    Memory(tmp_path / "r2.db").save_fact("Likes jazz", "agent")
    assert cli.cmd_memory(settings, "export", False) == 0
    assert (tmp_path / "memory" / "MEMORY.md").is_file()
    assert cli.cmd_memory(settings, "import", False) == 0
