import json

import httpx
import pytest

from r2.brain import BrainError, CloudBrain, OllamaBrain, make_brain, to_anthropic_messages
from r2.config import Settings, load_dotenv
from r2.types import ToolCall

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a folder",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


def cloud(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CloudBrain("sk-test", "claude-sonnet-5-5", client=client)


# ---------- message conversion ----------
def test_conversion_extracts_system_and_pairs_tool_results():
    messages = [
        {"role": "system", "content": "be R2"},
        {"role": "user", "content": "list it"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "list_dir", "arguments": {"path": "C:/"}}}]},
        {"role": "tool", "content": "a\nb"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a"}}}]},
        {"role": "tool", "content": "text"},
    ]
    system, out = to_anthropic_messages(messages)
    assert system == "be R2"
    assert out[0] == {"role": "user", "content": "list it"}
    first_use = out[1]["content"][0]
    assert first_use["type"] == "tool_use" and first_use["name"] == "list_dir"
    assert first_use["input"] == {"path": "C:/"}
    first_result = out[2]["content"][0]
    assert first_result == {"type": "tool_result", "tool_use_id": first_use["id"], "content": "a\nb"}
    second_use = out[3]["content"][0]
    assert second_use["id"] != first_use["id"]
    assert out[4]["content"][0]["tool_use_id"] == second_use["id"]


def test_conversion_drops_leading_assistant_and_empty_messages():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "stale answer"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": ""},
    ]
    _, out = to_anthropic_messages(messages)
    assert out == [{"role": "user", "content": "hello"}]


# ---------- CloudBrain ----------
def test_cloud_sends_expected_request_and_parses_tool_use():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "t1", "name": "list_dir", "input": {"path": "C:/"}},
        ]})

    d = cloud(handler).decide([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}], TOOLS)
    assert d.tool_call == ToolCall("list_dir", {"path": "C:/"})
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    body = seen["json"]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "s"
    assert body["max_tokens"] > 0
    assert body["tools"] == [{
        "name": "list_dir",
        "description": "List a folder",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }]


def test_cloud_parses_text_answer():
    d = cloud(lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": "all done"}]})).decide(
        [{"role": "user", "content": "hi"}], TOOLS
    )
    assert d.final == "all done" and d.tool_call is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "bad key"}}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"content": [{"type": "tool_use"}]}),
    ],
)
def test_cloud_errors_become_brain_error(response):
    with pytest.raises(BrainError):
        cloud(lambda r: response).decide([{"role": "user", "content": "hi"}], TOOLS)


def test_cloud_connection_error_becomes_brain_error():
    def handler(request):
        raise httpx.ConnectError("offline")

    with pytest.raises(BrainError):
        cloud(handler).decide([{"role": "user", "content": "hi"}], TOOLS)


def test_api_key_is_never_in_error_text():
    with pytest.raises(BrainError) as err:
        cloud(lambda r: httpx.Response(500, text="boom")).decide([{"role": "user", "content": "hi"}], TOOLS)
    assert "sk-test" not in str(err.value)


# ---------- make_brain ----------
def test_make_brain_defaults_to_ollama():
    assert isinstance(make_brain(Settings()), OllamaBrain)


def test_make_brain_cloud_needs_a_key():
    with pytest.raises(BrainError, match="ANTHROPIC_API_KEY"):
        make_brain(Settings(brain="cloud"))
    assert isinstance(make_brain(Settings(brain="cloud", anthropic_api_key="k")), CloudBrain)


# ---------- .env ----------
def test_load_dotenv_parses_and_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\n\nR2_MODEL=llama3\nANTHROPIC_API_KEY=\"abc\"\nBAD LINE\nR2_PORT = 9000\n", encoding="utf-8")
    monkeypatch.delenv("R2_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("R2_PORT", "1234")
    load_dotenv(env)
    import os

    assert os.environ["R2_MODEL"] == "llama3"
    assert os.environ["ANTHROPIC_API_KEY"] == "abc"
    assert os.environ["R2_PORT"] == "1234"
    monkeypatch.delenv("R2_MODEL")
    monkeypatch.delenv("ANTHROPIC_API_KEY")


def test_load_dotenv_missing_file_is_fine(tmp_path):
    load_dotenv(tmp_path / "nope.env")


def test_new_settings_from_env_and_key_hidden_in_repr(monkeypatch):
    monkeypatch.setenv("R2_BRAIN", "cloud")
    monkeypatch.setenv("R2_PORT", "9001")
    monkeypatch.setenv("R2_HOST", "127.0.0.1")
    monkeypatch.setenv("R2_DATA_DIR", "mydata")
    monkeypatch.setenv("R2_CLOUD_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    s = Settings.from_env()
    assert (s.brain, s.port, s.host, str(s.data_dir), s.cloud_model) == (
        "cloud", 9001, "127.0.0.1", "mydata", "claude-opus-5-5")
    assert s.anthropic_api_key == "sk-secret"
    assert "sk-secret" not in repr(s)


def test_voice_and_overlay_settings(monkeypatch):
    s = Settings()
    assert (s.voice, s.overlay, s.wake_model, s.whisper_model) == (False, True, "", "base")
    monkeypatch.setenv("R2_VOICE", "1")
    monkeypatch.setenv("R2_OVERLAY", "0")
    monkeypatch.setenv("R2_WAKE_MODEL", "hey_r2.onnx")
    monkeypatch.setenv("R2_WHISPER_MODEL", "small")
    s = Settings.from_env()
    assert (s.voice, s.overlay, s.wake_model, s.whisper_model) == (True, False, "hey_r2.onnx", "small")


def test_gpu_layers_default_is_cpu_only_and_can_be_changed_or_left_to_auto(monkeypatch):
    assert Settings().gpu_layers == 0
    monkeypatch.setenv("R2_GPU_LAYERS", "20")
    assert Settings.from_env().gpu_layers == 20
    monkeypatch.setenv("R2_GPU_LAYERS", "auto")
    assert Settings.from_env().gpu_layers is None


def test_both_ollama_brains_get_the_gpu_setting():
    from r2.brain import make_chat_brain

    s = Settings(model="qwen2.5:3b", chat_model="qwen2.5:1.5b")
    assert make_brain(s)._options == {"num_ctx": 4096, "num_gpu": 0}
    assert make_chat_brain(s)._options == {"num_ctx": 2048, "num_predict": 300, "num_gpu": 0}
    auto = Settings(model="qwen2.5:3b", chat_model="qwen2.5:1.5b", gpu_layers=None)
    assert "num_gpu" not in make_brain(auto)._options
