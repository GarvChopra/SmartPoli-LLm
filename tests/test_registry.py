import pytest

from r2.memory import Memory
from r2.tools import full_registry
from r2.types import Risk

EXPECTED = {
    "list_dir", "read_file", "remember", "find_files", "write_file", "delete_file",
    "run_shell", "open_app", "screenshot", "list_windows", "ui_elements",
    "type_text", "scroll", "click", "press_key", "web_search", "web_fetch",
    "browser_open", "browser_read", "browser_fill", "browser_click",
    "use_skill", "propose_skill", "create_project", "summarize_meeting", "list_tasks", "complete_task",
}


@pytest.fixture
def reg(tmp_path):
    return full_registry(Memory(tmp_path / "t.db"), data_dir=tmp_path, claude_finder=lambda: None)


def risk(reg, tool, **args):
    return reg.get(tool).risk_for(args)


def test_every_tool_is_registered_with_a_valid_schema(reg):
    names = {s["function"]["name"] for s in reg.schemas()}
    assert names == EXPECTED
    for s in reg.schemas():
        assert s["function"]["description"]
        assert s["function"]["parameters"]["type"] == "object"


def test_static_risks(reg):
    for name in ("list_dir", "read_file", "remember", "find_files", "screenshot", "list_windows",
                 "ui_elements", "type_text", "scroll", "web_search", "web_fetch",
                 "browser_open", "browser_read", "browser_fill"):
        assert risk(reg, name, text="x", path="p", url="http://x").risk is Risk.SAFE, name
    assert risk(reg, "write_file", path="p", content="c").action_type == "write"
    assert risk(reg, "delete_file", path="p").action_type == "delete"
    assert risk(reg, "click", x=1, y=1).action_type == "click"


def test_argument_dependent_risks(reg):
    assert risk(reg, "run_shell", command="dir").action_type == "shell"
    assert risk(reg, "run_shell", command="format C:").risk is Risk.BLOCKED
    assert risk(reg, "open_app", name="notepad").risk is Risk.SAFE
    assert risk(reg, "open_app", name="powershell").risk is Risk.CONFIRM
    assert risk(reg, "press_key", keys="tab").risk is Risk.SAFE
    assert risk(reg, "press_key", keys="enter").action_type == "submit"
    assert risk(reg, "browser_click", text="Next").risk is Risk.SAFE
    assert risk(reg, "browser_click", text="Post").action_type == "post"


def test_browser_is_not_started_until_used(reg):
    # building the registry must not launch Chrome
    assert reg.browser._ctx is None
    reg.close()


def test_exfiltration_guards_are_wired_into_the_registry(reg):
    assert risk(reg, "read_file", path="C:/proj/.env").action_type == "sensitive_read"
    assert risk(reg, "read_file", path="C:/proj/readme.md").risk is Risk.SAFE
    assert risk(reg, "web_fetch", url="https://a.b/?" + "x" * 400).risk is Risk.CONFIRM
    assert risk(reg, "browser_open", url="https://a.b/?" + "x" * 400).risk is Risk.CONFIRM
    assert risk(reg, "web_search", query="y" * 300).risk is Risk.CONFIRM
