"""Cheap, deterministic routing that keeps R2 fast.

* needs_tools(): is this message a request to DO something on the PC or the web, or just
  conversation? Conversation goes to a small, fast model with no tools at all.
* select_tools(): for real requests, only the tool descriptions that could matter are sent
  to the model. Fewer tool descriptions means a much shorter prompt, which on a CPU is the
  difference between seconds and a minute.

Both are plain keyword checks, not model calls, so they cost nothing and are easy to reason
about. A wrong guess is cheap: the chat model can answer NEED_TOOLS to hand the request over."""
from __future__ import annotations

import re

_FILLER = {
    "hey", "hi", "r2", "please", "pls", "can", "could", "would", "will", "you", "ok", "okay",
    "now", "just", "and", "then", "also", "kindly",
}
_ACTION_VERBS = {
    "open", "launch", "start", "run", "list", "find", "search", "read", "write", "save", "delete",
    "remove", "type", "click", "press", "fill", "submit", "post", "send", "download", "install",
    "browse", "visit", "close", "play", "create", "rename", "move", "copy", "scroll", "remember",
    "show", "take", "go", "check", "look",
}
_TOOL_NOUNS = {
    "file", "files", "folder", "folders", "directory", "desktop", "downloads", "documents",
    "screenshot", "screen", "browser", "website", "url", "http", "https", "www", "app", "apps",
    "window", "windows", "tab", "tabs", "notepad", "chrome", "terminal", "powershell", "cmd",
    "shell", "clipboard", "mouse", "keyboard",
}
_FILE_OR_DOMAIN = re.compile(
    r"\b[\w-]+\.(com|org|net|io|txt|pdf|docx?|xlsx?|png|jpe?g|py|js|exe|zip)\b", re.I
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def needs_tools(text: str) -> bool:
    if _FILE_OR_DOMAIN.search(text):
        return True
    words = _words(text)
    if any(w in _TOOL_NOUNS for w in words):
        return True
    while words and words[0] in _FILLER and len(words) > 1:
        words.pop(0)
    return bool(words) and words[0] in _ACTION_VERBS


# (keywords, tools) - a group is included when any keyword appears in the message
_GROUPS = [
    ({"file", "files", "folder", "folders", "directory", "downloads", "documents", "desktop", "pdf",
      "read", "find", "list", "txt", "note", "notes"},
     ["list_dir", "read_file", "find_files"]),
    # destructive file tools are only offered when the request is about writing or removing
    ({"write", "save", "delete", "remove", "create", "rename", "move", "copy", "note", "notes"},
     ["write_file", "delete_file"]),
    ({"run", "command", "terminal", "shell", "cmd", "powershell", "install", "ipconfig", "ping", "git",
      "pip", "npm", "python"},
     ["run_shell"]),
    ({"open", "launch", "start", "app", "application", "notepad", "chrome", "calculator", "calc",
      "explorer", "spotify", "word", "excel"},
     ["open_app", "list_windows"]),
    ({"screen", "screenshot", "window", "windows", "see", "looking", "display"},
     ["screenshot", "list_windows", "ui_elements"]),
    ({"type", "click", "press", "scroll", "key", "keyboard", "mouse", "enter", "tab"},
     ["type_text", "press_key", "click", "scroll"]),
    ({"search", "google", "web", "internet", "news", "weather", "lookup", "online", "latest", "price"},
     ["web_search", "web_fetch"]),
    ({"website", "browser", "page", "form", "fill", "submit", "login", "url", "http", "https", "www",
      "site", "visit", "go", "browse", "youtube", "post", "send", "tweet"},
     ["browser_open", "browser_read", "browser_fill", "browser_click"]),
    ({"meeting", "meetings", "transcript", "minutes", "mom", "standup", "sync", "summarize", "summary", "summarise",
      "task", "tasks", "project", "projects", "assigned", "assign", "action"},
     ["summarize_meeting", "list_tasks", "complete_task", "create_project", "read_file"]),
    ({"claude"}, ["ask_claude"]),
]
_CORE = ["list_dir", "read_file", "find_files", "open_app", "web_search", "run_shell"]
_ALWAYS = ["remember", "use_skill", "propose_skill"]


def select_tools(text: str, schemas: list[dict]) -> list[dict]:
    words = set(_words(text))
    wanted = set(_ALWAYS)
    for keywords, tools in _GROUPS:
        if words & keywords:
            wanted.update(tools)
    if _FILE_OR_DOMAIN.search(text):
        wanted.update(["browser_open", "browser_read", "web_fetch"])
    if wanted == set(_ALWAYS):
        wanted.update(_CORE)
    return [s for s in schemas if s["function"]["name"] in wanted]


# ---------------------------------------------------------------------------
# Instant commands: "open whatsapp" needs no model at all. Opening an app is a plain lookup,
# so doing it directly takes a fraction of a second instead of ~20 s of model time.
# Anything even slightly more complicated is left to the model.
# ---------------------------------------------------------------------------
_OPEN_COMMAND = re.compile(
    r"^(?:(?:(?:hey|hi|ok|okay)\s+r2[,\s]*|r2[,\s]+|please\s+|pls\s+|can\s+you\s+|could\s+you\s+"
    r"|would\s+you\s+|will\s+you\s+))*"
    r"(?:open|launch|start)\s+(?:the\s+|my\s+)?([A-Za-z0-9][A-Za-z0-9 ._+-]{0,40})$",
    re.I,
)
_NOT_A_PLAIN_NAME = {"and", "then", "with", "in", "on", "to", "for", "at", "from", "a", "an", "up", "of", "is"}


_DELEGATE = re.compile(
    r"^(?:(?:hey\s+r2[,\s]+|hi\s+r2[,\s]+|r2[,\s]+|please\s+|pls\s+))*"
    r"(?:(?:ask|tell|let|use)\s+claude(?:\s+code)?\s*(?:to\s+|that\s+|[:,]\s*)?|claude\s*[:,]\s*)"
    r"(.+)$",
    re.I | re.S,
)
_BROWSERY = re.compile(
    r"\b(?:browser|chrome|tabs?|web\s?sites?|web\s?pages?|gmail|youtube|google|linkedin|twitter|instagram|facebook"
    r"|sites?|online|log\s?in|sign\s?in|forms?|https?)\b|\.(?:com|org|net|in|io|co)\b|www\.",
    re.I,
)


def fast_command(message: str) -> tuple[str, dict] | None:
    """A command that needs no model: "ask claude to ..." (hand the task to Claude Code) or a bare
    "open <app>" request. Otherwise None."""
    delegated = _DELEGATE.match(message.strip())
    if delegated:
        task = delegated.group(1).strip()
        return ("ask_claude", {"task": task, "browser": bool(_BROWSERY.search(task))}) if task else None
    text = re.sub(r"[,\s]+(?:please|pls|now)$", "", message.strip().rstrip(".!?"), flags=re.I)
    match = _OPEN_COMMAND.match(text)
    if not match:
        return None
    name = match.group(1).strip()
    words = name.lower().split()
    if not words or len(words) > 3 or "." in name or any(w in _NOT_A_PLAIN_NAME for w in words):
        return None
    return ("open_app", {"name": name})
