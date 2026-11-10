import httpx
import pytest

from r2 import cli
from r2.brain import FakeBrain
from r2.config import Settings
from r2.doctor import Check, format_report, run_checks, time_brain
from r2.types import Decision


class Net:
    """Fake network: maps URL substrings to (status, json) or raises when told to."""

    def __init__(self, routes):
        self.routes = routes

    def __call__(self, url, timeout=None):
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                status, body = value
                return httpx.Response(status, json=body, request=httpx.Request("GET", url))
        raise httpx.ConnectError("unreachable")


def tags(*names):
    return (200, {"models": [{"name": n} for n in names]})


def ps(*names):
    return (200, {"models": [{"name": n} for n in names]})


GOOD = {
    "/api/tags": tags("qwen2.5:7b", "qwen2.5:1.5b"),
    "/api/ps": ps("qwen2.5:7b", "qwen2.5:1.5b"),
    "localhost:8765": (200, {}),
}
SETTINGS = Settings(model="qwen2.5:7b", chat_model="qwen2.5:1.5b")


def by_name(checks):
    return {c.name: c for c in checks}


def run(routes, settings=SETTINGS, env=None, modules=("sounddevice",), browser=True):
    return by_name(
        run_checks(
            settings,
            http_get=Net(routes),
            env={"OLLAMA_MAX_LOADED_MODELS": "2", **(env or {})},
            has_module=lambda m: m in modules,
            has_browser=lambda: browser,
        )
    )


def test_everything_healthy():
    checks = run(GOOD)
    assert all(c.status == "ok" for c in checks.values() if c.name != "Voice packages"), checks


def test_ollama_down_is_a_failure_with_the_fix():
    checks = run({"localhost:8765": (200, {})})
    assert checks["Ollama server"].status == "fail"
    assert "ollama" in checks["Ollama server"].detail.lower()


def test_missing_models_are_failures_that_name_the_pull_command():
    checks = run({**GOOD, "/api/tags": tags("qwen2.5:1.5b")})
    assert checks["Tool model"].status == "fail" and "ollama pull qwen2.5:7b" in checks["Tool model"].detail
    assert checks["Chat model"].status == "ok"


def test_models_not_loaded_is_a_slowness_warning():
    checks = run({**GOOD, "/api/ps": ps("qwen2.5:1.5b")})
    assert checks["Models in memory"].status == "warn"
    assert "qwen2.5:7b" in checks["Models in memory"].detail


def test_single_model_limit_is_a_warning_because_models_will_swap():
    checks = run(GOOD, env={"OLLAMA_MAX_LOADED_MODELS": "1"})
    assert checks["Ollama keeps 2 models loaded"].status == "warn"
    unset = by_name(run_checks(SETTINGS, http_get=Net(GOOD), env={}, has_module=lambda m: True, has_browser=lambda: True))
    assert unset["Ollama keeps 2 models loaded"].status == "warn"


def test_r2_server_not_running_is_a_warning_not_a_failure():
    routes = {k: v for k, v in GOOD.items() if k != "localhost:8765"}
    assert run(routes)["R2 server"].status == "warn"


def test_no_browser_is_a_warning():
    assert run(GOOD, browser=False)["Browser for web tasks"].status == "warn"


def test_cloud_brain_without_key_fails_and_skips_ollama_checks():
    checks = run(GOOD, settings=Settings(brain="cloud"))
    assert checks["Cloud brain"].status == "fail"
    assert "Tool model" not in checks


def test_voice_packages_only_matter_when_voice_is_on():
    off = run(GOOD, modules=())
    assert off["Voice packages"].status == "ok"  # not enabled, nothing to complain about
    on = run(GOOD, settings=Settings(model="qwen2.5:7b", chat_model="qwen2.5:1.5b", voice=True), modules=())
    assert on["Voice packages"].status == "warn" and "pip install" in on["Voice packages"].detail


def test_report_marks_each_line_and_summarises():
    text = format_report([Check("A", "ok", "fine"), Check("B", "warn", "meh"), Check("C", "fail", "broken")])
    assert "[ OK ] A" in text and "[WARN] B" in text and "[FAIL] C" in text
    assert text.strip().splitlines()[-1].startswith("1 problem")


# ---------- bench ----------
def test_time_brain_reports_first_word_total_and_words_per_second():
    ticks = iter([0.0, 1.5, 4.0])  # start, first word, end

    class Streaming(FakeBrain):
        def decide(self, messages, tools, on_token=None, should_stop=None):
            for piece in ("a ", "b ", "c ", "d "):
                on_token(piece)
            return Decision(final="a b c d")

    first, total, rate = time_brain(Streaming([]), "hi", clock=lambda: next(ticks))
    assert (first, total) == (1.5, 4.0)
    assert rate == pytest.approx(4 / 2.5)


def test_bench_command_runs_through_the_cli(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("R2_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_bench_brains", lambda settings: [("fake-model", FakeBrain([Decision(final="hello")] * 3))])
    assert cli.main(["bench"]) == 0
    out = capsys.readouterr().out
    assert "fake-model" in out and "first word" in out.lower()


def test_doctor_command_exit_code_reflects_failures(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("R2_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_run_doctor_checks", lambda settings: [Check("X", "fail", "bad")])
    assert cli.main(["doctor"]) == 1
    monkeypatch.setattr(cli, "_run_doctor_checks", lambda settings: [Check("X", "warn", "meh"), Check("Y", "ok", "")])
    assert cli.main(["doctor"]) == 0
    assert "[WARN] X" in capsys.readouterr().out


def test_doctor_reports_the_model_tiers_and_the_privacy_gateway_for_a_hosted_brain():
    from r2.config import Settings
    from r2.doctor import run_checks

    class Resp:
        status_code = 200

        def json(self):
            return {"models": []}

    settings = Settings(
        brain="openai", openai_base_url="https://x/v1", openai_api_key="k", openai_model="super",
        fast_model="nano", deep_model="ultra", chat_model="",
    )
    checks = {c.name: c for c in run_checks(settings, http_get=lambda *a, **k: Resp(), env={})}
    assert checks["Model tiers"].status == "ok" and "nano" in checks["Model tiers"].detail and "ultra" in checks["Model tiers"].detail
    assert checks["Privacy gateway"].status == "ok" and "r2 ledger" in checks["Privacy gateway"].detail
    bare = Settings(brain="openai", openai_base_url="https://x/v1", openai_api_key="k", openai_model="super", chat_model="")
    only = {c.name: c for c in run_checks(bare, http_get=lambda *a, **k: Resp(), env={})}
    assert only["Model tiers"].status == "warn" and "r2 cloud" in only["Model tiers"].detail
