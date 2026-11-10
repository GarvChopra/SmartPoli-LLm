import pytest
from fastapi.testclient import TestClient

from r2.brain import FakeBrain
from r2.config import Settings
from r2.privacy import EgressLedger
from r2.runtime import build_runtime
from r2.server import create_app
from r2.skills import SkillStore
from r2.tools import ToolRegistry
from r2.types import Decision


@pytest.fixture
def rt(tmp_path):
    settings = Settings(data_dir=tmp_path, db_path=tmp_path / "t.db")
    return build_runtime(settings, brain=FakeBrain([Decision(final="hi")]), registry=ToolRegistry())


@pytest.fixture
def client(rt):
    return TestClient(create_app(rt))


@pytest.fixture
def authed(rt, client):
    client.headers["Authorization"] = "Bearer " + rt.auth.redeem(rt.auth.new_pairing_code())
    return client


@pytest.mark.parametrize(
    "method,path",
    [("get", "/api/ledger"), ("get", "/api/skills"), ("post", "/api/skills/x/approve"), ("delete", "/api/skills/x/proposed")],
)
def test_the_new_routes_require_a_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_ledger_endpoint_summarises_requests_and_routing(rt, authed, tmp_path):
    assert authed.get("/api/ledger").json()["summary"]["requests"] == 0
    led = EgressLedger(tmp_path / "egress.jsonl")
    led.append({"host": "api.tokenfactory.nebius.com", "model": "m", "messages": 2, "bytes": 90, "masked": {"EMAIL": 1}, "preview": "p", "outcome": "HTTP 200"})
    led.append({"route": "local", "model": "qwen", "reason": "sensitive data found (EMAIL)", "left_pc": False, "fallback": False})
    data = authed.get("/api/ledger").json()
    assert data["summary"] == {"requests": 1, "bytes": 90, "masked": {"EMAIL": 1}}
    assert (data["routed"], data["stayed_local"]) == (1, 1) and len(data["recent"]) == 2


def test_skills_endpoints_list_approve_and_reject(rt, authed, tmp_path):
    store = SkillStore(tmp_path, bundled=None)
    store.propose("pack-bag", "Make a packing list", "List the items.")
    store.propose("bad-idea", "Nope", "Do nothing.")
    listing = authed.get("/api/skills").json()
    assert [p["name"] for p in listing["proposed"]] == ["bad-idea", "pack-bag"]
    assert "tidy-downloads" in [s["name"] for s in listing["skills"]]  # bundled ones are listed
    assert authed.post("/api/skills/pack-bag/approve").status_code == 200
    assert SkillStore(tmp_path, bundled=None).get("pack-bag") is not None
    assert authed.delete("/api/skills/bad-idea/proposed").status_code == 200
    assert authed.delete("/api/skills/bad-idea/proposed").status_code == 404
    assert authed.post("/api/skills/pack-bag/approve").status_code == 400  # already active
    assert authed.post("/api/skills/..%2Fx/approve").status_code in (400, 404)
    assert any(a["tool"] == "approve_skill" for a in rt.memory.list_actions(10))
