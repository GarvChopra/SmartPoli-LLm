import time

from fastapi.testclient import TestClient

from r2.auth import AuthStore
from r2.brain import FakeBrain
from r2.config import Settings
from r2.runtime import build_runtime
from r2.server import create_app
from r2.tools import ToolRegistry
from r2.types import Decision


def test_a_fixed_access_code_works_repeatedly_and_wrong_codes_do_not(tmp_path):
    auth = AuthStore(tmp_path / "t.json", static_code="Judge-2026")
    first, second = auth.redeem("judge-2026"), auth.redeem("JUDGE-2026")
    assert first and second and first != second and auth.verify(first) and auth.verify(second)
    assert auth.redeem("nope") is None


def test_without_a_fixed_code_nothing_changes(tmp_path):
    auth = AuthStore(tmp_path / "t.json")
    assert auth.redeem("") is None and auth.redeem("anything") is None


def demo_client(tmp_path, profile, limit=2, code="OPEN-SESAME"):
    settings = Settings(
        data_dir=tmp_path, db_path=tmp_path / "t.db", profile=profile, demo_code=code, demo_tasks_per_hour=limit
    )
    rt = build_runtime(settings, brain=FakeBrain([Decision(final="ok")] * 20), registry=ToolRegistry())
    return TestClient(create_app(rt)), rt


def test_demo_profile_honours_the_fixed_code_and_rate_limits_tasks(tmp_path):
    client, rt = demo_client(tmp_path, "demo")
    token = client.post("/api/pair", json={"code": "open-sesame"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    codes = []
    for _ in range(3):
        codes.append(client.post("/api/tasks", json={"message": "hello"}, headers=headers).status_code)
        rt.tasks.wait(3)
        time.sleep(0.05)
    assert codes == [202, 202, 429]
    assert "rate limited" in client.post("/api/tasks", json={"message": "hi"}, headers=headers).json()["detail"]


def test_outside_the_demo_profile_the_fixed_code_is_ignored_and_there_is_no_limit(tmp_path):
    client, rt = demo_client(tmp_path, "")
    assert client.post("/api/pair", json={"code": "OPEN-SESAME"}).status_code == 401
    token = rt.auth.redeem(rt.auth.new_pairing_code())
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(4):
        assert client.post("/api/tasks", json={"message": "hello"}, headers=headers).status_code == 202
        rt.tasks.wait(3)


def test_the_demo_profile_flags_itself_and_never_serves_the_screen(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("r2.tools.screen.screenshot", lambda path: called.append(path))
    client, rt = demo_client(tmp_path, "demo")
    headers = {"Authorization": "Bearer " + client.post("/api/pair", json={"code": "OPEN-SESAME"}).json()["token"]}
    assert client.get("/api/status", headers=headers).json()["demo"] is True
    assert client.get("/api/screenshot", headers=headers).status_code == 403 and called == []

    normal, nrt = demo_client(tmp_path / "n", "")
    nheaders = {"Authorization": "Bearer " + nrt.auth.redeem(nrt.auth.new_pairing_code())}
    assert normal.get("/api/status", headers=nheaders).json()["demo"] is False


def test_revoke_all_is_disabled_in_the_demo_so_one_visitor_cannot_sign_out_the_rest(tmp_path):
    client, rt = demo_client(tmp_path, "demo")
    first = client.post("/api/pair", json={"code": "OPEN-SESAME"}).json()["token"]
    second = client.post("/api/pair", json={"code": "OPEN-SESAME"}).json()["token"]
    assert client.post("/api/revoke", headers={"Authorization": f"Bearer {first}"}).status_code == 403
    assert client.get("/api/status", headers={"Authorization": f"Bearer {second}"}).status_code == 200


def test_demo_tokens_are_capped_so_the_file_cannot_grow_without_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("r2.auth.MAX_DEMO_TOKENS", 3)
    auth = AuthStore(tmp_path / "t.json", static_code="OPEN")
    tokens = [auth.redeem("open") for _ in range(5)]
    assert auth.count() == 3
    assert not auth.verify(tokens[0]) and auth.verify(tokens[-1])
