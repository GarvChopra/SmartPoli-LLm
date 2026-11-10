import time

import pytest
from fastapi.testclient import TestClient

from r2.brain import FakeBrain
from r2.config import Settings
from r2.runtime import build_runtime
from r2.server import create_app
from r2.tools import ToolRegistry, ToolSpec
from r2.tools import screen as screen_tools
from r2.types import Decision, Risk, RiskInfo, ToolCall

NO_ARGS = {"type": "object", "properties": {}}


def call(name, **args):
    return Decision(tool_call=ToolCall(name, args))


def fin(text):
    return Decision(final=text)


def make_runtime(tmp_path, script):
    reg = ToolRegistry()
    reg.register(ToolSpec("echo", "Echo", NO_ARGS, lambda: "echoed", RiskInfo(Risk.SAFE)))
    reg.register(ToolSpec("slow", "Slow", NO_ARGS, lambda: time.sleep(0.3) or "slow", RiskInfo(Risk.SAFE)))
    reg.register(
        ToolSpec("submit_form", "Submit", NO_ARGS, lambda: "submitted", RiskInfo(Risk.CONFIRM, "submit"))
    )
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / "t.db")
    return build_runtime(settings, brain=FakeBrain(script), registry=reg)


@pytest.fixture
def rt(tmp_path):
    return make_runtime(tmp_path, [fin("hello")])


@pytest.fixture
def client(rt):
    return TestClient(create_app(rt))


@pytest.fixture
def authed(rt, client):
    token = rt.auth.redeem(rt.auth.new_pairing_code())
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def wait_until(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------- page and headers ----------
def test_index_page_is_served_without_a_token(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "R2" in r.text and "text/html" in r.headers["content-type"]


def test_responses_carry_security_headers(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_favicon_request_does_not_404(client):
    assert client.get("/favicon.ico").status_code == 204


def test_api_docs_are_not_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ---------- auth ----------
@pytest.mark.parametrize(
    "method,path",
    [("get", "/api/status"), ("post", "/api/tasks"), ("post", "/api/approve"), ("post", "/api/stop"),
     ("get", "/api/screenshot"), ("get", "/api/facts"), ("get", "/api/actions"), ("post", "/api/revoke")],
)
def test_every_api_route_requires_a_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_bad_token_and_query_param_token_are_rejected(rt, client):
    token = rt.auth.redeem(rt.auth.new_pairing_code())
    assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(f"/api/status?token={token}").status_code == 401


def test_pairing_flow(rt, client):
    assert client.post("/api/pair", json={"code": "WRONGCODE"}).status_code == 401
    code = rt.auth.new_pairing_code()
    r = client.post("/api/pair", json={"code": code})
    assert r.status_code == 200
    token = r.json()["token"]
    assert client.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/api/pair", json={"code": code}).status_code == 401  # single use


def test_revoke_logs_out_every_device(authed):
    assert authed.post("/api/revoke").status_code == 200
    assert authed.get("/api/status").status_code == 401


# ---------- tasks ----------
def test_status_when_idle(authed):
    st = authed.get("/api/status").json()
    assert st["running"] is False and st["result"] is None and st["pending"] is None


def test_task_runs_and_reports_result(authed, rt):
    r = authed.post("/api/tasks", json={"message": "say hello"})
    assert r.status_code == 202 and r.json()["task_id"]
    assert rt.tasks.wait(3)
    assert authed.get("/api/status").json()["result"]["answer"] == "hello"


@pytest.mark.parametrize("body", [{}, {"message": ""}, {"message": "   "}, {"message": "x" * 4001}, {"message": 5}])
def test_task_input_is_validated(authed, body):
    assert authed.post("/api/tasks", json=body).status_code == 422


def test_second_task_while_running_is_409(tmp_path):
    rt = make_runtime(tmp_path, [call("slow"), fin("done")])
    c = TestClient(create_app(rt))
    c.headers["Authorization"] = "Bearer " + rt.auth.redeem(rt.auth.new_pairing_code())
    assert c.post("/api/tasks", json={"message": "a"}).status_code == 202
    assert c.post("/api/tasks", json={"message": "b"}).status_code == 409
    rt.tasks.wait(3)


def test_paused_server_refuses_tasks_with_a_clear_message(authed, rt):
    rt.tasks.paused = True
    r = authed.post("/api/tasks", json={"message": "hi"})
    assert r.status_code == 409 and "paused" in r.json()["detail"]


def test_approval_flow_over_http(tmp_path):
    rt = make_runtime(tmp_path, [call("submit_form"), fin("posted")])
    c = TestClient(create_app(rt))
    c.headers["Authorization"] = "Bearer " + rt.auth.redeem(rt.auth.new_pairing_code())
    c.post("/api/tasks", json={"message": "fill the form"})
    assert wait_until(lambda: c.get("/api/status").json()["pending"] is not None)
    pending = c.get("/api/status").json()["pending"]
    assert pending["tool"] == "submit_form"
    assert c.post("/api/approve", json={"id": "wrong", "approve": True}).status_code == 404
    assert c.post("/api/approve", json={"id": pending["id"], "approve": True}).status_code == 200
    assert rt.tasks.wait(3)
    assert c.get("/api/status").json()["result"]["status"] == "done"
    assert [a["approval"] for a in c.get("/api/actions").json()][0] == "approved"


def test_stop_ends_a_waiting_task(tmp_path):
    rt = make_runtime(tmp_path, [call("submit_form"), call("echo"), fin("never")])
    c = TestClient(create_app(rt))
    c.headers["Authorization"] = "Bearer " + rt.auth.redeem(rt.auth.new_pairing_code())
    c.post("/api/tasks", json={"message": "fill the form"})
    assert wait_until(lambda: c.get("/api/status").json()["pending"] is not None)
    assert c.post("/api/stop").status_code == 200
    assert rt.tasks.wait(3)
    assert c.get("/api/status").json()["result"]["status"] == "stopped"


# ---------- memory views ----------
def test_facts_list_and_delete(authed, rt):
    fid = rt.memory.save_fact("my name is Garv", "user")
    facts = authed.get("/api/facts").json()
    assert facts[0]["text"] == "my name is Garv"
    assert authed.delete(f"/api/facts/{fid}").status_code == 200
    assert authed.delete(f"/api/facts/{fid}").status_code == 404
    assert authed.get("/api/facts").json() == []


# ---------- screenshot ----------
def test_screenshot_returns_png(authed, monkeypatch):
    def fake(path):
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return "ok"

    monkeypatch.setattr(screen_tools, "screenshot", fake)
    r = authed.get("/api/screenshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


def test_screenshot_failure_is_503(authed, monkeypatch):
    def boom(path):
        raise RuntimeError("no display")

    monkeypatch.setattr(screen_tools, "screenshot", boom)
    assert authed.get("/api/screenshot").status_code == 503


def test_chat_history_endpoint(authed, rt, client):
    rt.memory.add_message("user", "hello")
    rt.memory.add_message("assistant", "hi there")
    assert authed.get("/api/chat").json() == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    client.headers.pop("Authorization")
    assert client.get("/api/chat").status_code == 401


def test_clear_history_endpoint_keeps_facts(authed, rt):
    rt.memory.add_message("user", "hello")
    rt.memory.save_fact("my name is Garv", "user")
    assert authed.delete("/api/history").status_code == 200
    assert authed.get("/api/chat").json() == []
    assert authed.get("/api/facts").json()[0]["text"] == "my name is Garv"
