import pytest

from r2.memory import Memory
from r2.router import select_tools
from r2.skills import SkillError, SkillStore, parse, validate
from r2.tools import full_registry

GOOD = "---\nname: pack-bag\ndescription: Make a packing list\n---\nList the items.\n"


def write(root, name, text):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")


def test_every_bundled_skill_is_valid(tmp_path):
    store = SkillStore(tmp_path)
    names = [s.name for s in store.all()]
    assert store.problems == []
    assert {"tidy-downloads", "find-and-summarize", "daily-brief", "safe-research", "meeting-prep"} <= set(names)


def test_parse_reads_the_header_and_body():
    meta, body = parse(GOOD)
    assert meta == {"name": "pack-bag", "description": "Make a packing list"} and body == "List the items."


@pytest.mark.parametrize(
    "text",
    ["no header", "---\nname: x\n", "---\nname: Bad Name\ndescription: d\n---\nbody", "---\nname: ok\ndescription: d\n---\n"],
)
def test_broken_skills_are_rejected(tmp_path, text):
    write(tmp_path / "skills", "ok", text)
    store = SkillStore(tmp_path, bundled=None)
    assert store.all() == [] and store.problems


def test_name_must_match_the_folder(tmp_path):
    write(tmp_path / "skills", "other-name", GOOD)
    store = SkillStore(tmp_path, bundled=None)
    assert store.all() == [] and "must match its folder" in store.problems[0]


def test_user_skills_override_bundled_ones_and_the_index_is_short(tmp_path):
    write(tmp_path / "skills", "daily-brief", "---\nname: daily-brief\ndescription: mine\n---\nmy steps")
    store = SkillStore(tmp_path)
    assert store.get("daily-brief").source == "user" and store.get("daily-brief").body == "my steps"
    assert "- daily-brief: mine" in store.index()


def test_skills_cannot_hold_secrets():
    with pytest.raises(SkillError):
        validate("log-in", "login helper", "use password: hunter2")


def test_propose_then_approve_makes_it_active_only_after_approval(tmp_path):
    store = SkillStore(tmp_path, bundled=None)
    store.propose("pack-bag", "Make a packing list", "List the items.")
    assert store.get("pack-bag") is None and [s.name for s in store.proposed()] == ["pack-bag"]
    store.approve("pack-bag")
    assert store.get("pack-bag").body == "List the items." and store.proposed() == []


def test_reject_deletes_a_proposal_and_bad_names_cannot_escape_the_folder(tmp_path):
    store = SkillStore(tmp_path, bundled=None)
    store.propose("pack-bag", "d", "steps")
    assert store.reject("pack-bag") is True and store.reject("pack-bag") is False
    assert store.reject("../x") is False
    with pytest.raises(SkillError):
        store.approve("../x")


def test_approve_refuses_to_overwrite_and_revalidates_edited_proposals(tmp_path):
    store = SkillStore(tmp_path, bundled=None)
    write(tmp_path / "skills", "pack-bag", GOOD)
    store.propose("pack-bag", "d", "steps")
    with pytest.raises(SkillError, match="already have"):
        store.approve("pack-bag")
    store.propose("tainted", "d", "steps")
    (store.proposed_dir / "tainted" / "SKILL.md").write_text(
        "---\nname: tainted\ndescription: d\n---\npassword: hunter2", encoding="utf-8"
    )
    with pytest.raises(SkillError):
        store.approve("tainted")


def test_the_tools_use_and_propose_skills(tmp_path):
    reg = full_registry(Memory(tmp_path / "m.db"), tmp_path, headless=True)
    assert "never lose a file" in reg.get("use_skill").func("tidy-downloads")
    assert reg.get("use_skill").func("nope").startswith("ERROR: no skill called")
    assert "inactive" in reg.get("propose_skill").func("pack-bag", "Packing list", "List the items.")
    assert reg.get("propose_skill").func("Bad Name", "d", "x").startswith("ERROR")
    assert reg.get("use_skill").risk.risk.name == "SAFE" and reg.get("propose_skill").risk.risk.name == "SAFE"


def test_skill_tools_are_always_offered_to_the_model(tmp_path):
    reg = full_registry(Memory(tmp_path / "m.db"), tmp_path, headless=True)
    names = {s["function"]["name"] for s in select_tools("hello there", reg.schemas())}
    assert {"use_skill", "propose_skill", "remember"} <= names
