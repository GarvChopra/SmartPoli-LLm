import pytest

from r2.router import needs_tools, select_tools


@pytest.mark.parametrize(
    "text",
    [
        "hi",
        "Hello there!",
        "how are you",
        "what do you think about python vs javascript",
        "what do you think about starting a business",
        "should I learn rust",
        "tell me a joke",
        "explain recursion simply",
        "who are you",
        "thanks",
        "what's 12*7",
        "give me your opinion on dark mode",
    ],
)
def test_plain_conversation_does_not_need_tools(text):
    assert needs_tools(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "open notepad",
        "please open chrome",
        "can you list my downloads",
        "hey r2 find my pdf files",
        "search the web for cats",
        "fill the form and submit",
        "type hello",
        "take a screenshot",
        "run ipconfig",
        "delete old.txt",
        "remember that my name is Garv",
        "what's on my screen",
        "what files are in downloads",
        "show me the files in C:/proj",
        "go to youtube.com",
        "could you write a note to notes.txt",
    ],
)
def test_action_requests_need_tools(text):
    assert needs_tools(text) is True


def schema(name):
    return {"type": "function", "function": {"name": name, "description": "d", "parameters": {"type": "object"}}}


ALL = [
    "list_dir", "read_file", "remember", "find_files", "write_file", "delete_file", "run_shell", "open_app",
    "screenshot", "list_windows", "ui_elements", "type_text", "scroll", "click", "press_key",
    "web_search", "web_fetch", "browser_open", "browser_read", "browser_fill", "browser_click",
]


def chosen(text):
    return {s["function"]["name"] for s in select_tools(text, [schema(n) for n in ALL])}


def test_file_requests_get_file_tools_only():
    names = chosen("find my pdf files in downloads")
    assert {"find_files", "list_dir", "read_file"} <= names
    assert "browser_click" not in names and "press_key" not in names
    assert len(names) < len(ALL)


def test_web_and_browser_requests():
    assert {"web_search", "web_fetch"} <= chosen("search the web for cats")
    assert {"browser_open", "browser_fill", "browser_click", "browser_read"} <= chosen("fill the form on this website")


def test_typing_and_screen_requests():
    assert {"type_text", "press_key", "click", "scroll"} <= chosen("type hello and press tab")
    assert {"screenshot", "list_windows", "ui_elements"} <= chosen("what is on my screen")


def test_remember_is_always_available():
    assert "remember" in chosen("open notepad")
    assert "remember" in chosen("search for cats")


def test_unclear_requests_get_a_sensible_core_set_not_everything():
    names = chosen("do the thing")
    assert {"list_dir", "open_app", "web_search", "run_shell"} <= names
    assert len(names) < len(ALL)


def test_schema_order_is_preserved():
    result = [s["function"]["name"] for s in select_tools("open notepad", [schema(n) for n in ALL])]
    assert result == [n for n in ALL if n in result]


def test_read_only_file_requests_do_not_carry_the_write_and_delete_tools():
    names = chosen("list the files in my downloads folder")
    assert {"list_dir", "read_file", "find_files"} <= names
    assert "write_file" not in names and "delete_file" not in names


@pytest.mark.parametrize(
    "text,tool",
    [
        ("delete old.txt", "delete_file"),
        ("remove the temp file", "delete_file"),
        ("save a note to notes.txt", "write_file"),
        ("write my todo list to a file", "write_file"),
        ("create a file called a.txt", "write_file"),
    ],
)
def test_write_and_delete_tools_appear_only_when_asked_for(text, tool):
    assert tool in chosen(text)
