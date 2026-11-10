import json

import httpx

from r2.brain import OpenAIBrain
from r2.privacy import EgressLedger, Gateway, format_ledger


def masked(text):
    state = Gateway().prepare([{"role": "user", "content": text}])
    return state.messages[0]["content"], state


def test_common_sensitive_values_are_masked():
    text = ("mail me at asha.k@example.com, call 98765 43210, card 4111 1111 1111 1111, "
            "PAN ABCDE1234F, Aadhaar 2345 6789 0123, password is hunter2, key nvapi-abcdefghijklmnopqrstuv")
    out, state = masked(text)
    for secret in ("asha.k@example.com", "98765 43210", "4111", "ABCDE1234F", "2345 6789", "hunter2", "nvapi-abcdef"):
        assert secret not in out
    assert set(state.counts) >= {"EMAIL", "PHONE", "CARD", "PAN", "AADHAAR", "PASSWORD", "API_KEY"}


def test_ordinary_text_and_non_card_numbers_are_left_alone():
    text = "Order 1234567890123 is a made-up number; meet at 5 pm, room 12."
    out, state = masked(text)
    assert out == text and state.counts == {}


def test_same_value_gets_the_same_placeholder_and_replies_are_restored_locally():
    out, state = masked("email a@b.com then email a@b.com again, cc c@d.org")
    assert out.count("[EMAIL_1]") == 2 and "[EMAIL_2]" in out
    assert Gateway.restore("Sent to [EMAIL_1] and [EMAIL_2]", state) == "Sent to a@b.com and c@d.org"
    assert Gateway.restore({"to": ["[EMAIL_1]"], "n": 3}, state) == {"to": ["a@b.com"], "n": 3}
    assert Gateway.restore("unknown [EMAIL_9]", state) == "unknown [EMAIL_9]"


def test_the_ledger_records_what_was_sent_but_never_the_original_values(tmp_path):
    ledger = EgressLedger(tmp_path / "egress.jsonl")
    state = Gateway(ledger).prepare([{"role": "user", "content": "my email is a@b.com"}])
    Gateway(ledger).record(state, "https://api.tokenfactory.nebius.com/v1", "nvidia/nemotron", 120, "HTTP 200")
    raw = (tmp_path / "egress.jsonl").read_text(encoding="utf-8")
    assert "a@b.com" not in raw and "[EMAIL_1]" in raw
    row = ledger.read()[0]
    assert row["host"] == "api.tokenfactory.nebius.com" and row["masked"] == {"EMAIL": 1}
    assert ledger.summary() == {"requests": 1, "bytes": 120, "masked": {"EMAIL": 1}}
    assert "1 EMAIL" in format_ledger(ledger)


def test_empty_ledger_says_so(tmp_path):
    assert "Nothing has been sent" in format_ledger(EgressLedger(tmp_path / "none.jsonl"))


def test_the_cloud_brain_sends_only_masked_text_logs_it_and_restores_the_answer(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode("utf-8")
        reply = {"choices": [{"message": {"content": "Done, I wrote to [EMAIL_1]."}}]}
        return httpx.Response(200, json=reply)

    ledger = EgressLedger(tmp_path / "egress.jsonl")
    brain = OpenAIBrain("https://api.tokenfactory.nebius.com/v1", "k", "nvidia/nemotron",
                        client=httpx.Client(transport=httpx.MockTransport(handler)), gateway=Gateway(ledger))
    decision = brain.decide([{"role": "user", "content": "write to boss@corp.com"}], [])
    assert "boss@corp.com" not in seen["body"] and "[EMAIL_1]" in seen["body"]
    assert decision.final == "Done, I wrote to boss@corp.com."
    assert ledger.read()[0]["outcome"] == "HTTP 200"


def test_a_failed_request_is_still_logged(tmp_path):
    def handler(request):
        return httpx.Response(500, json={"error": {"message": "boom"}})

    ledger = EgressLedger(tmp_path / "egress.jsonl")
    brain = OpenAIBrain("https://h/v1", "k", "m", client=httpx.Client(transport=httpx.MockTransport(handler)),
                        gateway=Gateway(ledger))
    try:
        brain.decide([{"role": "user", "content": "hi"}], [])
    except Exception:
        pass
    assert ledger.read()[0]["outcome"] == "HTTP 500"
