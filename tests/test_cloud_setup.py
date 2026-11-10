import httpx
import pytest

from r2 import cli, daemon
from r2.cloud_setup import PROVIDERS, list_models, recommend, setup_cloud, write_env
from r2.config import Settings, load_dotenv
from r2.doctor import run_checks


def models_response(*ids):
    return httpx.Response(200, json={"data": [{"id": i} for i in ids]}, request=httpx.Request("GET", "https://x/models"))


GROQ_MODELS = ("whisper-large-v3", "llama-3.1-8b-instant", "llama-3.3-70b-versatile", "meta-llama/llama-guard-4", "playai-tts")


# ---------- writing .env ----------
def test_write_env_updates_existing_keys_and_appends_new_ones_keeping_everything_else(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# my settings\nR2_BRAIN=ollama\nR2_MODEL=qwen2.5:3b\n\nR2_PORT=8765\n", encoding="utf-8")
    write_env(env, {"R2_BRAIN": "openai", "R2_OPENAI_MODEL": "m"})
    text = env.read_text(encoding="utf-8")
    assert "# my settings" in text and "R2_MODEL=qwen2.5:3b" in text and "R2_PORT=8765" in text
    assert text.count("R2_BRAIN=") == 1 and "R2_BRAIN=openai" in text
    assert text.rstrip().endswith("R2_OPENAI_MODEL=m")


def test_write_env_creates_the_file_and_round_trips_through_load_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    write_env(env, {"R2_OPENAI_API_KEY": "k=ey", "R2_CHAT_MODEL": ""})
    for name in ("R2_OPENAI_API_KEY", "R2_CHAT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    load_dotenv(env)
    import os

    assert os.environ["R2_OPENAI_API_KEY"] == "k=ey" and os.environ["R2_CHAT_MODEL"] == ""
    monkeypatch.delenv("R2_OPENAI_API_KEY")
    monkeypatch.delenv("R2_CHAT_MODEL")


# ---------- listing and choosing models ----------
def test_list_models_sends_the_key_drops_non_chat_models_and_strips_prefixes():
    seen = {}

    def get(url, headers=None, timeout=None):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return models_response(*GROQ_MODELS, "models/gemini-2.0-flash")

    out = list_models("https://api.groq.com/openai/v1/", "gsk_key", get)
    assert seen == {"url": "https://api.groq.com/openai/v1/models", "auth": "Bearer gsk_key"}
    assert "llama-3.3-70b-versatile" in out and "gemini-2.0-flash" in out
    assert not any(w in m for m in out for w in ("whisper", "guard", "tts"))


def test_recommend_prefers_the_provider_style_model_else_the_first():
    assert recommend(["a", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"], PROVIDERS[0]["prefer"]) == "llama-3.3-70b-versatile"
    assert recommend(["gemini-pro", "gemini-2.0-flash"], PROVIDERS[1]["prefer"]) == "gemini-2.0-flash"
    assert recommend(["zzz", "yyy"], ["flash"]) == "zzz"
    assert recommend([], ["flash"]) is None


# ---------- the interactive setup ----------
def run_setup(tmp_path, answers, key="gsk_supersecret", get=None):
    lines = []
    answers = list(answers)
    code = setup_cloud(
        input_fn=lambda prompt="": answers.pop(0),
        secret_fn=lambda prompt="": key,
        http_get=get or (lambda url, headers=None, timeout=None: models_response(*GROQ_MODELS)),
        env_path=tmp_path / ".env",
        out=lines.append,
    )
    return code, "\n".join(lines), (tmp_path / ".env")


def test_setup_with_defaults_picks_groq_the_recommended_model_and_cloud_for_everything(tmp_path):
    code, output, env = run_setup(tmp_path, ["", "", ""])  # provider, model, scope: all defaults
    assert code == 0
    text = env.read_text(encoding="utf-8")
    assert "R2_BRAIN=openai" in text
    assert "R2_OPENAI_BASE_URL=https://api.groq.com/openai/v1" in text
    assert "R2_OPENAI_MODEL=llama-3.3-70b-versatile" in text
    assert "R2_OPENAI_API_KEY=gsk_supersecret" in text
    assert "R2_CHAT_MODEL=" in text and "R2_CHAT_MODEL=qwen" not in text  # chat goes to the cloud too (fastest)


def test_setup_never_prints_the_key(tmp_path):
    _, output, _ = run_setup(tmp_path, ["", "", ""])
    assert "gsk_supersecret" not in output


def test_setup_can_keep_chat_on_this_pc(tmp_path):
    _, _, env = run_setup(tmp_path, ["", "", "2"])
    assert "R2_CHAT_MODEL=qwen2.5:1.5b" in env.read_text(encoding="utf-8")


def test_setup_lets_you_pick_a_model_by_number_and_a_custom_provider(tmp_path):
    code, output, env = run_setup(tmp_path, ["5", "https://my.host/v1", "1", "1"])
    assert code == 0
    text = env.read_text(encoding="utf-8")
    assert "R2_OPENAI_BASE_URL=https://my.host/v1" in text and "R2_OPENAI_MODEL=" in text


def test_a_rejected_key_writes_nothing_and_explains(tmp_path):
    def get(url, headers=None, timeout=None):
        return httpx.Response(401, json={"error": {"message": "bad key gsk_supersecret"}}, request=httpx.Request("GET", url))

    code, output, env = run_setup(tmp_path, [""], get=get)
    assert code == 1 and not env.exists()
    assert "gsk_supersecret" not in output and "key" in output.lower()


def test_an_empty_key_stops_early(tmp_path):
    code, output, env = run_setup(tmp_path, [""], key="  ")
    assert code == 1 and not env.exists()


def test_the_privacy_trade_off_is_stated_up_front(tmp_path):
    _, output, _ = run_setup(tmp_path, ["", "", ""])
    assert "sent to" in output.lower() and "provider" in output.lower()


# ---------- cli ----------
def test_cloud_and_local_commands_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("R2_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    called = []
    monkeypatch.setattr(cli, "setup_cloud", lambda: called.append("cloud") or 0)
    assert cli.main(["cloud"]) == 0 and called == ["cloud"]
    assert cli.main(["local"]) == 0
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "R2_BRAIN=ollama" in text and "R2_CHAT_MODEL=qwen2.5:1.5b" in text


# ---------- doctor + warm-up with a cloud brain ----------
OPENAI = dict(brain="openai", openai_base_url="https://api.groq.com/openai/v1", openai_api_key="k", openai_model="m")


class Net:
    def __init__(self, routes):
        self.routes = routes

    def __call__(self, url, timeout=None, headers=None):
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return httpx.Response(value[0], json=value[1], request=httpx.Request("GET", url))
        raise httpx.ConnectError("unreachable")


def doctor(settings, routes):
    return {c.name: c for c in run_checks(settings, http_get=Net(routes), env={"OLLAMA_MAX_LOADED_MODELS": "2"},
                                          has_module=lambda m: True, has_browser=lambda: True)}


def test_doctor_checks_the_cloud_key_with_a_real_models_request():
    ok = doctor(Settings(**OPENAI), {"groq.com": (200, {"data": []}), "11434/api/tags": (200, {"models": [{"name": "qwen2.5:1.5b"}]}),
                                      "11434/api/ps": (200, {"models": [{"name": "qwen2.5:1.5b"}]})})
    assert ok["Cloud model"].status == "ok" and "m" in ok["Cloud model"].detail
    bad = doctor(Settings(**OPENAI), {"groq.com": (401, {}), "11434/api/tags": (200, {"models": []})})
    assert bad["Cloud model"].status == "fail" and "key" in bad["Cloud model"].detail.lower()
    missing = doctor(Settings(brain="openai"), {})
    assert missing["Cloud model"].status == "fail" and "r2 cloud" in missing["Cloud model"].detail


def test_doctor_does_not_ask_for_a_local_tool_model_when_the_cloud_does_the_tasks():
    checks = doctor(Settings(**OPENAI), {"groq.com": (200, {"data": []}), "11434/api/tags": (200, {"models": [{"name": "qwen2.5:1.5b"}]}),
                                         "11434/api/ps": (200, {"models": []})})
    assert "Tool model" not in checks and "Chat model" in checks
    everything_cloud = doctor(Settings(**OPENAI, chat_model=""), {"groq.com": (200, {"data": []})})
    assert "Ollama server" not in everything_cloud and "Chat model" not in everything_cloud


def test_warmup_only_loads_models_that_will_really_run_on_this_pc():
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json["model"])

        class R:
            status_code = 200

        return R()

    daemon.warm_models(Settings(**OPENAI, chat_model="qwen2.5:1.5b"), post=post)
    assert sent == ["qwen2.5:1.5b"]  # the big local tool model is not loaded: the cloud does that work
    sent.clear()
    assert "skipped" in daemon.warm_models(Settings(**OPENAI, chat_model=""), post=post)
    assert sent == []


NEBIUS_MODELS = ("meta-llama/Llama-3.3-70B-Instruct", "nvidia/nemotron-3-super", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "nvidia/Nemotron-3-Ultra-550b-a55b", "BAAI/bge-en-icl")


def test_nebius_token_factory_is_offered_and_prefers_an_nvidia_nemotron_model(tmp_path):
    nebius = next(p for p in PROVIDERS if "Nebius" in p["name"])
    assert nebius["base"] == "https://api.tokenfactory.nebius.com/v1"
    code, output, env = run_setup(
        tmp_path, ["4", "", ""], key="nb_secret",
        get=lambda url, headers=None, timeout=None: models_response(*NEBIUS_MODELS),
    )
    text = env.read_text(encoding="utf-8")
    assert code == 0
    assert "R2_OPENAI_BASE_URL=https://api.tokenfactory.nebius.com/v1" in text
    assert "R2_OPENAI_MODEL=nvidia/nemotron-3-super" in text
    assert "nb_secret" not in output


def test_nebius_setup_wires_nano_super_ultra_tiers(tmp_path):
    code, output, env = run_setup(
        tmp_path, ["4", "", ""], key="nb_secret",
        get=lambda url, headers=None, timeout=None: models_response(*NEBIUS_MODELS),
    )
    text = env.read_text(encoding="utf-8")
    assert "R2_OPENAI_MODEL=nvidia/nemotron-3-super" in text
    assert "R2_FAST_MODEL=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B" in text
    assert "R2_DEEP_MODEL=nvidia/Nemotron-3-Ultra-550b-a55b" in text
    assert "Model tiers" in output


def test_keeping_chat_on_this_pc_leaves_the_fast_tier_off(tmp_path):
    _, _, env = run_setup(
        tmp_path, ["4", "", "2"], key="nb_secret",
        get=lambda url, headers=None, timeout=None: models_response(*NEBIUS_MODELS),
    )
    assert "R2_FAST_MODEL=" in env.read_text(encoding="utf-8").splitlines()
