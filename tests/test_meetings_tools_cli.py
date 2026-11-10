import json

import pytest

from r2 import cli
from r2.brain import FakeBrain
from r2.config import Settings
from r2.meetings import MeetingError, MeetingStore, read_transcript_file
from r2.memory import Memory
from r2.router import select_tools
from r2.tools import full_registry
from r2.types import Decision, Risk

TRANSCRIPT = (
    "Garv: Aman, please finish the login page by Friday.\n"
    "Garv: Riya will handle the API documentation and Karan will do the QA pass before 20 October.\n"
    "Garv: Someone needs to update the privacy policy.\n"
)
SRT = "1\n00:00:01,000 --> 00:00:04,000\nGarv: Karan, can you deploy the staging build by Wednesday?\n"


@pytest.fixture
def reg(tmp_path):
    return full_registry(Memory(tmp_path / "m.db"), tmp_path, headless=True, claude_finder=lambda: None)


@pytest.fixture
def transcript_file(tmp_path):
    f = tmp_path / "standup.txt"
    f.write_text(TRANSCRIPT, encoding="utf-8")
    return f


# ---------- agent tools ----------
def test_the_meeting_tools_create_summarize_list_and_complete(reg, transcript_file):
    assert "Created project 'Launch'" in reg.get("create_project").func("Launch", "Aman Sharma=Aman, Riya, Karan")
    assert reg.get("create_project").func("Launch", "A, B").startswith("ERROR")  # duplicate

    out = reg.get("summarize_meeting").func("Launch", str(transcript_file), date="2026-10-12")
    assert "Action items" in out and "Aman Sharma: Finish the login page." in out and "TASKS.md" in out
    tasks = reg.get("list_tasks").func("Launch")
    assert "Riya" in tasks and "[open]" in tasks
    assert "Aman" in reg.get("list_tasks").func("launch", owner="aman sharma") and "Riya" not in reg.get("list_tasks").func("Launch", owner="aman sharma")

    first_id = int(tasks.split()[0].lstrip("#"))
    assert reg.get("complete_task").func(first_id).startswith("Marked task")
    assert "No matching tasks." not in reg.get("list_tasks").func("Launch", status="all")
    assert first_id not in [int(line.split()[0].lstrip("#")) for line in reg.get("list_tasks").func("Launch").splitlines()]
    assert reg.get("complete_task").func(9999).startswith("ERROR")


def test_tool_errors_are_plain_messages(reg, tmp_path):
    assert reg.get("summarize_meeting").func("Nope", "x.txt").startswith("ERROR: no project called 'Nope'")
    reg.get("create_project").func("Launch", "Aman, Riya")
    assert reg.get("summarize_meeting").func("Launch", str(tmp_path / "missing.txt")).startswith("ERROR: no such file")
    assert reg.get("list_tasks").func("Nope").startswith("ERROR")


def test_the_tools_use_the_hosted_brain_when_the_registry_has_one(reg, transcript_file):
    reply = json.dumps({"summary": "Model minutes.", "decisions": [], "open_questions": [], "action_items": []})
    reg.meeting_brain = FakeBrain([Decision(final=reply)])
    reg.get("create_project").func("Launch", "Aman, Riya, Karan")
    assert "Model minutes." in reg.get("summarize_meeting").func("Launch", str(transcript_file), date="2026-10-12")


def test_reading_a_secret_looking_file_still_needs_approval_and_tools_are_offered_for_meeting_requests(reg):
    spec = reg.get("summarize_meeting")
    assert spec.risk_for({"path": "C:/x/standup.txt"}).risk is Risk.SAFE
    assert spec.risk_for({"path": "C:/Users/me/.ssh/id_rsa"}).risk is Risk.CONFIRM
    names = {s["function"]["name"] for s in select_tools("summarize my meeting transcript for the Launch project", reg.schemas())}
    assert {"summarize_meeting", "list_tasks", "complete_task", "create_project"} <= names
    assert "run_shell" not in names


# ---------- reading files ----------
def test_srt_and_vtt_files_are_read_and_unknown_types_are_refused(tmp_path):
    srt = tmp_path / "call.srt"
    srt.write_text(SRT, encoding="utf-8")
    assert "deploy the staging build" in read_transcript_file(srt)
    for name in ("x.exe", "notes.docx"):
        (tmp_path / name).write_bytes(b"MZ")
        with pytest.raises(MeetingError, match="unsupported file type"):
            read_transcript_file(tmp_path / name)
    with pytest.raises(MeetingError, match="no such file"):
        read_transcript_file(tmp_path / "nope.txt")


def test_audio_without_the_voice_extras_explains_what_to_do(tmp_path, monkeypatch):
    audio = tmp_path / "call.mp3"
    audio.write_bytes(b"\x00" * 10)
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", None)  # make the import fail
    with pytest.raises(MeetingError, match="voice extras"):
        read_transcript_file(audio)


def test_oversized_files_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("r2.meetings.MAX_FILE_BYTES", 10)
    big = tmp_path / "big.txt"
    big.write_text("x" * 50, encoding="utf-8")
    with pytest.raises(MeetingError, match="too large"):
        read_transcript_file(big)


# ---------- command line ----------
def settings_for(tmp_path):
    return Settings(data_dir=tmp_path, db_path=tmp_path / "r2.db", claude=False)


def test_cli_project_meeting_and_tasks_flow(tmp_path, transcript_file, capsys):
    s = settings_for(tmp_path)
    assert cli.cmd_project(s, "list", None, None) == 0 and "No projects yet" in capsys.readouterr().out
    assert cli.cmd_project(s, "create", "Launch", "Aman Sharma=Aman|Ams, Riya, Karan") == 0
    assert cli.cmd_project(s, "create", "Launch", "A, B") == 1  # duplicate
    assert cli.cmd_project(s, "create", "Empty", "") == 1
    assert cli.cmd_project(s, "add-member", "Launch", "Neha") == 0
    capsys.readouterr()

    assert cli.cmd_meeting(s, "add", "Launch", str(transcript_file), "Sync", "2026-10-12", None, "offline") == 0
    out = capsys.readouterr().out
    assert "Sync (2026-10-12)" in out and "Aman Sharma: Finish the login page." in out and "Saved:" in out
    assert (tmp_path / "projects" / "launch" / "TASKS.md").is_file()
    assert cli.cmd_meeting(s, "list", "Launch", None, None, None, None, "auto") == 0 and "Sync" in capsys.readouterr().out
    assert cli.cmd_meeting(s, "show", "1", None, None, None, None, "auto") == 0 and "### Aman Sharma" in capsys.readouterr().out
    assert cli.cmd_meeting(s, "show", "99", None, None, None, None, "auto") == 1
    assert cli.cmd_meeting(s, "add", "Nope", str(transcript_file), None, None, None, "offline") == 1
    assert cli.cmd_meeting(s, "add", "Launch", str(tmp_path / "gone.txt"), None, None, None, "offline") == 1
    capsys.readouterr()

    assert cli.cmd_tasks(s, "Launch", "list", None, None, False) == 0
    listing = capsys.readouterr().out
    assert "Aman Sharma: Finish the login page." in listing and "[ ]" in listing
    assert cli.cmd_tasks(s, "Launch", "done", 1, None, False) == 0
    assert cli.cmd_tasks(s, "Launch", "list", None, None, False) == 0 and "#1 " not in capsys.readouterr().out.split("updated.")[-1]
    assert cli.cmd_tasks(s, "Launch", "list", None, None, True) == 0 and "[x] #1" in capsys.readouterr().out
    assert cli.cmd_tasks(s, "Launch", "reopen", 1, None, False) == 0
    last = len(MeetingStore(tmp_path / "projects.db").list_items(1))
    assert cli.cmd_tasks(s, "Launch", "assign", last, "neha", False) == 0
    assert cli.cmd_tasks(s, "Launch", "assign", last, "Stranger", False) == 1
    assert cli.cmd_tasks(s, "Launch", "assign", last, None, False) == 1
    assert cli.cmd_tasks(s, "Launch", "done", 999, None, False) == 1
    assert cli.cmd_tasks(s, "Nope", "list", None, None, False) == 1


def test_cli_reads_the_transcript_from_stdin(tmp_path, monkeypatch, capsys):
    import io

    s = settings_for(tmp_path)
    cli.cmd_project(s, "create", "Launch", "Aman, Riya")
    monkeypatch.setattr("sys.stdin", io.StringIO("Garv: Riya, please send the invoice tomorrow.\n"))
    assert cli.cmd_meeting(s, "add", "Launch", "-", None, "2026-10-12", None, "offline") == 0
    assert "Riya: Send the invoice." in capsys.readouterr().out


def test_cli_uses_the_offline_rules_unless_a_hosted_model_is_configured(tmp_path):
    local = settings_for(tmp_path)
    assert cli._meeting_brain(local, "auto") is None and cli._meeting_brain(local, "offline") is None
