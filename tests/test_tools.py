import pytest

from r2.memory import Memory
from r2.tools import ToolRegistry, ToolSpec, default_registry
from r2.tools.files import list_dir, read_file
from r2.types import Risk, RiskInfo


def test_registry_register_get_and_schema():
    reg = ToolRegistry()
    spec = ToolSpec(
        "echo",
        "Echo text",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        lambda text: text,
        RiskInfo(Risk.SAFE),
    )
    reg.register(spec)
    assert reg.get("echo") is spec
    assert reg.get("nope") is None
    assert reg.schemas() == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo text",
                "parameters": spec.parameters,
            },
        }
    ]


def test_list_dir_marks_directories(tmp_path):
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    assert list_dir(str(tmp_path)) == "2 items (1 file, 1 folder):\nb.txt\nsub/"


def test_list_dir_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        list_dir(str(tmp_path / "missing"))


def test_read_file_truncates(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("a" * 100)
    out = read_file(str(p), max_chars=10)
    assert out.startswith("a" * 10)
    assert "truncated" in out


def test_read_file_tolerates_bad_bytes(tmp_path):
    p = tmp_path / "bin.dat"
    p.write_bytes(b"ok\xff\xfe")
    assert read_file(str(p)).startswith("ok")


def test_default_registry_is_all_safe_and_remember_works(tmp_path):
    mem = Memory(tmp_path / "t.db")
    reg = default_registry(mem)
    assert {"list_dir", "read_file", "remember"} <= {s["function"]["name"] for s in reg.schemas()}
    assert all(reg.get(n).risk.risk is Risk.SAFE for n in ("list_dir", "read_file", "remember"))
    out = reg.get("remember").func(text="my name is Garv")
    assert out.startswith("Saved fact #")
    assert mem.list_facts()[0].source == "agent"


def test_remember_refuses_secrets(tmp_path):
    mem = Memory(tmp_path / "t.db")
    out = default_registry(mem).get("remember").func(text="my password is hunter2")
    assert out.startswith("Not saved")
    assert mem.list_facts() == []


def _spec(classify=None):
    return ToolSpec(
        "t", "d", {"type": "object", "properties": {}}, lambda **kw: "ok",
        RiskInfo(Risk.SAFE), classify,
    )


def test_risk_for_defaults_to_static_risk():
    assert _spec().risk_for({"a": 1}) == RiskInfo(Risk.SAFE)


def test_risk_for_uses_classifier():
    spec = _spec(lambda args: RiskInfo(Risk.BLOCKED, "x") if args.get("bad") else RiskInfo(Risk.SAFE))
    assert spec.risk_for({"bad": True}).risk is Risk.BLOCKED
    assert spec.risk_for({}).risk is Risk.SAFE


def test_risk_for_falls_back_to_confirm_when_classifier_crashes():
    spec = _spec(lambda args: 1 / 0)
    assert spec.risk_for({}).risk is Risk.CONFIRM


def test_list_dir_states_the_counts_so_the_model_never_has_to_count(tmp_path):
    for i in range(12):
        (tmp_path / f"f{i}.txt").write_text("x")
    (tmp_path / "d1").mkdir()
    (tmp_path / "d2").mkdir()
    out = list_dir(str(tmp_path))
    assert out.splitlines()[0] == "14 items (12 files, 2 folders):"
    assert len(out.splitlines()) == 15


def test_list_dir_of_an_empty_folder():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        assert list_dir(d) == "0 items (0 files, 0 folders):"
