"""R2 can hand a task to Claude Code (headless `claude -p`, using the user's own Claude login), including its
browser extension and read-only file access. These tests use a fake process; nothing real is launched."""
import json
import subprocess

import pytest

from r2.claude_bridge import DENY_READ, ask_claude, build_command, parse_output


# ---------- the command line ----------
def flags(cmd):
    return " ".join(cmd)


def after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_command_is_headless_json_and_never_prompts():
    cmd = build_command("claude", browser=False, dirs=[])
    assert cmd[0] == "claude" and "-p" in cmd
    assert after(cmd, "--output-format") == "json"
    assert after(cmd, "--permission-mode") == "dontAsk"
    assert after(cmd, "--permission-prompts") == "none"
    assert "--max-turns" in cmd


def test_it_must_not_use_bare_mode_because_that_ignores_the_users_login():
    assert "--bare" not in build_command("claude", browser=True, dirs=["C:/x"])


def test_only_read_only_file_tools_and_web_are_allowed_by_default():
    allowed = after(build_command("claude", browser=False, dirs=[]), "--allowedTools")
    for tool in ("Read", "Glob", "Grep", "WebSearch", "WebFetch"):
        assert tool in allowed.split(",")
    for forbidden in ("Bash", "Edit", "Write", "NotebookEdit", "mcp__claude-in-chrome"):
        assert forbidden not in allowed


def test_browser_tools_are_added_only_for_browser_tasks():
    plain = build_command("claude", browser=False, dirs=[])
    assert "--no-chrome" in plain and "--chrome" not in plain
    browser = build_command("claude", browser=True, dirs=[])
    assert "--chrome" in browser and "--no-chrome" not in browser
    assert "mcp__claude-in-chrome__*" in after(browser, "--allowedTools").split(",")


def test_secret_files_are_denied_at_claude_codes_own_level():
    deny = after(build_command("claude", browser=False, dirs=[]), "--disallowedTools").split(",")
    for pattern in ("Read(//**/.env)", "Read(//**/.env.*)", "Read(//**/*.pem)", "Read(//**/.ssh/**)",
                    "Read(//**/tokens.json)", "Read(//**/overlay.token)", "Read(//**/browser-profile/**)"):
        assert pattern in deny
    assert set(deny) == set(DENY_READ)


def test_every_deny_rule_is_anchored_at_the_filesystem_root():
    """Claude Code anchors a plain pattern such as Read(**/.env) at the folder it was started in, which here is an
    empty scratch folder, so it would never match the user's files. A real run read .env through exactly that gap.
    Absolute rules start with // (and match anywhere on the disk)."""
    for rule in DENY_READ:
        assert rule.startswith("Read(//"), rule


def test_each_folder_is_shared_with_add_dir():
    cmd = build_command("claude", browser=False, dirs=["C:/Users/x/Downloads", "C:/Users/x/Documents"])
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-dir"] == ["C:/Users/x/Downloads", "C:/Users/x/Documents"]


@pytest.mark.parametrize("bad", ["C:/x & calc", "C:/x|y", "C:/x>y", "C:/x^y", "C:/%PATH%", 'C:/x"y', "C:/x\ny"])
def test_folders_with_shell_characters_are_refused(bad):
    with pytest.raises(ValueError):
        build_command("claude", browser=False, dirs=[bad])


def test_the_system_note_says_to_treat_web_text_as_data_and_not_to_change_files():
    note = after(build_command("claude", browser=False, dirs=[]), "--append-system-prompt").lower()
    assert "data" in note and "never" in note


# ---------- reading the answer ----------
def test_parse_json_result():
    assert parse_output(json.dumps({"type": "result", "result": "Hello!", "is_error": False})) == "Hello!"


def test_parse_falls_back_to_plain_text_and_handles_empty_results():
    assert parse_output("just text") == "just text"
    assert "nothing" in parse_output(json.dumps({"result": ""})).lower()


def test_an_error_result_raises_with_the_message():
    with pytest.raises(RuntimeError, match="Credit balance"):
        parse_output(json.dumps({"is_error": True, "result": "Credit balance is too low"}))


def test_very_long_answers_are_trimmed():
    out = parse_output(json.dumps({"result": "x" * 20000}))
    assert len(out) < 8000 and "trimmed" in out


# ---------- running it ----------
class FakeProc:
    def __init__(self, stdout="", stderr="", code=0, hang=False):
        self.stdout, self.stderr, self.returncode, self.hang = stdout, stderr, code, hang
        self.stdin_text = None
        self.pid = 4242

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.stdin_text = input
        if self.hang:
            raise subprocess.TimeoutExpired("claude", timeout or 0)
        return self.stdout, self.stderr


def runner(proc, seen=None):
    def popen(cmd, **kw):
        if seen is not None:
            seen.update(cmd=cmd, kw=kw)
        return proc

    return popen


FOUND = lambda: "C:/npm/claude.cmd"  # noqa: E731


def test_the_task_goes_in_through_stdin_not_the_command_line(tmp_path):
    proc = FakeProc(stdout=json.dumps({"result": "done"}))
    seen = {}
    task = 'summarise "my notes" & then | delete'  # nasty characters must never reach a shell
    out = ask_claude(task, find=FOUND, popen=runner(proc, seen), workdir=tmp_path / "work")
    assert out == "done" and proc.stdin_text == task
    assert task not in " ".join(seen["cmd"])
    assert seen["kw"]["cwd"] == str(tmp_path / "work") and (tmp_path / "work").is_dir()
    assert seen["kw"].get("shell") in (None, False)


def test_it_runs_in_an_empty_scratch_folder_so_no_project_hooks_or_settings_load(tmp_path):
    work = tmp_path / "work"
    ask_claude("hi", find=FOUND, popen=runner(FakeProc(stdout="{}")), workdir=work)
    assert list(work.iterdir()) == []


def test_missing_claude_code_gives_a_clear_message(tmp_path):
    with pytest.raises(FileNotFoundError, match="not installed"):
        ask_claude("hi", find=lambda: None, popen=runner(FakeProc()), workdir=tmp_path)


def test_a_failed_run_reports_the_error_and_hints_at_login(tmp_path):
    proc = FakeProc(stdout="", stderr="Error: Not logged in. Please run /login", code=1)
    with pytest.raises(RuntimeError) as err:
        ask_claude("hi", find=FOUND, popen=runner(proc), workdir=tmp_path)
    assert "claude" in str(err.value).lower() and "/login" in str(err.value)


def test_a_failed_run_never_echoes_huge_output(tmp_path):
    proc = FakeProc(stdout="", stderr="boom " * 5000, code=1)
    with pytest.raises(RuntimeError) as err:
        ask_claude("hi", find=FOUND, popen=runner(proc), workdir=tmp_path)
    assert len(str(err.value)) < 1500


def test_a_run_that_takes_too_long_is_killed(tmp_path):
    killed = []
    with pytest.raises(TimeoutError):
        ask_claude("hi", find=FOUND, popen=runner(FakeProc(hang=True)), workdir=tmp_path,
                   timeout=0.3, poll=0.05, kill=killed.append)
    assert killed and killed[0].pid == 4242


def test_pressing_stop_kills_it_immediately(tmp_path):
    killed = []
    flags_ = iter([False, False, True])
    with pytest.raises(RuntimeError, match="stopped"):
        ask_claude("hi", find=FOUND, popen=runner(FakeProc(hang=True)), workdir=tmp_path,
                   timeout=30, poll=0.01, kill=killed.append, should_stop=lambda: next(flags_, True))
    assert killed


def test_browser_flag_reaches_the_command_line(tmp_path):
    seen = {}
    ask_claude("open a page", browser=True, find=FOUND, popen=runner(FakeProc(stdout="{}"), seen), workdir=tmp_path)
    assert "--chrome" in seen["cmd"]


# ---------- which login it uses ----------
def test_api_keys_and_parent_session_variables_are_not_passed_on(tmp_path, monkeypatch):
    """With an API key in the environment Claude Code bills that key instead of the user's plan and switches the
    browser extension off, so the child process must get a clean environment."""
    for name, value in {
        "ANTHROPIC_API_KEY": "sk-should-not-leak", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "https://x",
        "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_CODE_SSE_PORT": "1",
        "CLAUDE_CODE_MESSAGING_TOKEN": "m", "CLAUDE_CODE_OAUTH_TOKEN": "o", "CLAUDE_CODE_USE_BEDROCK": "1",
        "R2_OPENAI_API_KEY": "gsk_other", "MY_HARMLESS_SETTING": "keep",
    }.items():
        monkeypatch.setenv(name, value)
    seen = {}
    ask_claude("hi", find=FOUND, popen=runner(FakeProc(stdout="{}"), seen), workdir=tmp_path)
    env = seen["kw"]["env"]
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "CLAUDE_CODE_USE_BEDROCK", "R2_OPENAI_API_KEY"):
        assert name not in env, name
    assert env["MY_HARMLESS_SETTING"] == "keep" and "PATH" in {k.upper() for k in env}


def test_a_failure_shows_the_real_error_not_an_unrelated_warning_on_stderr(tmp_path):
    proc = FakeProc(
        stdout=json.dumps({"is_error": True, "result": "Not logged in. Please run /login"}),
        stderr="warning: connectors are disabled because another auth source is set",
        code=1,
    )
    with pytest.raises(RuntimeError) as err:
        ask_claude("hi", find=FOUND, popen=runner(proc), workdir=tmp_path)
    assert "Not logged in" in str(err.value) and "connectors" not in str(err.value)
