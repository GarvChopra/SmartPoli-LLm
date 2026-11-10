from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from r2.brain import ledger_path
from r2.meetings import (
    MAX_TRANSCRIPT, MeetingError, MeetingStore, meeting_markdown, process_meeting, refresh_exports,
)
from r2.privacy import EgressLedger
from r2.runtime import Runtime, TaskBusy
from r2.skills import SkillError, SkillStore
from r2.tools import screen

WEB_DIR = Path(__file__).parent / "web"
DEMO_MAX_PROJECTS = 25
DEMO_MAX_TRANSCRIPT = 20_000

_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class PairBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class TaskBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value.strip()


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    members: list[str] = Field(min_length=1, max_length=20)


class MeetingBody(BaseModel):
    transcript: str = Field(min_length=1, max_length=MAX_TRANSCRIPT)
    title: str = Field(default="", max_length=100)
    date: str = Field(default="", max_length=10)
    me: str = Field(default="", max_length=60)


class StatusBody(BaseModel):
    status: str = Field(pattern="^(open|done)$")


class OwnerBody(BaseModel):
    owner: str = Field(min_length=1, max_length=80)


class ApproveBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    approve: bool


def create_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="R2", docs_url=None, redoc_url=None, openapi_url=None)
    bearer = HTTPBearer(auto_error=False)

    def require_token(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if creds is None or not rt.auth.verify(creds.credentials):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # ---- static page ----
    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/app.js")
    def app_js():
        return Response((WEB_DIR / "app.js").read_text(encoding="utf-8"), media_type="text/javascript")

    @app.get("/app.css")
    def app_css():
        return Response((WEB_DIR / "app.css").read_text(encoding="utf-8"), media_type="text/css")

    # ---- pairing (the only unauthenticated API) ----
    @app.post("/api/pair")
    def pair(body: PairBody):
        token = rt.auth.redeem(body.code)
        if token is None:
            raise HTTPException(status_code=401, detail="invalid or expired code")
        return {"token": token}

    # ---- authenticated API ----
    api = [Depends(require_token)]
    started: deque[float] = deque()  # demo profile only: when recent tasks began

    def demo_rate_limit() -> None:
        if rt.settings.profile != "demo":
            return
        now = time.time()
        while started and now - started[0] > 3600:
            started.popleft()
        if len(started) >= rt.settings.demo_tasks_per_hour:
            raise HTTPException(status_code=429, detail="The public demo is rate limited; please try again later.")
        started.append(now)

    @app.get("/api/status", dependencies=api)
    def status():
        return {**rt.tasks.status(), "demo": rt.settings.profile == "demo"}

    @app.post("/api/tasks", status_code=202, dependencies=api)
    def start_task(body: TaskBody):
        demo_rate_limit()
        try:
            return {"task_id": rt.tasks.start(body.message)}
        except TaskBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/approve", dependencies=api)
    def approve(body: ApproveBody):
        if not rt.broker.resolve(body.id, body.approve):
            raise HTTPException(status_code=404, detail="no such pending approval (it may have expired)")
        return {"ok": True}

    @app.post("/api/stop", dependencies=api)
    def stop():
        rt.tasks.stop()
        return {"ok": True}

    @app.get("/api/screenshot", dependencies=api)
    def screenshot():
        if rt.settings.profile == "demo":  # the public demo must never expose the host's screen
            raise HTTPException(status_code=403, detail="not available in the public demo")
        path = Path(rt.settings.data_dir) / "screens" / "latest.png"
        try:
            screen.screenshot(str(path))
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"cannot capture the screen: {exc}")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/facts", dependencies=api)
    def facts():
        return [{"id": f.id, "text": f.text, "source": f.source, "ts": f.ts} for f in rt.memory.list_facts()]

    @app.delete("/api/facts/{fact_id}", dependencies=api)
    def delete_fact(fact_id: int):
        if not rt.memory.delete_fact(fact_id):
            raise HTTPException(status_code=404, detail="no such fact")
        return {"ok": True}

    @app.get("/api/actions", dependencies=api)
    def actions():
        return rt.memory.list_actions(50)

    @app.get("/api/chat", dependencies=api)
    def chat():
        return rt.memory.recent_messages(40)

    @app.delete("/api/history", dependencies=api)
    def clear_history():
        rt.memory.clear_history()
        return {"ok": True}

    @app.get("/api/ledger", dependencies=api)
    def ledger():
        led = EgressLedger(ledger_path(rt.settings))
        rows = led.read(200)
        routes = [r for r in rows if "route" in r]
        return {
            "summary": led.summary(),
            "stayed_local": sum(1 for r in routes if not r.get("left_pc")),
            "routed": len(routes),
            "recent": rows[-12:][::-1],
        }

    @app.get("/api/skills", dependencies=api)
    def skills():
        store = SkillStore(rt.settings.data_dir)
        pack = lambda s: {"name": s.name, "description": s.description, "source": s.source, "body": s.body}
        return {"skills": [pack(s) for s in store.all()], "proposed": [pack(s) for s in store.proposed()]}

    @app.post("/api/skills/{name}/approve", dependencies=api)
    def approve_skill(name: str):
        try:
            SkillStore(rt.settings.data_dir).approve(name)
        except SkillError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        rt.memory.log_action("approve_skill", {"name": name}, "approved by you", "user")
        return {"ok": True}

    @app.delete("/api/skills/{name}/proposed", dependencies=api)
    def reject_skill(name: str):
        if not SkillStore(rt.settings.data_dir).reject(name):
            raise HTTPException(status_code=404, detail="no such proposal")
        return {"ok": True}

    # ---- projects, meeting summaries, tasks ----
    meetings = getattr(rt.registry, "meetings", None) or MeetingStore(
        Path(rt.settings.data_dir) / "projects.db", export_root=Path(rt.settings.data_dir) / "projects")

    def project_json(p) -> dict:
        items = meetings.list_items(p.id)
        return {"id": p.id, "name": p.name, "slug": p.slug, "members": [m.name for m in p.members],
                "open_tasks": sum(1 for i in items if i["status"] == "open"), "meetings": len(meetings.list_meetings(p.id))}

    def need_project(project_id: int):
        project = meetings.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="no such project")
        return project

    @app.get("/api/projects", dependencies=api)
    def projects():
        return [project_json(p) for p in meetings.list_projects()]

    @app.post("/api/projects", status_code=201, dependencies=api)
    def create_project(body: ProjectBody):
        if rt.settings.profile == "demo" and len(meetings.list_projects()) >= DEMO_MAX_PROJECTS:
            raise HTTPException(status_code=400, detail="The public demo is full; please use an existing project.")
        try:
            project = meetings.create_project(body.name, [m for m in body.members if m.strip()])
        except MeetingError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        rt.memory.log_action("create_project", {"name": project.name}, "created", "user")
        return project_json(project)

    @app.get("/api/projects/{project_id}", dependencies=api)
    def project_detail(project_id: int):
        project = need_project(project_id)
        return {**project_json(project), "meeting_list": meetings.list_meetings(project.id),
                "items": meetings.list_items(project.id)}

    @app.post("/api/projects/{project_id}/meetings", status_code=201, dependencies=api)
    def add_meeting(project_id: int, body: MeetingBody):
        project = need_project(project_id)
        if rt.settings.profile == "demo" and len(body.transcript) > DEMO_MAX_TRANSCRIPT:
            raise HTTPException(status_code=400, detail=f"The public demo accepts up to {DEMO_MAX_TRANSCRIPT} characters per meeting.")
        demo_rate_limit()
        # a hosted model writes better minutes; without one, the offline rules do the job
        brain = getattr(rt.registry, "meeting_brain", None)
        try:
            meeting = process_meeting(meetings, project, body.transcript, brain=brain, title=body.title or None,
                                      held_on=body.date or None, me=body.me or None)
        except (MeetingError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        rt.memory.log_action("summarize_meeting", {"project": project.name, "meeting": meeting.id}, meeting.method, "user")
        return {"id": meeting.id, "title": meeting.title, "held_on": meeting.held_on, "summary": meeting.summary,
                "decisions": list(meeting.decisions), "questions": list(meeting.questions), "method": meeting.method,
                "warnings": list(meeting.warnings), "items": list(meeting.items)}

    @app.get("/api/meetings/{meeting_id}", dependencies=api)
    def meeting_detail(meeting_id: int):
        meeting = meetings.get_meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="no such meeting")
        project = meetings.get_project(meeting.project_id)
        return {"id": meeting.id, "title": meeting.title, "held_on": meeting.held_on, "summary": meeting.summary,
                "decisions": list(meeting.decisions), "questions": list(meeting.questions), "method": meeting.method,
                "warnings": list(meeting.warnings), "items": list(meeting.items),
                "markdown": meeting_markdown(project, meeting)}

    def change_item(item_id: int, action):
        item = meetings.get_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such task")
        try:
            action()
        except MeetingError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        refresh_exports(meetings, meetings.get_project(item["project_id"]))
        return meetings.get_item(item_id)

    @app.post("/api/items/{item_id}/status", dependencies=api)
    def item_status(item_id: int, body: StatusBody):
        return change_item(item_id, lambda: meetings.set_status(item_id, body.status))

    @app.post("/api/items/{item_id}/owner", dependencies=api)
    def item_owner(item_id: int, body: OwnerBody):
        return change_item(item_id, lambda: meetings.reassign(item_id, body.owner))

    @app.post("/api/revoke", dependencies=api)
    def revoke():
        if rt.settings.profile == "demo":  # one visitor must not be able to sign everyone else out
            raise HTTPException(status_code=403, detail="not available in the public demo")
        rt.auth.revoke_all()
        return {"ok": True}

    return app
