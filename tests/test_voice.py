import threading
import time

import pytest

from r2.agent import Agent
from r2.brain import FakeBrain
from r2.memory import Memory
from r2.runtime import ApprovalBroker, TaskManager
from r2.tools import ToolRegistry, ToolSpec
from r2.types import Decision, Risk, RiskInfo, ToolCall
from r2.voice import (
    VoiceAssistant,
    collect_utterance,
    is_stop_command,
    run_task_and_wait,
    speakable,
)

NO_ARGS = {"type": "object", "properties": {}}


# ---------- pure helpers ----------
@pytest.mark.parametrize("text", ["stop", "Stop.", "cancel", "never mind", "Nevermind!", "stop that", "abort"])
def test_stop_commands(text):
    assert is_stop_command(text)


@pytest.mark.parametrize("text", ["open notepad", "don't stop the music", "what is a bus stop", ""])
def test_non_stop_commands(text):
    assert not is_stop_command(text)


def test_speakable_strips_markup_and_shortens_at_a_sentence():
    assert speakable("**Done**: opened `notepad`.") == "Done: opened notepad."
    long = "First sentence. " + "word " * 200
    out = speakable(long, limit=100)
    assert out == "First sentence."
    assert len(speakable("x" * 500, limit=100)) <= 104
    assert speakable("   ") == ""


def frame(level, n=1280):
    np = pytest.importorskip("numpy")
    return np.full(n, level, dtype=np.int16)


def test_collect_utterance_returns_speech_after_noise_floor():
    frames = [frame(20)] * 4 + [frame(4000)] * 5 + [frame(20)] * 12 + [frame(4000)] * 3
    out = collect_utterance(iter(frames))
    assert out is not None
    # 5 loud + at least the 10 quiet frames that ended the utterance; later speech is not included
    assert 15 * 1280 <= len(out) <= 16 * 1280


def test_collect_utterance_gives_up_when_nobody_speaks():
    frames = [frame(20)] * 200
    assert collect_utterance(iter(frames)) is None


def test_collect_utterance_is_capped_in_length():
    frames = [frame(20)] * 4 + [frame(4000)] * 1000
    out = collect_utterance(iter(frames), max_s=2.0)
    assert len(out) <= 2.1 * 16000


def test_collect_utterance_with_too_few_frames_returns_none():
    assert collect_utterance(iter([frame(4000)] * 2)) is None


# ---------- assistant flow ----------
class Parts:
    def __init__(self, heard=True, text="open notepad", answer="Opened notepad."):
        self.heard, self.text, self.answer = heard, text, answer
        self.spoken, self.tasks, self.stops, self.wake_calls = [], [], 0, 0

    def wake(self):
        self.wake_calls += 1
        return self.heard

    def record(self):
        return b"audio"

    def transcribe(self, audio):
        assert audio == b"audio"
        return self.text

    def speak(self, text):
        self.spoken.append(text)

    def run_task(self, text):
        self.tasks.append(text)
        return self.answer

    def stop_task(self):
        self.stops += 1

    def build(self):
        return VoiceAssistant(self.wake, self.record, self.transcribe, self.speak, self.run_task, self.stop_task)


def test_happy_path():
    p = Parts()
    assert p.build().run_once() == "answered"
    assert p.tasks == ["open notepad"]
    assert p.spoken[-1] == "Opened notepad."
    assert len(p.spoken) == 2  # a short acknowledgement, then the answer


def test_muted_assistant_never_listens():
    p = Parts()
    va = p.build()
    va.muted = True
    assert va.run_once() == "muted"
    assert p.wake_calls == 0 and p.spoken == []


def test_no_wake_word_does_nothing():
    p = Parts(heard=False)
    assert p.build().run_once() == "no_wake"
    assert p.tasks == [] and p.spoken == []


def test_listen_now_skips_the_wake_word():
    p = Parts(heard=False)
    va = p.build()
    va.listen_now()
    assert va.run_once() == "answered"
    assert p.wake_calls == 0
    assert va.run_once() == "no_wake"  # the manual trigger is used up


def test_empty_transcript_asks_again():
    p = Parts(text="  ")
    assert p.build().run_once() == "empty"
    assert "didn't catch" in p.spoken[-1]
    assert p.tasks == []


def test_stop_word_stops_the_task_instead_of_starting_one():
    p = Parts(text="Stop.")
    assert p.build().run_once() == "stopped"
    assert p.stops == 1 and p.tasks == []


def test_component_failure_does_not_kill_the_loop():
    p = Parts()

    def boom(audio):
        raise RuntimeError("microphone unplugged")

    va = p.build()
    va.transcribe = boom
    assert va.run_once() == "error"
    assert "something went wrong" in p.spoken[-1].lower()
    va.transcribe = p.transcribe
    assert va.run_once() == "answered"


def test_failure_while_reporting_failure_is_swallowed():
    p = Parts()
    va = p.build()
    va.transcribe = lambda audio: 1 / 0
    va.speak = lambda text: (_ for _ in ()).throw(RuntimeError("tts broken"))
    assert va.run_once() == "error"


def test_run_forever_stops_when_asked():
    p = Parts(heard=False)
    va = p.build()
    done = threading.Event()
    t = threading.Thread(target=va.run_forever, args=(done,))
    t.start()
    time.sleep(0.1)
    done.set()
    t.join(2)
    assert not t.is_alive()


# ---------- voice never approves anything ----------
def test_voice_tasks_report_pending_approvals_but_cannot_grant_them(tmp_path):
    mem = Memory(tmp_path / "t.db")
    ran = []
    reg = ToolRegistry()
    reg.register(
        ToolSpec("submit_form", "Submit", NO_ARGS, lambda: ran.append(1) or "submitted", RiskInfo(Risk.CONFIRM, "submit"))
    )
    broker = ApprovalBroker(timeout=5)
    agent = Agent(
        FakeBrain([Decision(tool_call=ToolCall("submit_form", {})), Decision(final="posted")]),
        reg, mem, broker.request,
    )
    tasks = TaskManager(agent, broker)
    notices = []
    result = {}

    t = threading.Thread(
        target=lambda: result.update(answer=run_task_and_wait(tasks, "fill the form", notices.append, poll=0.02))
    )
    t.start()
    end = time.time() + 3
    while time.time() < end and not notices:
        time.sleep(0.02)
    assert len(notices) == 1 and "approval" in notices[0].lower() and "phone" in notices[0].lower()
    assert ran == [] and t.is_alive()  # still waiting: speech alone cannot approve
    broker.resolve(broker.pending()["id"], True)
    t.join(3)
    assert result["answer"] == "posted" and ran == [1]
    assert len(notices) == 1  # told once, not repeatedly


def test_run_task_and_wait_reports_busy_and_paused(tmp_path):
    mem = Memory(tmp_path / "t.db")
    broker = ApprovalBroker(timeout=5)
    tasks = TaskManager(Agent(FakeBrain([Decision(final="x")]), ToolRegistry(), mem, broker.request), broker)
    tasks.paused = True
    assert "paused" in run_task_and_wait(tasks, "hi", lambda s: None).lower()


# ---------- speak sentence by sentence while the model is still writing ----------
from r2.voice import split_sentences  # noqa: E402


def test_split_sentences_returns_complete_ones_and_the_unfinished_rest():
    assert split_sentences("Hello there. How are") == (["Hello there."], "How are")
    assert split_sentences("One. Two! Three? Four") == (["One.", "Two!", "Three?"], "Four")
    assert split_sentences("no end yet") == ([], "no end yet")
    assert split_sentences("") == ([], "")


def test_split_sentences_does_not_cut_numbers_or_trailing_periods_mid_stream():
    assert split_sentences("It costs 3.5 dollars. Fine") == (["It costs 3.5 dollars."], "Fine")
    assert split_sentences("Done.") == ([], "Done.")  # could still be 'Done.5'; wait for more text


def test_first_sentence_is_spoken_while_the_task_is_still_running(tmp_path):
    mem = Memory(tmp_path / "t.db")
    release = threading.Event()

    class Slow:
        def decide(self, messages, tools, on_token=None, should_stop=None):
            on_token("First sentence. Sec")
            release.wait(3)
            on_token("ond one.")
            return Decision(final="First sentence. Second one.")

    broker = ApprovalBroker(timeout=5)
    tasks = TaskManager(Agent(Slow(), ToolRegistry(), mem, broker.request), broker)
    spoken, out = [], {}
    t = threading.Thread(
        target=lambda: out.update(rest=run_task_and_wait(tasks, "tell me", lambda s: None, poll=0.02, on_sentence=spoken.append))
    )
    t.start()
    end = time.time() + 3
    while time.time() < end and not spoken:
        time.sleep(0.02)
    assert spoken == ["First sentence."] and t.is_alive()  # spoken before the task finished
    release.set()
    t.join(3)
    assert spoken == ["First sentence.", "Second one."]
    assert out["rest"] == ""  # everything was already spoken


def test_unspoken_answers_such_as_errors_are_still_returned_to_be_spoken(tmp_path):
    mem = Memory(tmp_path / "t.db")
    broker = ApprovalBroker(timeout=5)
    tasks = TaskManager(Agent(FakeBrain([]), ToolRegistry(), mem, broker.request), broker)
    tasks.paused = True
    spoken = []
    rest = run_task_and_wait(tasks, "hi", lambda s: None, on_sentence=spoken.append)
    assert spoken == [] and "paused" in rest.lower()


def test_assistant_stays_quiet_when_the_answer_was_already_spoken_piecemeal():
    p = Parts(answer="")
    assert p.build().run_once() == "answered"
    assert p.spoken == ["Yes?"]


def test_long_streamed_answers_stop_after_a_few_sentences(tmp_path):
    mem = Memory(tmp_path / "t.db")
    text = " ".join(f"Sentence number {i}." for i in range(10))
    broker = ApprovalBroker(timeout=5)
    tasks = TaskManager(Agent(FakeBrain([Decision(final=text)]), ToolRegistry(), mem, broker.request), broker)
    spoken = []
    rest = run_task_and_wait(tasks, "go on", lambda s: None, poll=0.02, on_sentence=spoken.append)
    assert len(spoken) <= 4 and rest == ""
