"""End to end through the real code path (settings -> make_brain -> tiers -> gateway -> HTTP -> ledger),
with a local stand-in for the Nebius Token Factory API instead of the real service."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from r2.config import Settings
from r2.privacy import EgressLedger
from r2.runtime import build_runtime
from r2.server import create_app

SUPER, DEEP, NANO = "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3-nano-30b-a3b"


@pytest.fixture
def fake_token_factory():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"auth": self.headers.get("Authorization"), "body": body})
            reply = json.dumps({"choices": [{"message": {"content": f"answered by {body['model']}"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1", seen
    server.shutdown()


def finish(client, headers, text):
    assert client.post("/api/tasks", json={"message": text}, headers=headers).status_code == 202
    end = time.time() + 5
    while time.time() < end:
        st = client.get("/api/status", headers=headers).json()
        if not st["running"] and st["result"]:
            return st["result"]["answer"]
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def test_demo_build_talks_to_token_factory_with_tiers_masking_and_a_ledger(tmp_path, monkeypatch, fake_token_factory):
    base, seen = fake_token_factory
    monkeypatch.setattr("r2.brain.ollama_up", lambda url: False)  # no local model: secrets are masked and sent
    settings = Settings(
        data_dir=tmp_path, db_path=tmp_path / "t.db", brain="openai", openai_base_url=base,
        openai_api_key="nb-secret-key", openai_model=SUPER, deep_model=DEEP, fast_model=NANO,
        chat_model="", profile="demo", demo_code="JUDGE-ACCESS", claude=False,
    )
    rt = build_runtime(settings)
    client = TestClient(create_app(rt))
    token = client.post("/api/pair", json={"code": "judge-access"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # the demo build exposes only the safe tools
    assert set(rt.registry.names()) == {"remember", "use_skill", "propose_skill", "web_search", "web_fetch"}

    # plain chat goes to the small model; the phone number is masked on the way out and restored in the reply
    assert finish(client, headers, "hello, my number is 98765 43210") == f"answered by {NANO}"
    # a request that needs tools and asks for analysis escalates to the biggest model
    assert finish(client, headers, "search the web and analyze LLM pricing") == f"answered by {DEEP}"
    # an ordinary tool task uses the main model
    assert finish(client, headers, "search the web for cats") == f"answered by {SUPER}"

    assert [r["body"]["model"] for r in seen] == [NANO, DEEP, SUPER]
    assert all(r["auth"] == "Bearer nb-secret-key" for r in seen)
    assert "98765" not in json.dumps(seen[0]["body"]) and "[PHONE_1]" in json.dumps(seen[0]["body"])

    ledger = client.get("/api/ledger", headers=headers).json()
    # the first message stays in the chat history, so it is masked again in each later request
    assert ledger["summary"]["requests"] == 3 and ledger["summary"]["masked"] == {"PHONE": 3}
    assert all("98765" not in json.dumps(r["body"]) for r in seen)
    assert any(r.get("route") == "deep" for r in EgressLedger(tmp_path / "egress.jsonl").read())
    assert "nb-secret-key" not in (tmp_path / "egress.jsonl").read_text(encoding="utf-8")


def test_with_a_local_model_running_a_message_with_a_secret_never_reaches_the_hosted_api(tmp_path, monkeypatch, fake_token_factory):
    from r2.types import Decision

    base, seen = fake_token_factory
    monkeypatch.setattr("r2.brain.ollama_up", lambda url: True)
    monkeypatch.setattr("r2.brain.OllamaBrain.decide", lambda self, *a, **k: Decision(final="answered on this PC"))
    settings = Settings(
        data_dir=tmp_path, db_path=tmp_path / "t.db", brain="openai", openai_base_url=base,
        openai_api_key="nb-secret-key", openai_model=SUPER, deep_model=DEEP, fast_model=NANO,
        chat_model="qwen2.5:1.5b", profile="demo", demo_code="JUDGE-ACCESS", claude=False,
    )
    rt = build_runtime(settings)
    client = TestClient(create_app(rt))
    headers = {"Authorization": "Bearer " + client.post("/api/pair", json={"code": "judge-access"}).json()["token"]}

    # chat path and tool path both keep the secret on this PC
    assert finish(client, headers, "hello, my number is 98765 43210") == "answered on this PC"
    assert finish(client, headers, "search the web, my password is hunter2") == "answered on this PC"
    assert seen == []  # nothing reached the hosted API

    ledger = client.get("/api/ledger", headers=headers).json()
    assert ledger["summary"]["requests"] == 0 and ledger["stayed_local"] == ledger["routed"] >= 2

    # a request without a secret still goes to the hosted model, even though the local one is running
    assert finish(client, headers, "search the web for cats") == f"answered by {SUPER}"
    assert [r["body"]["model"] for r in seen] == [SUPER]
