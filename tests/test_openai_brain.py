import json
import threading
import time

import httpx
import pytest

from r2.brain import BrainError, Cancelled, OllamaBrain, OpenAIBrain, make_brain, make_chat_brain, to_openai_messages
from r2.config import Settings
from r2.types import ToolCall

TOOLS = [
    {
        "type": "function",
        "function": {"name": "list_dir", "description": "List a folder", "parameters": {"type": "object", "properties": {}}},
    }
]


def brain(handler, key="sk-secret-123", **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAIBrain("https://api.example.com/v1/", key, "fast-model", client=client, **kw)


def answer(text=None, tool_calls=None):
    message = {"role": "assistant", "content": text}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": message}]})


# ---------- message conversion ----------
def test_conversion_adds_ids_stringifies_arguments_and_pairs_tool_results():
    messages = [
        {"role": "system", "content": "be R2"},
        {"role": "user", "content": "list it"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_dir", "arguments": {"path": "C:/"}}}]},
        {"role": "tool", "content": "a\nb"},
        {"role": "assistant", "content": "done"},
        {"role": "assistant", "content": ""},
    ]
    out = to_openai_messages(messages)
    assert out[0] == {"role": "system", "content": "be R2"}
    call = out[2]["tool_calls"][0]
    assert out[2]["content"] is None and call["type"] == "function" and call["id"]
    assert json.loads(call["function"]["arguments"]) == {"path": "C:/"}
    assert out[3] == {"role": "tool", "tool_call_id": call["id"], "name": "list_dir", "content": "a\nb"}
    assert out[4] == {"role": "assistant", "content": "done"}
    assert len(out) == 5  # the empty assistant message is dropped


def test_two_tool_rounds_get_different_ids():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "a", "arguments": {}}}]},
        {"role": "tool", "content": "1"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "b", "arguments": {}}}]},
        {"role": "tool", "content": "2"},
    ]
    out = to_openai_messages(messages)
    assert out[0]["tool_calls"][0]["id"] != out[2]["tool_calls"][0]["id"]
    assert out[3]["tool_call_id"] == out[2]["tool_calls"][0]["id"]


# ---------- requests and replies ----------
def test_request_shape_and_headers():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["json"] = json.loads(request.content)
        return answer("hi")

    brain(handler).decide([{"role": "user", "content": "hello"}], TOOLS)
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-secret-123"
    assert seen["json"]["model"] == "fast-model"
    assert seen["json"]["tools"] == TOOLS
    assert seen["json"]["messages"] == [{"role": "user", "content": "hello"}]


def test_no_tools_key_when_there_are_no_tools():
    seen = {}
    brain(lambda r: seen.update(j=json.loads(r.content)) or answer("hi")).decide([{"role": "user", "content": "x"}], [])
    assert "tools" not in seen["j"]


def test_text_answer_is_streamed_to_the_callback_in_one_piece():
    tokens = []
    d = brain(lambda r: answer("Hello there.")).decide([{"role": "user", "content": "x"}], [], on_token=tokens.append)
    assert d.final == "Hello there." and d.tool_call is None and tokens == ["Hello there."]


def test_tool_call_is_parsed_with_json_string_arguments():
    d = brain(
        lambda r: answer(None, [{"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": '{"path": "C:/"}'}}])
    ).decide([{"role": "user", "content": "x"}], TOOLS)
    assert d.tool_call == ToolCall("list_dir", {"path": "C:/"})


def test_providers_that_omit_ids_or_send_dict_arguments_still_work():
    d = brain(lambda r: answer(None, [{"function": {"name": "list_dir", "arguments": {"path": "D:/"}}}])).decide([], TOOLS)
    assert d.tool_call == ToolCall("list_dir", {"path": "D:/"})


def test_bad_json_arguments_become_empty_arguments():
    d = brain(lambda r: answer(None, [{"function": {"name": "x", "arguments": "{oops"}}])).decide([], TOOLS)
    assert d.tool_call == ToolCall("x", {})


def test_null_content_is_an_empty_answer():
    assert brain(lambda r: answer(None)).decide([], []).final == ""


# ---------- errors ----------
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {}}]}}]}),
    ],
)
def test_malformed_replies_become_brain_errors(response):
    with pytest.raises(BrainError):
        brain(lambda r: response).decide([], [])


def test_rate_limit_message_is_friendly():
    with pytest.raises(BrainError, match="rate limit"):
        brain(lambda r: httpx.Response(429, json={"error": {"message": "slow down"}})).decide([], [])


def test_bad_key_message_never_contains_the_key():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Invalid API key sk-secret-123 provided"}})

    with pytest.raises(BrainError) as err:
        brain(handler).decide([], [])
    assert "sk-secret-123" not in str(err.value) and "401" in str(err.value)


def test_connection_error_is_a_brain_error_without_the_key():
    def handler(request):
        raise httpx.ConnectError("offline")

    with pytest.raises(BrainError) as err:
        brain(handler).decide([], [])
    assert "sk-secret-123" not in str(err.value)


def test_stop_abandons_a_slow_request_immediately():
    release = threading.Event()

    def handler(request):
        release.wait(5)
        return answer("late")

    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    start = time.time()
    with pytest.raises(Cancelled):
        brain(handler).decide([], [], should_stop=stop.is_set)
    assert time.time() - start < 2
    release.set()


# ---------- settings and factories ----------
def test_settings_read_the_openai_variables_and_hide_the_key(monkeypatch):
    monkeypatch.setenv("R2_BRAIN", "openai")
    monkeypatch.setenv("R2_OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("R2_OPENAI_API_KEY", "gsk_topsecret")
    monkeypatch.setenv("R2_OPENAI_MODEL", "some-model")
    s = Settings.from_env()
    assert (s.brain, s.openai_base_url, s.openai_model) == ("openai", "https://api.groq.com/openai/v1", "some-model")
    assert s.openai_api_key == "gsk_topsecret" and "gsk_topsecret" not in repr(s)


def test_make_brain_openai_needs_everything_configured(monkeypatch):
    monkeypatch.setattr("r2.brain.ollama_up", lambda url: False)  # do not depend on this machine's Ollama
    full = Settings(brain="openai", openai_base_url="https://x/v1", openai_api_key="k", openai_model="m")
    from r2.tiers import TieredBrain

    assert isinstance(make_brain(full), TieredBrain)
    for missing in ("openai_base_url", "openai_api_key", "openai_model"):
        broken = Settings(**{**{"brain": "openai", "openai_base_url": "https://x/v1", "openai_api_key": "k", "openai_model": "m"}, missing: ""})
        with pytest.raises(BrainError, match="R2_OPENAI"):
            make_brain(broken)


def test_chat_stays_local_by_default_with_the_cloud_for_tasks_and_goes_to_the_cloud_when_chat_model_is_empty():
    base = dict(brain="openai", openai_base_url="https://x/v1", openai_api_key="k", openai_model="m")
    assert isinstance(make_chat_brain(Settings(**base, chat_model="qwen2.5:1.5b")), OllamaBrain)
    assert make_chat_brain(Settings(**base, chat_model="")) is None  # the agent then uses the cloud brain for chat too


def test_the_hosted_brain_is_tiered_and_notices_a_local_model_at_request_time(monkeypatch):
    from r2.tiers import TieredBrain

    base = dict(brain="openai", openai_base_url="https://x/v1", openai_api_key="k", openai_model="m")
    up = {"now": False}
    monkeypatch.setattr("r2.brain.ollama_up", lambda url: up["now"])
    brain = make_brain(Settings(**base, deep_model="big"))
    assert isinstance(brain, TieredBrain) and brain._brains["deep"]._model == "big"
    assert brain._local_ok() is False
    up["now"] = True
    brain._local_seen = (0.0, False)  # let the 20 s memory expire
    assert brain._local_ok() is True  # Ollama was started after R2: no restart needed


def test_a_fast_model_becomes_a_tiered_hosted_chat_brain_that_keeps_secrets_local(monkeypatch):
    from r2.tiers import TieredBrain

    base = dict(brain="openai", openai_base_url="https://x/v1", openai_api_key="k", openai_model="m")
    monkeypatch.setattr("r2.brain.ollama_up", lambda url: True)
    chat = make_chat_brain(Settings(**base, fast_model="nano", chat_model="qwen2.5:1.5b"))
    assert isinstance(chat, TieredBrain) and chat._brains["act"]._model == "nano"
    assert chat._brains["local"]._model == "qwen2.5:1.5b"
    from r2.tiers import classify

    assert classify([{"role": "user", "content": "my card is 4111 1111 1111 1111"}], True, chat._local_ok(), False).tier == "local"
