"""Hands-free voice: wake word -> record -> speech-to-text -> task -> spoken answer.

Design rules:
- Voice may start and stop tasks and hear answers. It can NEVER approve a risky action:
  a spoken "yes" could come from a video, a TV or someone nearby, so approvals stay on
  the phone, the tray menu or the overlay.
- Every hardware piece (wake word, microphone, Whisper, text-to-speech) is injected, so
  the flow is tested with fakes. The real pieces below are optional installs (`.[voice]`).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

log = logging.getLogger("r2.voice")

RATE = 16000
FRAME = 1280  # 80 ms at 16 kHz

_STOP_PHRASES = {"stop", "cancel", "never mind", "nevermind", "abort", "stop that", "stop it", "cancel that"}


def is_stop_command(text: str) -> bool:
    cleaned = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    return cleaned in _STOP_PHRASES


def speakable(text: str, limit: int = 300) -> str:
    text = re.sub(r"[*`#]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", text[:limit])]
    if ends:
        return text[: ends[-1]]
    return text[:limit] + "..."


def collect_utterance(
    frames,
    rate: int = RATE,
    frame_len: int = FRAME,
    silence_s: float = 0.8,
    max_s: float = 15.0,
    wait_s: float = 5.0,
    calibrate_frames: int = 4,
):
    """Read int16 frames until the speaker pauses. Returns the audio, or None if nobody spoke.

    The first few frames set the background-noise level; speech is anything well above it."""
    import numpy as np

    frame_s = frame_len / rate
    chunks: list = []
    floor, threshold = 0.0, None
    started, quiet, seen = False, 0, 0
    for frame in frames:
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
        seen += 1
        if seen <= calibrate_frames:
            floor = max(floor, rms)
            if seen == calibrate_frames:
                threshold = max(300.0, floor * 3)
            continue
        if not started:
            if rms > threshold:
                started = True
                chunks.append(frame)
            elif (seen - calibrate_frames) * frame_s >= wait_s:
                return None
        else:
            chunks.append(frame)
            quiet = quiet + 1 if rms <= threshold else 0
            if quiet * frame_s >= silence_s or len(chunks) * frame_s >= max_s:
                break
    return np.concatenate(chunks) if chunks else None


MAX_SPOKEN_SENTENCES = 4  # a spoken answer should be short; the full text is always in the window
_BOUNDARY = re.compile(r"[.!?](?=\s)")


def _split(text: str) -> tuple[list[str], int]:
    """Complete sentences in `text`, and the index where the unfinished rest begins."""
    sentences, start = [], 0
    for m in _BOUNDARY.finditer(text):
        sentence = text[start : m.end()].strip()
        if sentence:
            sentences.append(sentence)
        start = m.end()
    while start < len(text) and text[start].isspace():
        start += 1
    return sentences, start


def split_sentences(text: str) -> tuple[list[str], str]:
    """Split streamed text into finished sentences and the unfinished remainder.
    A sentence only counts as finished once whitespace follows its end mark, so "3.5" and a
    trailing "Done." (which might still become "Done.5") are not cut early."""
    sentences, start = _split(text)
    return sentences, text[start:]


def run_task_and_wait(
    tasks,
    text: str,
    notify: Callable[[str], None],
    poll: float = 0.5,
    timeout: float = 900,
    on_sentence: Callable[[str], None] | None = None,
) -> str:
    """Start a task on the TaskManager and wait for its answer.

    Pending approvals are announced once (via `notify`) and then left for a real approval
    surface. With `on_sentence`, each sentence of the answer is passed on as soon as it is
    complete, so speech can start while the model is still writing; the call then returns ""
    because the answer has already been delivered."""
    from r2.runtime import TaskBusy

    try:
        tasks.start(text)
    except TaskBusy as exc:
        return f"I can't do that right now: {exc}."
    told = None
    spoken: list[str] = []
    consumed = 0

    def say(sentence: str) -> None:
        if on_sentence is not None and len(spoken) < MAX_SPOKEN_SENTENCES:
            on_sentence(sentence)
            spoken.append(sentence)

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = tasks.status()
        pending = status["pending"]
        if pending and pending["id"] != told:
            told = pending["id"]
            notify(
                f"I need your approval to run {pending['tool']}. "
                "Please approve it on your phone or from the tray menu."
            )
        if on_sentence is not None and status["partial"]:
            sentences, rest_at = _split(status["partial"][consumed:])
            for sentence in sentences:
                say(sentence)
            consumed += rest_at
        if not status["running"]:
            break
        time.sleep(poll)
    else:
        tasks.stop()
        return "That took too long, so I stopped it."
    answer = (tasks.status()["result"] or {}).get("answer") or "Done."
    if on_sentence is None:
        return answer
    answer_n = re.sub(r"\s+", " ", answer).strip()
    spoken_text = " ".join(spoken)
    if not spoken:
        remaining = answer_n
    elif answer_n.startswith(spoken_text):
        remaining = answer_n[len(spoken_text):].strip()
    else:
        remaining = ""
    sentences, rest_at = _split(remaining + " ")
    for sentence in sentences:
        say(sentence)
    return ""


class VoiceAssistant:
    def __init__(
        self,
        wake: Callable[[], bool],
        record: Callable[[], object],
        transcribe: Callable[[object], str],
        speak: Callable[[str], None],
        run_task: Callable[[str], str],
        stop_task: Callable[[], None],
        on_mute_change: Callable[[bool], None] | None = None,
    ):
        self.wake = wake
        self.record = record
        self.transcribe = transcribe
        self.speak = speak
        self.run_task = run_task
        self.stop_task = stop_task
        self._on_mute_change = on_mute_change
        self._muted = False
        self._manual = threading.Event()

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        self._muted = bool(value)
        if self._on_mute_change:
            self._on_mute_change(self._muted)

    def listen_now(self) -> None:
        """Skip the wake word once (tray menu 'Listen now')."""
        self._manual.set()

    def run_once(self) -> str:
        if self._muted:
            return "muted"
        try:
            if self._manual.is_set():
                self._manual.clear()
                heard = True
            else:
                heard = self.wake()
            if not heard:
                return "no_wake"
            self.speak("Yes?")
            audio = self.record()
            text = (self.transcribe(audio) if audio is not None else "").strip()
            if not text:
                self.speak("Sorry, I didn't catch that.")
                return "empty"
            if is_stop_command(text):
                self.stop_task()
                self.speak("Stopped.")
                return "stopped"
            answer = speakable(self.run_task(text))
            if answer:  # empty when the answer was already spoken sentence by sentence
                self.speak(answer)
            return "answered"
        except Exception:
            log.exception("voice turn failed")
            try:
                self.speak("Sorry, something went wrong.")
            except Exception:
                pass
            return "error"

    def run_forever(self, should_stop: threading.Event) -> None:
        while not should_stop.is_set():
            outcome = self.run_once()
            if outcome == "muted":
                time.sleep(0.2)
            elif outcome == "no_wake":
                time.sleep(0.01)


# ---------------------------------------------------------------------------
# Real hardware pieces. Optional install: pip install -e ".[voice]"
# These cannot be exercised in automated tests (they need a microphone and models).
# ---------------------------------------------------------------------------
class VoiceUnavailable(RuntimeError):
    pass


def build_real_voice(settings, run_task: Callable[[str], str], stop_task: Callable[[], None]) -> VoiceAssistant:
    try:
        import numpy as np
        import pyttsx3
        import sounddevice as sd
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VoiceUnavailable(f'voice needs extra packages: pip install -e ".[voice]" ({exc})') from exc

    state: dict = {"stream": None, "whisper": None, "wake": None}

    def stream():
        if state["stream"] is None:
            state["stream"] = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME)
            state["stream"].start()
        return state["stream"]

    def frames():
        s = stream()
        while True:
            data, _ = s.read(FRAME)
            yield data[:, 0].copy()

    def close_mic(muted: bool) -> None:
        if muted and state["stream"] is not None:
            state["stream"].stop()
            state["stream"].close()
            state["stream"] = None

    def wake() -> bool:
        if not settings.wake_model:
            time.sleep(0.5)  # no wake-word model configured: only the tray "Listen now" works
            return False
        if state["wake"] is None:
            from openwakeword.model import Model

            state["wake"] = Model(wakeword_models=[settings.wake_model])
        s = stream()
        for _ in range(12):  # ~1 second, then return so mute/stop can be checked
            data, _ = s.read(FRAME)
            scores = state["wake"].predict(data[:, 0])
            if max(scores.values(), default=0.0) > 0.5:
                return True
        return False

    def record():
        return collect_utterance(frames())

    def transcribe(audio) -> str:
        if state["whisper"] is None:
            state["whisper"] = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
        pcm = audio.astype(np.float32) / 32768.0
        segments, _ = state["whisper"].transcribe(pcm, beam_size=1, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    def speak(text: str) -> None:
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        engine.stop()

    return VoiceAssistant(wake, record, transcribe, speak, run_task, stop_task, on_mute_change=close_mic)
