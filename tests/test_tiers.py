import pytest

from r2.brain import BrainError, FakeBrain
from r2.privacy import EgressLedger, format_ledger
from r2.tiers import DEEP_AFTER_TOOL_CALLS, TieredBrain, classify
from r2.types import Decision


def user(text, tools=0):
    return [{"role": "user", "content": text}] + [{"role": "tool", "content": "x"}] * tools


def test_a_normal_task_goes_to_the_main_model():
    assert classify(user("open notepad"), True, True, True).tier == "act"


@pytest.mark.parametrize("text", ["plan my week", "Compare these two files", "analyze this report", "write a summary of it"])
def test_planning_and_analysis_escalate_to_the_biggest_model(text):
    assert classify(user(text), True, True, True).tier == "deep"


def test_long_requests_and_long_tasks_escalate():
    assert classify(user("x " * 300), True, True, True).tier == "deep"
    assert classify(user("open it", tools=DEEP_AFTER_TOOL_CALLS), True, True, True).tier == "deep"
    assert classify(user("open it", tools=DEEP_AFTER_TOOL_CALLS - 1), True, True, True).tier == "act"


def test_without_a_deep_model_nothing_escalates():
    assert classify(user("plan my week"), True, True, False).tier == "act"


def test_private_requests_stay_local_but_only_if_asked_and_available():
    text = "email boss@corp.com my password is hunter2"
    choice = classify(user(text), True, True, True)
    assert choice.tier == "local" and "EMAIL" in choice.reason and "hunter2" not in choice.reason
    assert classify(user(text), False, True, True).tier != "local"  # switched off
    assert classify(user(text), True, False, True).tier != "local"  # no local model running


def test_private_wins_over_deep():
    assert classify(user("plan how to use password is abc123"), True, True, True).tier == "local"


def brains():
    def make(text):
        return FakeBrain([Decision(final=text)] * 5)
    return make("act"), make("deep"), make("local")


def test_routing_calls_the_right_brain_and_logs_why(tmp_path):
    act, deep, local = brains()
    ledger = EgressLedger(tmp_path / "e.jsonl")
    tb = TieredBrain(act, deep, local, True, ledger, {"act": "super", "deep": "ultra", "local": "qwen"})
    assert tb.decide(user("open notepad"), []).final == "act"
    assert tb.decide(user("plan my trip"), []).final == "deep"
    assert tb.decide(user("my card 4111 1111 1111 1111"), []).final == "local"
    rows = ledger.read()
    assert [r["route"] for r in rows] == ["act", "deep", "local"]
    assert [r["left_pc"] for r in rows] == [True, True, False]
    assert "1 stayed on this PC" in format_ledger(ledger)


class Failing:
    def decide(self, *a, **k):
        raise BrainError("rate limited")


def test_a_failed_hosted_model_falls_back_to_the_local_one(tmp_path):
    ledger = EgressLedger(tmp_path / "e.jsonl")
    tb = TieredBrain(Failing(), None, FakeBrain([Decision(final="local ok")]), True, ledger)
    assert tb.decide(user("open notepad"), []).final == "local ok"
    assert ledger.read()[-1]["fallback"] is True


def test_without_a_local_model_the_error_surfaces():
    with pytest.raises(BrainError):
        TieredBrain(Failing()).decide(user("open notepad"), [])


def test_a_private_request_is_never_sent_out_when_the_local_model_fails():
    tb = TieredBrain(FakeBrain([Decision(final="SHOULD NOT BE USED")]), None, Failing(), True)
    with pytest.raises(BrainError) as exc:
        tb.decide(user("my password is hunter2"), [])
    assert "not sent to a hosted model" in str(exc.value)


def test_only_the_current_turn_decides_privacy_not_old_history_or_the_system_prompt():
    history = [
        {"role": "system", "content": "Known facts: call 98765 43210"},
        {"role": "user", "content": "my card is 4111 1111 1111 1111"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "open notepad"},
    ]
    assert classify(history, True, True, True).tier == "act"  # the old secret is masked, not pinned local
    with_tool_result = history + [{"role": "tool", "content": "file says password is hunter2"}]
    assert classify(with_tool_result, True, True, True).tier == "local"  # a secret just read by a tool is private
