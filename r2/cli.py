from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from r2.agent import Agent
from r2.auth import AuthStore
from r2.brain import BrainError, make_brain, make_chat_brain
from r2.claude_bridge import find_claude
from r2.doctor import claude_version, format_report, run_checks, time_brain
from r2.cloud_setup import setup_cloud, write_env
from r2.config import Settings, load_dotenv
from r2.memory import Memory
from r2.tools import full_registry
from r2.types import ToolCall


def _db_path(settings: Settings):
    if settings.db_path.parent == Path("."):
        return settings.data_dir / settings.db_path.name
    return settings.db_path


def build_agent(settings: Settings, approver) -> Agent:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    memory = Memory(_db_path(settings))
    registry = full_registry(
        memory, settings.data_dir, settings.headless,
        claude_dirs=settings.claude_dirs,
        claude_finder=find_claude if settings.claude else (lambda: None),
        profile=settings.profile,
    )
    return Agent(
        make_brain(settings),
        registry,
        memory,
        approver,
        max_steps=settings.max_steps,
        chat_brain=make_chat_brain(settings),
        smart_routing=True,
    )


def _ask_on_terminal(call: ToolCall) -> bool:
    answer = input(f"Allow {call.name}({call.args})? [y/N] ")
    return answer.strip().lower() == "y"


def _public_base(settings: Settings) -> str:
    return (os.environ.get("R2_PUBLIC_URL") or f"http://localhost:{settings.port}").rstrip("/")


def cmd_ask(settings: Settings, task: str) -> int:
    try:
        agent = build_agent(settings, _ask_on_terminal)
    except BrainError as exc:
        print(exc)
        return 1
    streamed = []

    def show(piece: str) -> None:
        streamed.append(piece)
        print(piece, end="", flush=True)

    try:
        result = agent.run(task, on_token=show)
    finally:
        agent.registry.close()
    if streamed and "".join(streamed).strip() == result.answer.strip():
        print()  # the answer was already printed word by word
    else:
        print(result.answer)
    return 0 if result.status == "done" else 1


def cmd_pair(settings: Settings) -> int:
    import qrcode

    store = AuthStore(settings.data_dir / "tokens.json")
    code = store.new_pairing_code()
    link = f"{_public_base(settings)}/?code={code}"
    print(f"Pairing code: {code}  (valid 10 minutes, works once)\n")
    print(f"Open this on the device you want to pair:\n  {link}\n")
    qr = qrcode.QRCode(border=1)
    qr.add_data(link)
    qr.print_ascii(invert=True)
    if "localhost" in link:
        print("\nTip: for your phone, set R2_PUBLIC_URL to your Tailscale address (see README).")
    return 0


def cmd_revoke(settings: Settings) -> int:
    AuthStore(settings.data_dir / "tokens.json").revoke_all()
    print("All paired devices have been signed out.")
    return 0


def cmd_forget(settings: Settings) -> int:
    Memory(_db_path(settings)).clear_history()
    print("Forgot your chat history, action log and learned habits. Saved facts were kept.")
    return 0


def cmd_skills(settings: Settings, action: str, name: str | None) -> int:
    from r2.skills import SkillError, SkillStore

    store = SkillStore(settings.data_dir)
    if action == "list":
        skills = store.all()
        for s in skills:
            print(f"  {s.name:<22} {s.description}  [{s.source}]")
        pending = store.proposed()
        if pending:
            print(f"\n{len(pending)} proposed by R2, waiting for you: " + ", ".join(p.name for p in pending))
            print("Read one with: r2 skills show NAME   Activate with: r2 skills approve NAME")
        for problem in store.problems:
            print(f"  (skipped) {problem}")
        return 0
    if not name:
        print(f"r2 skills {action} needs a skill name.")
        return 1
    try:
        if action == "show":
            skill = store.get(name) or next((p for p in store.proposed() if p.name == name), None)
            if skill is None:
                print(f"No skill called '{name}'.")
                return 1
            print(f"{skill.name} [{skill.source}]\n{skill.description}\n\n{skill.body}")
        elif action == "approve":
            store.approve(name)
            print(f"'{name}' is now active.")
        elif action == "reject":
            print("Deleted." if store.reject(name) else f"No proposed skill called '{name}'.")
    except SkillError as exc:
        print(f"Could not do that: {exc}")
        return 1
    return 0


def cmd_tools(settings: Settings, action: str, name: str | None) -> int:
    from r2 import policy
    from r2.policy import load_disabled, policy_path

    memory = Memory(_db_path(settings))
    every = full_registry(
        memory, settings.data_dir, True, claude_dirs=settings.claude_dirs,
        claude_finder=find_claude if settings.claude else (lambda: None),
    )
    disabled = load_disabled(settings.data_dir)
    all_names = set(every.names()) | disabled  # disabled tools are already gone from the registry
    if action == "list":
        for tool in sorted(all_names):
            print(f"  [{'off' if tool in disabled else 'on '}] {tool}")
        print(f"\nChange it with: r2 tools disable NAME / enable NAME   (saved in {policy_path(settings.data_dir)})")
        return 0
    if not name or name not in all_names:
        print(f"Unknown tool '{name}'. See: r2 tools")
        return 1
    if action == "disable":
        disabled.add(name)
    else:
        disabled.discard(name)
    policy.save_disabled(settings.data_dir, disabled)
    print(f"'{name}' is now {'OFF' if name in disabled else 'on'}. Restart R2 to apply.")
    return 0


def cmd_memory(settings: Settings, action: str, prune: bool) -> int:
    from r2.memory_files import export_facts, import_facts

    memory = Memory(_db_path(settings))
    path = settings.data_dir / "memory" / "MEMORY.md"
    if action == "export":
        count = export_facts(memory, path)
        print(f"Wrote {count} facts to {path}. Edit it, then: r2 memory import")
        return 0
    if not path.is_file():
        print(f"{path} does not exist yet. Run: r2 memory export")
        return 1
    print(import_facts(memory, path, prune=prune).summary())
    return 0


def _meeting_store(settings: Settings):
    from r2.meetings import MeetingStore

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return MeetingStore(settings.data_dir / "projects.db", export_root=settings.data_dir / "projects")


def _meeting_brain(settings: Settings, mode: str):
    """offline: rules only. auto: the hosted model if one is configured. local: also allow the local Ollama model."""
    if mode == "offline" or (mode == "auto" and settings.brain != "openai"):
        return None
    try:
        return make_brain(settings)
    except BrainError as exc:
        print(f"(model not available: {exc}; using the offline rules)")
        return None


def cmd_project(settings: Settings, action: str, name: str | None, members: str | None) -> int:
    from r2.meetings import MeetingError

    store = _meeting_store(settings)
    try:
        if action == "list":
            projects = store.list_projects()
            for p in projects:
                print(f"  {p.name} ({p.slug}): {', '.join(m.name for m in p.members)}")
            if not projects:
                print("No projects yet. Create one: r2 project create \"Launch\" --members \"Aman, Riya, Karan\"")
            return 0
        if not name:
            print(f"r2 project {action} needs a project name.")
            return 1
        if action == "create":
            project = store.create_project(name, [m for m in (members or "").replace(";", ",").split(",") if m.strip()])
            print(f"Created '{project.name}' with {', '.join(m.name for m in project.members)}.")
            print("Nicknames help matching: --members \"Aman Sharma=Aman|Ams, Riya, Karan\"")
        elif action == "add-member":
            project = store.get_project(name)
            if project is None:
                print(f"No project called '{name}'.")
                return 1
            print(f"Added {store.add_member(project.id, members or '').name}.")
    except MeetingError as exc:
        print(f"Could not do that: {exc}")
        return 1
    return 0


def cmd_meeting(settings: Settings, action: str, target: str | None, source: str | None, title: str | None,
                date: str | None, me: str | None, mode: str) -> int:
    from r2.meetings import MeetingError, meeting_markdown, process_meeting, read_transcript_file, summary_text

    store = _meeting_store(settings)
    try:
        if action == "show":
            meeting = store.get_meeting(int(target or 0))
            if meeting is None:
                print(f"No meeting #{target}.")
                return 1
            project = store.get_project(meeting.project_id)
            print(meeting_markdown(project, meeting))
            return 0
        project = store.get_project(target or "")
        if project is None:
            print(f"No project called '{target}'. See: r2 project list")
            return 1
        if action == "list":
            for m in store.list_meetings(project.id):
                print(f"  #{m['id']} {m['held_on']}  {m['title']}  ({m['items']} items, {m['method']})")
            return 0
        if not source:
            print("Give a transcript file, or - to read it from the keyboard/pipe.")
            return 1
        text = sys.stdin.read() if source == "-" else read_transcript_file(source, settings.whisper_model)
        meeting = process_meeting(store, project, text, brain=_meeting_brain(settings, mode),
                                  title=title or (None if source == "-" else Path(source).stem), held_on=date, me=me)
        print(summary_text(project, meeting, store.export_root / project.slug / "TASKS.md"))
    except (MeetingError, ValueError, OSError) as exc:
        print(f"Could not do that: {exc}")
        return 1
    return 0


def cmd_tasks(settings: Settings, project_ref: str, action: str, task_id: int | None, owner: str | None, show_all: bool) -> int:
    from r2.meetings import MeetingError, refresh_exports

    store = _meeting_store(settings)
    try:
        if action == "list":
            project = store.get_project(project_ref)
            if project is None:
                print(f"No project called '{project_ref}'. See: r2 project list")
                return 1
            items = store.list_items(project.id, owner=owner, status=None if show_all else "open")
            for i in items:
                mark = "x" if i["status"] == "done" else " "
                flag = "" if i["verified"] else "  [unverified]"
                print(f"  [{mark}] #{i['id']} {i['owner']}: {i['task']}  (due: {i['due'] or i['due_text'] or 'none'}){flag}")
            print("No tasks." if not items else "")
            return 0
        item = store.get_item(task_id or 0)
        if item is None:
            print(f"No task #{task_id}.")
            return 1
        if action in ("done", "reopen"):
            store.set_status(item["id"], "done" if action == "done" else "open")
        elif action == "assign":
            if not owner:
                print("Say who: r2 tasks PROJECT assign ID --owner NAME")
                return 1
            store.reassign(item["id"], owner)
        refresh_exports(store, store.get_project(item["project_id"]))
        print(f"Task #{item['id']} updated.")
    except MeetingError as exc:
        print(f"Could not do that: {exc}")
        return 1
    return 0


def cmd_ledger(settings: Settings) -> int:
    from r2.brain import ledger_path
    from r2.privacy import EgressLedger, format_ledger

    print(format_ledger(EgressLedger(ledger_path(settings))))
    return 0


def _run_doctor_checks(settings: Settings):
    return run_checks(settings, claude_version=claude_version)


def _bench_brains(settings: Settings) -> list:
    brains = []
    chat = make_chat_brain(settings)
    if chat is not None:
        brains.append((settings.chat_model, chat))
    brains.append((settings.cloud_model if settings.brain == "cloud" else settings.model, make_brain(settings)))
    return brains


def cmd_doctor(settings: Settings) -> int:
    checks = _run_doctor_checks(settings)
    print(format_report(checks))
    return 1 if any(c.status == "fail" for c in checks) else 0


def cmd_bench(settings: Settings) -> int:
    print("Timing a short reply from each model (the first, discarded run loads the model)...")
    for label, brain in _bench_brains(settings):
        prompt = "Say hello in five words."
        try:
            time_brain(brain, prompt)  # warm-up: loads the model
            first, total, rate = time_brain(brain, prompt)
        except BrainError as exc:
            print(f"{label:22} failed: {exc}")
            continue
        print(f"{label:22} first word {first:5.1f}s   total {total:5.1f}s   {rate:4.1f} words/s")
    return 0


def cmd_local() -> int:
    write_env(Path(".env"), {"R2_BRAIN": "ollama", "R2_CHAT_MODEL": "qwen2.5:1.5b"})
    print("R2 will run fully on this PC again. Restart it for the change to apply (tray icon -> Quit, then r2 daemon).")
    return 0


def cmd_serve(settings: Settings) -> int:
    import uvicorn

    from r2.runtime import build_runtime
    from r2.server import create_app

    is_loopback = ("127.0.0.1", "localhost", "::1").__contains__  # not r2.daemon: it needs the desktop overlay

    # R2 controls this PC, so it only listens on localhost. The one exception is the hosted demo profile,
    # which has switched off every tool that touches a machine (see r2/policy.py).
    if not is_loopback(settings.host) and settings.profile != "demo" and os.environ.get("R2_ALLOW_LAN") != "1":
        print(f"Refusing to listen on {settings.host}: R2 must listen on localhost only "
              "(set R2_PROFILE=demo for the hosted demo, or R2_ALLOW_LAN=1 to override).")
        return 2
    rt = build_runtime(settings)
    try:
        uvicorn.run(create_app(rt), host=settings.host, port=settings.port)
    finally:
        rt.registry.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(prog="r2", description="R2 local PC assistant")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="run one task in this terminal")
    ask.add_argument("task")
    sub.add_parser("pair", help="print a pairing link and QR code for a phone or browser")
    sub.add_parser("revoke", help="sign out every paired device")
    sub.add_parser("forget", help="erase chat history, action log and learned habits (keeps saved facts)")
    sub.add_parser("doctor", help="check that models, server and tools are set up, and say what is slow")
    sub.add_parser("bench", help="time how fast each model answers on this PC")
    sub.add_parser("cloud", help="connect R2 to a hosted model (guided; free-tier options)")
    sub.add_parser("local", help="go back to running everything on this PC")
    skills = sub.add_parser("skills", help="list, read, approve or reject R2's reusable skills")
    skills.add_argument("action", nargs="?", default="list", choices=["list", "show", "approve", "reject"])
    skills.add_argument("name", nargs="?")
    tools = sub.add_parser("tools", help="see which tools R2 may use, and switch them on or off")
    tools.add_argument("action", nargs="?", default="list", choices=["list", "enable", "disable"])
    tools.add_argument("name", nargs="?")
    mem = sub.add_parser("memory", help="export what R2 knows to a markdown file you can edit, or import it back")
    mem.add_argument("action", choices=["export", "import"])
    mem.add_argument("--prune", action="store_true", help="on import, forget facts you deleted from the file")
    proj = sub.add_parser("project", help="team projects that hold meeting summaries and tasks")
    proj.add_argument("action", nargs="?", default="list", choices=["list", "create", "add-member"])
    proj.add_argument("name", nargs="?")
    proj.add_argument("--members", help='comma separated, e.g. "Aman Sharma=Aman|Ams, Riya, Karan"')
    meet = sub.add_parser("meeting", help="summarize a meeting transcript or recording and save it to a project")
    meet.add_argument("action", choices=["add", "list", "show"])
    meet.add_argument("target", nargs="?", help="add/list: the project; show: the meeting number")
    meet.add_argument("source", nargs="?", help="transcript file (.txt .vtt .srt) or audio, or - for stdin")
    meet.add_argument("--title")
    meet.add_argument("--date", help="YYYY-MM-DD (default today)")
    meet.add_argument("--me", help="your name, for 'I will ...' when the transcript has no speaker names")
    meet.add_argument("--mode", choices=["auto", "offline", "local"], default="auto",
                      help="auto: hosted model if configured; offline: rules only; local: also use the local Ollama model")
    tsk = sub.add_parser("tasks", help="a project's action items: list, mark done, reopen, reassign")
    tsk.add_argument("project")
    tsk.add_argument("action", nargs="?", default="list", choices=["list", "done", "reopen", "assign"])
    tsk.add_argument("task_id", nargs="?", type=int)
    tsk.add_argument("--owner")
    tsk.add_argument("--all", action="store_true", help="include finished tasks")
    sub.add_parser("ledger", help="show everything R2 sent to a hosted model, and what was masked first")
    sub.add_parser("care", help="run the SmartPoli sandbox actions service for the eGain hackathon")
    sub.add_parser("support", help="run the Penny support-actions backend for the eGain hackathon")
    sub.add_parser("serve", help="run the server in this terminal (no tray icon)")
    sub.add_parser("daemon", help="run the background assistant with a tray icon")
    args = parser.parse_args(argv)

    load_dotenv()
    settings = Settings.from_env()
    if args.command == "ask":
        return cmd_ask(settings, args.task)
    if args.command == "pair":
        return cmd_pair(settings)
    if args.command == "revoke":
        return cmd_revoke(settings)
    if args.command == "cloud":
        return setup_cloud()
    if args.command == "local":
        return cmd_local()
    if args.command == "doctor":
        return cmd_doctor(settings)
    if args.command == "bench":
        return cmd_bench(settings)
    if args.command == "project":
        return cmd_project(settings, args.action, args.name, args.members)
    if args.command == "meeting":
        return cmd_meeting(settings, args.action, args.target, args.source, args.title, args.date, args.me, args.mode)
    if args.command == "tasks":
        return cmd_tasks(settings, args.project, args.action, args.task_id, args.owner, args.all)
    if args.command == "tools":
        return cmd_tools(settings, args.action, args.name)
    if args.command == "memory":
        return cmd_memory(settings, args.action, args.prune)
    if args.command == "skills":
        return cmd_skills(settings, args.action, args.name)
    if args.command == "ledger":
        return cmd_ledger(settings)
    if args.command == "forget":
        return cmd_forget(settings)
    if args.command == "care":
        from r2 import care_actions

        return care_actions.main()
    if args.command == "support":
        from r2 import support

        return support.main()
    if args.command == "serve":
        return cmd_serve(settings)
    from r2 import daemon

    return daemon.main()


if __name__ == "__main__":
    sys.exit(main())
