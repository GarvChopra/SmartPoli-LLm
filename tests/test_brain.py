import json
import threading
import time

import httpx
import pytest

from r2.brain import BrainError, Cancelled, FakeBrain, OllamaBrain
from r2.types import Decision, ToolCall


def ndjson(*chunks):
    return ("\n".join(json.dumps(c) for c in chunks) + "\n").encode()


def reply(text="", tool_calls=None, pieces=None):
    """A streamed Ollama reply: content in pieces, then a final done chunk."""
    chunks = [{"message": {"role": "assistant", "content": p}, "done": False} for p in (pieces or [text])]
    if tool_calls:
        chunks.append({"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}, "done": False})
    chunks.append({"message": {"role": "assistant", "content": ""}, "done": True})
    return httpx.Response(200, content=ndjson(*chunks))


def make_brain(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaBrain("http://ollama.test/", "qwen2.5:3b", client=client, **kw)


def test_fake_brain_plays_script_records_calls_and_streams_the_answer():
    brain = FakeBrain([Decision(final="a"), Decision(final="b")])
    seen = []
    assert brain.decide([{"role": "user", "content": "x"}], [{"t": 1}], on_token=seen.append).final == "a"
    assert seen == ["a"]
    assert brain.decide([], []).final == "b"
    assert brain.decide([], []).final == "(script exhausted)"
    assert brain.calls[0] == [{"role": "user", "content": "x"}]
    assert brain.tools_seen[0] == [{"t": 1}]


def test_ollama_streams_tokens_and_returns_the_full_answer():
    d_tokens = []
    d = make_brain(lambda r: reply(pieces=["Hel", "lo ", "there"])).decide([], [], on_token=d_tokens.append)
    assert d_tokens == ["Hel", "lo ", "there"]
    assert d.final == "Hello there" and d.tool_call is None


def test_ollama_parses_tool_call_and_sends_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return reply(tool_calls=[{"function": {"name": "list_dir", "arguments": {"path": "C:/"}}}])

    d = make_brain(handler, num_ctx=2048, num_predict=300, num_gpu=0, keep_alive="30m").decide(
        [{"role": "user", "content": "hi"}], [{"type": "function"}]
    )
    assert d.tool_call == ToolCall("list_dir", {"path": "C:/"})
    assert d.final is None
    body = seen["json"]
    assert seen["url"] == "http://ollama.test/api/chat"
    assert body["model"] == "qwen2.5:3b" and body["stream"] is True
    assert body["tools"] == [{"type": "function"}]
    assert body["keep_alive"] == "30m"
    assert body["options"] == {"num_ctx": 2048, "num_predict": 300, "num_gpu": 0}


def test_ollama_omits_tools_key_when_there_are_none():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return reply("ok")

    make_brain(handler).decide([], [])
    assert "tools" not in seen["json"]


def test_ollama_accepts_string_arguments():
    d = make_brain(
        lambda r: reply(tool_calls=[{"function": {"name": "remember", "arguments": '{"text": "hi"}'}}])
    ).decide([], [])
    assert d.tool_call == ToolCall("remember", {"text": "hi"})


def test_ollama_bad_json_arguments_become_empty_args():
    d = make_brain(lambda r: reply(tool_calls=[{"function": {"name": "x", "arguments": "{oops"}}])).decide([], [])
    assert d.tool_call == ToolCall("x", {})


def test_ollama_connection_error_becomes_brain_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(BrainError, match="Ollama"):
        make_brain(handler).decide([], [])


def test_ollama_http_error_becomes_brain_error():
    with pytest.raises(BrainError):
        make_brain(lambda r: httpx.Response(500, text="boom")).decide([], [])


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json\n"),
        httpx.Response(200, content=ndjson({"message": {"content": "", "tool_calls": [{"function": {}}]}})),
        httpx.Response(200, content=ndjson({"message": {"content": "", "tool_calls": ["oops"]}})),
    ],
)
def test_ollama_malformed_reply_becomes_brain_error(response):
    with pytest.raises(BrainError):
        make_brain(lambda request: response).decide([], [])


def test_stop_cancels_while_waiting_for_the_first_token():
    release = threading.Event()

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            release.wait(5)  # the model is still "thinking"
            yield ndjson({"message": {"content": "late"}, "done": True})

    def handler(request):
        return httpx.Response(200, stream=Slow())

    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    start = time.time()
    with pytest.raises(Cancelled):
        make_brain(handler).decide([], [], should_stop=stop.is_set)
    assert time.time() - start < 2  # returned long before the model finished
    release.set()


def test_stop_cancels_in_the_middle_of_an_answer():
    stop = threading.Event()
    seen = []

    def on_token(piece):
        seen.append(piece)
        stop.set()

    with pytest.raises(Cancelled):
        make_brain(lambda r: reply(pieces=["a", "b", "c"])).decide([], [], on_token=on_token, should_stop=stop.is_set)
    assert seen == ["a"]
