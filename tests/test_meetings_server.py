import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from r2.brain import FakeBrain
from r2.config import Settings
from r2.runtime import build_runtime
from r2.server import create_app
from r2.tools import ToolRegistry
from r2.types import Decision

TRANSCRIPT = (
    "Garv: Aman, please finish the login page by Friday.\n"
    "Garv: Riya will handle the API documentation and Karan will do the QA pass before 20 October.\n"
    "Garv: Someone needs to update the privacy policy.\n"
    "Garv: We decided to go with the dark theme."
)


@pytest.fixture
def rt(tmp_path):
    return build_runtime(Settings(data_dir=tmp_path, db_path=tmp_path / "t.db"),
                         brain=FakeBrain([Decision(final="hi")]), registry=ToolRegistry())


@pytest.fixture
def client(rt):
    return TestClient(create_app(rt))


@pytest.fixture
def authed(rt, client):
    client.headers["Authorization"] = "Bearer " + rt.auth.redeem(rt.auth.new_pairing_code())
    return client


def make_project(authed, name="Launch"):
    res = authed.post("/api/projects", json={"name": name, "members": ["Aman Sharma=Aman", "Riya", "Karan"]})
    assert res.status_code == 201
    return res.json()


@pytest.mark.parametrize("method,path", [
    ("get", "/api/projects"), ("post", "/api/projects"), ("get", "/api/projects/1"), ("post", "/api/projects/1/meetings"),
    ("get", "/api/meetings/1"), ("post", "/api/items/1/status"), ("post", "/api/items/1/owner"),
])
def test_every_meeting_route_requires_a_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_create_list_and_validate_projects(authed):
    project = make_project(authed)
    assert project["members"] == ["Aman Sharma", "Riya", "Karan"] and project["open_tasks"] == 0
    assert authed.get("/api/projects").json()[0]["name"] == "Launch"
    assert authed.post("/api/projects", json={"name": "Launch", "members": ["A"]}).status_code == 400  # duplicate
    assert authed.post("/api/projects", json={"name": "", "members": ["A"]}).status_code == 422
    assert authed.post("/api/projects", json={"name": "X", "members": []}).status_code == 422
    assert authed.get("/api/projects/999").status_code == 404


def test_summarize_a_meeting_then_work_the_tasks(authed, rt, tmp_path):
    project = make_project(authed)
    res = authed.post(f"/api/projects/{project['id']}/meetings",
                      json={"transcript": TRANSCRIPT, "title": "Launch sync", "date": "2026-10-12"})
    assert res.status_code == 201
    meeting = res.json()
    assert meeting["title"] == "Launch sync" and meeting["method"] == "rules"
    owners = {i["owner"] for i in meeting["items"]}
    assert {"Aman Sharma", "Riya", "Karan", "Unassigned"} <= owners
    assert any("dark theme" in d for d in meeting["decisions"])

    detail = authed.get(f"/api/projects/{project['id']}").json()
    assert detail["open_tasks"] == len(meeting["items"]) and detail["meeting_list"][0]["id"] == meeting["id"]
    assert (tmp_path / "projects" / "launch" / "TASKS.md").is_file()

    first = detail["items"][0]
    assert authed.post(f"/api/items/{first['id']}/status", json={"status": "done"}).json()["status"] == "done"
    assert authed.post(f"/api/items/{first['id']}/status", json={"status": "bogus"}).status_code == 422
    unassigned = next(i for i in detail["items"] if i["owner"] == "Unassigned")
    assert authed.post(f"/api/items/{unassigned['id']}/owner", json={"owner": "Karan"}).json()["owner"] == "Karan"
    assert authed.post(f"/api/items/{unassigned['id']}/owner", json={"owner": "Stranger"}).status_code == 400
    assert authed.post("/api/items/999/status", json={"status": "done"}).status_code == 404
    assert "- [x]" in (tmp_path / "projects" / "launch" / "TASKS.md").read_text(encoding="utf-8")

    md = authed.get(f"/api/meetings/{meeting['id']}").json()["markdown"]
    assert "### Aman Sharma" in md and "## Decisions" in md
    assert authed.get("/api/meetings/999").status_code == 404
    assert any(a["tool"] == "summarize_meeting" for a in rt.memory.list_actions(10))


def test_bad_transcripts_are_rejected(authed):
    project = make_project(authed)
    url = f"/api/projects/{project['id']}/meetings"
    assert authed.post(url, json={"transcript": "too short"}).status_code == 400
    assert authed.post(url, json={"transcript": ""}).status_code == 422
    assert authed.post("/api/projects/999/meetings", json={"transcript": TRANSCRIPT}).status_code == 404
    assert authed.post(url, json={"transcript": TRANSCRIPT, "date": "not-a-date"}).status_code == 400


def test_the_hosted_model_is_used_when_the_registry_has_one(authed, rt):
    reply = json.dumps({"summary": "A model wrote this.", "decisions": [], "open_questions": [], "action_items": []})
    rt.registry.meeting_brain = FakeBrain([Decision(final=reply)])
    project = make_project(authed)
    meeting = authed.post(f"/api/projects/{project['id']}/meetings", json={"transcript": TRANSCRIPT, "date": "2026-10-12"}).json()
    assert meeting["method"] == "model+rules" and meeting["summary"] == "A model wrote this."


def test_the_meeting_endpoints_are_rate_limited_in_the_demo_profile(tmp_path):
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / "t.db", profile="demo", demo_code="OPEN", demo_tasks_per_hour=2)
    rt = build_runtime(settings, brain=FakeBrain([Decision(final="hi")]), registry=ToolRegistry())
    client = TestClient(create_app(rt))
    client.headers["Authorization"] = "Bearer " + client.post("/api/pair", json={"code": "open"}).json()["token"]
    project = client.post("/api/projects", json={"name": "D", "members": ["Aman", "Riya"]}).json()
    url = f"/api/projects/{project['id']}/meetings"
    codes = [client.post(url, json={"transcript": TRANSCRIPT, "date": "2026-10-12"}).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


# ---------- the real page, in a real Chrome ----------
@pytest.fixture
def live_server(tmp_path):
    rt = build_runtime(Settings(data_dir=tmp_path, db_path=tmp_path / "t.db"),
                       brain=FakeBrain([Decision(final="hi")]), registry=ToolRegistry())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(rt), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    yield rt, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_meetings_panel_in_a_real_browser(live_server):
    sync_api = pytest.importorskip("playwright.sync_api")
    rt, url = live_server
    code = rt.auth.new_pairing_code()
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"no Chrome available: {exc}")
        page = browser.new_page(viewport={"width": 390, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"{url}/?code={code}")
        page.click("#pairBtn")
        page.wait_for_selector("#app:not([hidden])")

        page.click("#projNew")
        page.fill("#projName", "Launch")
        page.fill("#projMembers", "Aman Sharma=Aman, Riya, Karan")
        page.click("#projCreate")
        page.locator("#projSelect option", has_text="Launch").wait_for(state="attached")

        page.fill("#trText", TRANSCRIPT + "\nGarv: <img src=x onerror=window.__xss=1> Riya, please check this.")
        page.fill("#trDate", "2026-10-12")
        page.click("#trGo")
        page.locator("#trResult:not([hidden])").wait_for()
        assert "action item(s) saved" in page.inner_text("#trMethod")
        assert "dark theme" in page.inner_text("#trDecisions")
        tasks = page.inner_text("#taskList")
        assert "Aman Sharma" in tasks and "login page" in tasks and "2026-10-16" in tasks and "Unassigned" in tasks
        assert page.evaluate("() => window.__xss") is None and page.locator("#taskList img").count() == 0

        box = page.locator("#taskList li input[type=checkbox]").first
        box.check()
        page.locator("#taskList .task.done").first.wait_for()
        who = page.locator("#taskList li select").last
        who.select_option("Karan")
        page.wait_for_function("() => document.querySelector('#projSelect option').textContent.includes('open')")
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        assert not [e for e in errors if "401" not in e and "Unauthorized" not in e]
        browser.close()


def test_the_demo_caps_projects_and_transcript_size(tmp_path, monkeypatch):
    monkeypatch.setattr("r2.server.DEMO_MAX_PROJECTS", 2)
    monkeypatch.setattr("r2.server.DEMO_MAX_TRANSCRIPT", 100)
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / "t.db", profile="demo", demo_code="OPEN")
    rt = build_runtime(settings, brain=FakeBrain([Decision(final="hi")]), registry=ToolRegistry())
    client = TestClient(create_app(rt))
    client.headers["Authorization"] = "Bearer " + client.post("/api/pair", json={"code": "open"}).json()["token"]
    ids = [client.post("/api/projects", json={"name": f"P{n}", "members": ["A", "B"]}) for n in range(3)]
    assert [r.status_code for r in ids] == [201, 201, 400]
    url = f"/api/projects/{ids[0].json()['id']}/meetings"
    assert client.post(url, json={"transcript": "Garv: A will do the report by Friday. " * 10}).status_code == 400
