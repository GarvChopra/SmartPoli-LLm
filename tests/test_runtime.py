import threading
import time

import pytest

from r2.agent import Agent
from r2.auth import AuthStore
from r2.brain import FakeBrain
from r2.memory import Memory
from r2.runtime import ApprovalBroker, TaskBusy, TaskManager
from r2.tools import ToolRegistry, ToolSpec
from r2.types import Decision, Risk, RiskInfo, ToolCall

NO_ARGS = {"type": "object", "properties": {}}


def call(name, **args):
    return Decision(tool_call=ToolCall(name, args))


def fin(text):
    return Decision(final=text)


def wait_until(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------- ApprovalBroker ----------
def ask_in_thread(broker, name="submit_form", **args):
    box = {}

    def run():
        box["answer"] = broker.request(ToolCall(name, args))

    t = threading.Thread(target=run)
    t.start()
    return t, box


def test_broker_approve():
    broker = ApprovalBroker(timeout=5)
    t, box = ask_in_thread(broker, text="hi")
    assert wait_until(lambda: broker.pending() is not None)
    pending = broker.pending()
    assert pending["tool"] == "submit_form" and pending["args"] == {"text": "hi"}
    assert broker.resolve(pending["id"], True) is True
    t.join(2)
    assert box["answer"] is True
    assert broker.pending() is None


def test_broker_deny():
    broker = ApprovalBroker(timeout=5)
    t, box = ask_in_thread(broker)
    assert wait_until(lambda: broker.pending() is not None)
    broker.resolve(broker.pending()["id"], False)
    t.join(2)
    assert box["answer"] is False


def test_broker_times_out_as_denied():
    broker = ApprovalBroker(timeout=0.1)
    assert broker.request(ToolCall("x", {})) is False
    assert broker.pending() is None


def test_broker_rejects_unknown_or_stale_ids():
    broker = ApprovalBroker(timeout=5)
    assert broker.resolve("nope", True) is False
    t, box = ask_in_thread(broker)
    assert wait_until(lambda: broker.pending() is not None)
    old_id = broker.pending()["id"]
    broker.resolve(old_id, True)
    t.join(2)
    assert broker.resolve(old_id, True) is False  # already used


def test_broker_cancel_denies_waiting_request():
    broker = ApprovalBroker(timeout=5)
    t, box = ask_in_thread(broker)
    assert wait_until(lambda: broker.pending() is not None)
    broker.cancel()
    t.join(2)
    assert box["answer"] is False


def test_broker_truncates_long_arguments_for_display():
    broker = ApprovalBroker(timeout=5)
    t, _ = ask_in_thread(broker, text="x" * 1000)
    assert wait_until(lambda: broker.pending() is not None)
    assert len(broker.pending()["args"]["text"]) < 400
    broker.cancel()
    t.join(2)


# ---------- TaskManager ----------
@pytest.fixture
def parts(tmp_path):
    mem = Memory(tmp_path / "t.db")
    reg = ToolRegistry()
    ran = []
    reg.register(ToolSpec("echo", "Echo", NO_ARGS, lambda: "echoed", RiskInfo(Risk.SAFE)))
    reg.register(ToolSpec("slow", "Slow", NO_ARGS, lambda: time.sleep(0.2) or "slow", RiskInfo(Risk.SAFE)))
    reg.register(
        ToolSpec("submit_form", "Submit", NO_ARGS, lambda: ran.append(1) or "submitted", RiskInfo(Risk.CONFIRM, "submit"))
    )
    broker = ApprovalBroker(timeout=5)
    return mem, reg, broker, ran


def make_manager(parts, script):
    mem, reg, broker, _ = parts
    agent = Agent(FakeBrain(script), reg, mem, broker.request)
    return TaskManager(agent, broker)


def test_task_runs_to_completion(parts):
    m = make_manager(parts, [call("echo"), fin("all done")])
    task_id = m.start("do it")
    assert m.wait(3)
    st = m.status()
    assert st["running"] is False and st["task_id"] == task_id
    assert st["result"] == {"status": "done", "answer": "all done", "steps": 1}
    assert st["message"] == "do it"


def test_only_one_task_at_a_time(parts):
    m = make_manager(parts, [call("slow"), fin("done")])
    m.start("first")
    with pytest.raises(TaskBusy):
        m.start("second")
    assert m.wait(3)


def test_new_task_allowed_after_previous_finishes(parts):
    m = make_manager(parts, [fin("one"), fin("two")])
    m.start("a")
    m.wait(3)
    m.start("b")
    m.wait(3)
    assert m.status()["result"]["answer"] == "two"


def test_approval_flow_through_manager(parts):
    mem, reg, broker, ran = parts
    m = make_manager(parts, [call("submit_form"), fin("posted")])
    m.start("fill the form")
    assert wait_until(lambda: m.status()["pending"] is not None)
    assert ran == []
    broker.resolve(m.status()["pending"]["id"], True)
    assert m.wait(3)
    assert ran == [1]
    assert m.status()["result"]["status"] == "done"


def test_stop_cancels_pending_approval_and_ends_task(parts):
    mem, reg, broker, ran = parts
    m = make_manager(parts, [call("submit_form"), call("echo"), fin("never")])
    m.start("fill the form")
    assert wait_until(lambda: m.status()["pending"] is not None)
    m.stop()
    assert m.wait(3)
    assert ran == []
    assert m.status()["result"]["status"] == "stopped"


def test_agent_crash_becomes_failed_result(parts):
    mem, reg, broker, _ = parts

    class Exploding:
        def decide(self, messages, tools, **kwargs):
            raise RuntimeError("kaboom")

    m = TaskManager(Agent(Exploding(), reg, mem, broker.request), broker)
    m.start("x")
    assert m.wait(3)
    result = m.status()["result"]
    assert result["status"] == "failed" and "kaboom" in result["answer"]


def test_paused_manager_refuses_new_tasks(parts):
    m = make_manager(parts, [fin("x")])
    m.paused = True
    with pytest.raises(TaskBusy, match="paused"):
        m.start("hi")


# ---------- AuthStore ----------
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def auth(tmp_path):
    clock = Clock()
    return AuthStore(tmp_path / "tokens.json", clock=clock), clock


def test_pairing_code_redeems_once_for_a_token(auth):
    store, _ = auth
    code = store.new_pairing_code()
    assert len(code) == 8
    token = store.redeem(code)
    assert token and store.verify(token)
    assert store.redeem(code) is None  # single use


def test_pairing_code_expires_after_ten_minutes(auth):
    store, clock = auth
    code = store.new_pairing_code()
    clock.now += 601
    assert store.redeem(code) is None


def test_code_matching_ignores_case_and_spaces(auth):
    store, _ = auth
    code = store.new_pairing_code()
    assert store.redeem(f" {code.lower()} ") is not None


def test_wrong_codes_lock_out_after_ten_attempts(auth):
    store, clock = auth
    good = store.new_pairing_code()
    for _ in range(10):
        assert store.redeem("WRONG123") is None
    assert store.redeem(good) is None  # locked out even with the right code
    clock.now += 61
    assert store.redeem(good) is not None


def test_verify_rejects_garbage_and_empty(auth):
    store, _ = auth
    store.redeem(store.new_pairing_code())
    assert not store.verify("")
    assert not store.verify("not-a-token")
    assert not store.verify(None)


def test_tokens_persist_only_as_hashes(auth, tmp_path):
    store, _ = auth
    token = store.redeem(store.new_pairing_code())
    raw = (tmp_path / "tokens.json").read_text()
    assert token not in raw
    assert AuthStore(tmp_path / "tokens.json").verify(token)


def test_revoke_all_invalidates_tokens(auth):
    store, _ = auth
    token = store.redeem(store.new_pairing_code())
    store.revoke_all()
    assert not store.verify(token)


def test_code_created_by_another_process_can_be_redeemed(tmp_path):
    cli_side = AuthStore(tmp_path / "tokens.json")
    server_side = AuthStore(tmp_path / "tokens.json")
    code = cli_side.new_pairing_code()
    token = server_side.redeem(code)
    assert token and cli_side.verify(token)
    assert server_side.redeem(code) is None
    assert code not in (tmp_path / "tokens.json").read_text()


# ---------- streaming partial text, and wiring ----------
def test_status_shows_partial_text_while_the_model_is_still_talking(parts):
    mem, reg, broker, _ = parts
    release = threading.Event()

    class Talkative:
        def decide(self, messages, tools, on_token=None, should_stop=None):
            on_token("Hel")
            on_token("lo")
            release.wait(3)
            return Decision(final="Hello")

    m = TaskManager(Agent(Talkative(), reg, mem, broker.request), broker)
    m.start("hi")
    assert wait_until(lambda: m.status()["partial"] == "Hello")
    assert m.status()["running"] is True
    release.set()
    assert m.wait(3)
    assert m.status()["partial"] == ""
    assert m.status()["result"]["answer"] == "Hello"


def test_partial_resets_for_each_new_task(parts):
    m = make_manager(parts, [fin("one"), fin("two")])
    m.start("a")
    m.wait(3)
    m.start("b")
    m.wait(3)
    assert m.status()["partial"] == ""


def test_build_runtime_uses_fast_chat_routing_only_with_the_real_brain(tmp_path):
    from r2.brain import OllamaBrain
    from r2.config import Settings
    from r2.runtime import build_runtime

    settings = Settings(data_dir=tmp_path, db_path=tmp_path / "t.db", model="qwen2.5:7b", chat_model="qwen2.5:3b")
    real = build_runtime(settings)
    assert real.tasks.agent.smart_routing is True
    assert isinstance(real.tasks.agent.chat_brain, OllamaBrain)
    real.registry.close()
    injected = build_runtime(settings, brain=FakeBrain([fin("x")]), registry=ToolRegistry())
    assert injected.tasks.agent.smart_routing is False and injected.tasks.agent.chat_brain is None
