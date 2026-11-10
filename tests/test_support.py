from fastapi.testclient import TestClient

from r2.support import create_app


def test_freeze_then_replace_and_guardrails():
    c = TestClient(create_app())
    assert c.post("/actions/order_replacement", json={"last4": "4242"}).status_code == 409
    r = c.post("/actions/freeze_card", json={"last4": "4242", "reason": "stolen"})
    assert r.json()["status"] == "frozen"
    r = c.post("/actions/order_replacement", json={"last4": "4242", "speed": "expedited"})
    assert r.json()["fee_usd"] == 15
    assert c.post("/actions/freeze_card", json={"last4": "4242424242424242"}).status_code == 422
    assert c.post("/actions/freeze_card", json={"last4": "9999"}).status_code == 404


def test_chat_end_to_end():
    c = TestClient(create_app())
    sid = c.post("/chat/start", json={"name": "Sam", "topic": "lost_card", "urgent": True}).json()["session"]
    say = lambda m: c.post("/chat", json={"session": sid, "message": m}).json()["reply"]
    say("stolen")
    assert "frozen" in say("4242")
    assert "Replacement ordered" in say("expedited")
    assert "1%" in say("what is the foreign transaction fee")
    assert "can't" in say("should I buy tesla stock")
    assert "callback" in say("let me talk to a human")


def test_large_dispute_escalates_and_auth():
    c = TestClient(create_app("k"))
    body = {"last4": "1111", "amount_usd": 900, "merchant": "X"}
    assert c.post("/actions/open_dispute", json=body).status_code == 401
    r = c.post("/actions/open_dispute", json=body, headers={"X-API-Key": "k"})
    assert r.json()["escalate"] is True
