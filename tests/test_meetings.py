import datetime as dt
import json

import pytest

from r2.brain import FakeBrain
from r2.meetings import (
    UNASSIGNED, MeetingError, MeetingStore, Member, extract_model, extract_rules, merge, meeting_markdown,
    parse_member, parse_transcript, process_meeting, resolve_due, tasks_markdown,
)
from r2.types import Decision

MON = dt.date(2026, 10, 12)  # a Monday
TEAM = [Member("Aman Sharma", ("Ams",)), Member("Riya"), Member("Karan")]

ENGLISH = """Garv: Okay everyone, quick sync on the launch. We decided to go with the dark theme for the first release.
Garv: Aman, please finish the login page by Friday.
Garv: Riya will handle the API documentation and Karan will do the QA pass before 20 October.
Karan: I'll set up the test devices tomorrow.
Garv: Can you review the pricing page, Riya? We need it in 3 days.
Garv: What should we do about the payment provider? We are not sure yet.
Garv: Someone needs to update the privacy policy.
"""

HINGLISH = """Garv: Theek hai, aaj ka plan. Aman tum backend ka kaam kal tak kar do.
Garv: Riya aap design dekh lena, shukravar tak.
Garv: Karan ko demo video bana dena agle hafte tak.
Garv: Main deck bhej dunga parso tak.
"""

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
<v Garv>Karan, can you deploy the staging build by Wednesday?</v>

2
00:00:05.000 --> 00:00:08.000
<v Riya>I will write the release notes tonight.</v>
"""


def owners(ext):
    return {(i.owner, i.task) for i in ext.items}


def by_owner(ext, name):
    return [i for i in ext.items if i.owner == name]


# ---------- transcript parsing ----------
def test_speaker_lines_timestamps_and_wrapped_captions():
    turns = parse_transcript("[00:01] Garv: Hello team\ncontinued here\nRiya: Hi\n00:00:09\nGarv: Bye")
    assert [(t.speaker, t.text) for t in turns] == [("Garv", "Hello team continued here"), ("Riya", "Hi"), ("Garv", "Bye")]


def test_webvtt_voice_tags_and_cue_numbers_are_understood():
    turns = parse_transcript(VTT)
    assert [(t.speaker, t.text[:11]) for t in turns] == [("Garv", "Karan, can "), ("Riya", "I will writ")]


def test_plain_text_without_speakers_and_non_names_before_colons():
    turns = parse_transcript("Note: remember the demo\nwe talked about launch dates")
    assert all(t.speaker is None for t in turns) and "remember the demo" in turns[0].text


# ---------- deadlines ----------
@pytest.mark.parametrize("text,due", [
    ("by Friday", "2026-10-16"), ("tomorrow", "2026-10-13"), ("kal tak", "2026-10-13"), ("parso tak", "2026-10-14"),
    ("in 3 days", "2026-10-15"), ("next week", "2026-10-19"), ("agle hafte tak", "2026-10-19"), ("shukravar tak", "2026-10-16"),
    ("by 20 October", "2026-10-20"), ("Oct 20", "2026-10-20"), ("20/10", "2026-10-20"), ("end of the week", "2026-10-16"),
    ("tonight", "2026-10-12"), ("EOD", "2026-10-12"), ("next Monday", "2026-10-19"),
])
def test_deadlines_in_english_and_hinglish(text, due):
    assert resolve_due(text, MON)[0] == due


def test_no_deadline_is_none():
    assert resolve_due("sometime soon maybe", MON) == (None, None)


# ---------- English assignments ----------
def test_english_assignments_get_the_right_owner_task_and_deadline():
    ext = extract_rules(parse_transcript(ENGLISH), TEAM, MON)
    aman = by_owner(ext, "Aman Sharma")
    assert len(aman) == 1 and "login page" in aman[0].task and aman[0].due == "2026-10-16"
    assert aman[0].quote.startswith("Aman, please finish")
    riya = by_owner(ext, "Riya")
    assert any("API documentation" in i.task for i in riya)
    karan = by_owner(ext, "Karan")
    assert any("QA pass" in i.task and i.due == "2026-10-20" for i in karan)  # second clause of the same sentence
    assert any("test devices" in i.task and i.due == "2026-10-13" for i in karan)  # "I'll" -> the speaker
    assert any("pricing page" in i.task and i.due == "2026-10-15" for i in riya)  # "Can you review ... , Riya?"
    assert UNASSIGNED in {i.owner for i in ext.items} and any("privacy policy" in i.task for i in by_owner(ext, UNASSIGNED))


def test_decisions_and_open_questions_are_found():
    ext = extract_rules(parse_transcript(ENGLISH), TEAM, MON)
    assert any("dark theme" in d for d in ext.decisions)
    assert any("payment provider" in q for q in ext.questions)


def test_questions_that_are_not_requests_are_not_tasks():
    ext = extract_rules(parse_transcript("Garv: Will the build be ready by Friday?\nGarv: Riya will it work?"), TEAM, MON)
    assert ext.items == []


def test_shared_assignment_creates_one_item_per_person():
    ext = extract_rules(parse_transcript("Garv: Aman and Riya will prepare the demo by Thursday."), TEAM, MON)
    assert {i.owner for i in ext.items} == {"Aman Sharma", "Riya"} and all(i.due == "2026-10-15" for i in ext.items)


def test_explicit_assign_phrases_and_nicknames():
    ext = extract_rules(parse_transcript("Garv: I'm assigning the database migration to Ams.\nGarv: The icons task is assigned to Karan."), TEAM, MON)
    assert ("Aman Sharma", "I'm assigning the database migration to Ams.") in owners(ext)
    assert any(i.owner == "Karan" and "icons" in i.task for i in ext.items)


def test_a_speaker_outside_the_team_is_flagged_not_dropped():
    ext = extract_rules(parse_transcript("Garv: I'll book the venue tomorrow."), TEAM, MON)
    assert ext.items and ext.items[0].owner == "Garv" and ext.items[0].on_team is False


def test_me_names_the_unlabelled_speaker():
    ext = extract_rules(parse_transcript("I'll book the venue tomorrow."), TEAM, MON, me="Riya")
    assert ext.items[0].owner == "Riya" and ext.items[0].on_team is True


# ---------- Hinglish ----------
def test_hinglish_assignments_and_deadlines():
    ext = extract_rules(parse_transcript(HINGLISH), TEAM, MON, me="Garv")
    aman = by_owner(ext, "Aman Sharma")
    assert aman and "backend" in aman[0].task.lower() and aman[0].due == "2026-10-13"
    riya = by_owner(ext, "Riya")
    assert riya and riya[0].due == "2026-10-16"
    karan = by_owner(ext, "Karan")
    assert karan and karan[0].due == "2026-10-19"
    garv = by_owner(ext, "Garv")
    assert garv and "deck" in garv[0].task.lower() and garv[0].due == "2026-10-14"


def test_vtt_assignment_end_to_end():
    ext = extract_rules(parse_transcript(VTT), TEAM, MON)
    assert any(i.owner == "Karan" and "staging" in i.task and i.due == "2026-10-14" for i in ext.items)
    assert any(i.owner == "Riya" and "release notes" in i.task and i.due == "2026-10-12" for i in ext.items)


def test_a_meeting_with_no_assignments_finds_none():
    ext = extract_rules(parse_transcript("Garv: The weather is nice today and the office looks good."), TEAM, MON)
    assert ext.items == [] and ext.decisions == []


# ---------- model path ----------
def model_json(**over):
    data = {
        "summary": "The team planned the launch.",
        "decisions": ["Use the dark theme"],
        "open_questions": ["Which payment provider?"],
        "action_items": [
            {"owner": "Aman", "task": "Finish the login page", "due": "2026-10-16", "due_text": "by Friday",
             "quote": "Aman, please finish the login page by Friday."},
            {"owner": "Nobody Here", "task": "Invented thing", "due": None, "due_text": None, "quote": "this was never said"},
        ],
    }
    data.update(over)
    return json.dumps(data)


def test_model_output_is_validated_owner_names_and_quotes():
    ext = extract_model(FakeBrain([Decision(final="```json\n" + model_json() + "\n```")]), ENGLISH, TEAM, MON)
    good, bad = ext.items
    assert good.owner == "Aman Sharma" and good.verified is True and good.due == "2026-10-16" and good.source == "model"
    assert bad.owner == UNASSIGNED and bad.verified is False  # unknown owner, quote not in the transcript
    assert ext.summary == "The team planned the launch." and ext.method == "model"


def test_bad_model_output_returns_none():
    for text in ("sorry, I cannot do that", "{not json", "[]", json.dumps({"summary": "", "action_items": []})):
        assert extract_model(FakeBrain([Decision(final=text)]), ENGLISH, TEAM, MON) is None


def test_a_failing_model_returns_none():
    class Boom:
        def decide(self, *a, **k):
            raise RuntimeError("down")

    assert extract_model(Boom(), ENGLISH, TEAM, MON) is None


def test_long_transcripts_are_truncated_for_the_model_with_a_warning():
    seen = {}

    class Spy:
        def decide(self, messages, tools, **k):
            seen["prompt"] = messages[1]["content"]
            return Decision(final=model_json())

    ext = extract_model(Spy(), "Garv: " + "word " * 10000, TEAM, MON)
    assert ext.warnings and len(seen["prompt"]) < 26000


def test_merge_keeps_model_items_and_adds_rule_items_it_missed_without_duplicates():
    rules = extract_rules(parse_transcript(ENGLISH), TEAM, MON)
    model = extract_model(FakeBrain([Decision(final=model_json())]), ENGLISH, TEAM, MON)
    merged = merge(rules, model)
    login = [i for i in merged.items if "login" in i.task.lower() and i.owner == "Aman Sharma"]
    assert len(login) == 1 and login[0].source == "model"
    assert any(i.source == "rules" and i.owner == "Karan" for i in merged.items)  # a rule item the model missed
    assert merged.method == "model+rules" and merge(rules, None) is rules


# ---------- projects, storage, pipeline ----------
@pytest.fixture
def store(tmp_path):
    return MeetingStore(tmp_path / "projects.db", export_root=tmp_path / "projects")


def test_project_creation_rules(store):
    p = store.create_project("Launch Plan", ["Aman Sharma=Aman|Ams", "Riya", "Karan"])
    assert p.slug == "launch-plan" and [m.name for m in p.members] == ["Aman Sharma", "Riya", "Karan"]
    assert p.members[0].aliases == ("Aman", "Ams")
    assert store.get_project("launch-plan").id == p.id == store.get_project(p.id).id == store.get_project("Launch Plan").id
    for bad in (dict(name="", members=["A"]), dict(name="X", members=[]), dict(name="Launch Plan", members=["A"]),
                dict(name="Y", members=["A", "a"])):
        with pytest.raises(MeetingError):
            store.create_project(**bad)
    assert store.add_member(p.id, "Neha").name == "Neha"
    with pytest.raises(MeetingError):
        store.add_member(p.id, "neha")


def test_process_meeting_saves_exports_and_tracks_items(store, tmp_path):
    p = store.create_project("Launch", ["Aman Sharma=Aman", "Riya", "Karan"])
    m = process_meeting(store, p, ENGLISH, title="Launch sync", held_on=MON, me="Garv")
    assert m.title == "Launch sync" and m.method == "rules" and len(m.items) >= 5
    assert store.list_meetings(p.id)[0]["items"] == len(m.items)
    folder = tmp_path / "projects" / "launch"
    md = (folder / "meetings" / f"2026-10-12-launch-sync-{m.id}.md").read_text(encoding="utf-8")
    assert "### Aman Sharma" in md and "login page" in md and '- said: "Aman, please finish' in md and "## Decisions" in md
    assert "### Unassigned" in md and "Open questions" in md
    assert "## Riya" in (folder / "TASKS.md").read_text(encoding="utf-8")

    item = store.list_items(p.id, owner="aman sharma")[0]
    assert store.set_status(item["id"], "done") and store.get_item(item["id"])["status"] == "done"
    assert [i["id"] for i in store.list_items(p.id, status="done")] == [item["id"]]
    assert "- [x]" in tasks_markdown(store, p)
    with pytest.raises(MeetingError):
        store.set_status(item["id"], "weird")

    unassigned = by_owner_items(store, p, UNASSIGNED)[0]
    assert store.reassign(unassigned["id"], "karan") and store.get_item(unassigned["id"])["owner"] == "Karan"
    with pytest.raises(MeetingError):
        store.reassign(unassigned["id"], "Stranger")
    assert store.reassign(999, "Karan") is False and store.set_status(999, "done") is False
    assert store.transcript(m.id).startswith("Garv: Okay everyone")


def by_owner_items(store, p, owner):
    return store.list_items(p.id, owner=owner)


def test_process_meeting_validates_input(store):
    p = store.create_project("V", ["A", "B"])
    with pytest.raises(MeetingError):
        process_meeting(store, p, "   too short ")
    with pytest.raises(MeetingError):
        process_meeting(store, p, "x" * 500_001)


def test_pipeline_uses_the_model_when_given_and_notes_when_it_fails(store):
    p = store.create_project("M", ["Aman Sharma=Aman", "Riya", "Karan"])
    ok = process_meeting(store, p, ENGLISH, brain=FakeBrain([Decision(final=model_json())]), held_on=MON, export=False)
    assert ok.method == "model+rules" and ok.summary == "The team planned the launch."
    bad = process_meeting(store, p, ENGLISH, brain=FakeBrain([Decision(final="nope")]), held_on=MON, export=False)
    assert bad.method == "rules" and any("offline rules" in w for w in bad.warnings)


def test_unverified_items_are_flagged_in_the_markdown(store):
    p = store.create_project("U", ["Aman Sharma=Aman", "Riya", "Karan"])
    m = process_meeting(store, p, ENGLISH, brain=FakeBrain([Decision(final=model_json())]), held_on=MON, export=False)
    assert "unverified" in meeting_markdown(p, m)


def test_parse_member_spec():
    assert parse_member("Aman Sharma=Aman|Ams") == Member("Aman Sharma", ("Aman", "Ams"))
    assert parse_member("  Riya ") == Member("Riya")


def test_a_deadline_belongs_to_its_own_clause_and_tasks_read_as_instructions():
    ext = extract_rules(
        parse_transcript("Garv: Karan will do the QA pass before 20 October and Riya will handle the API docs."), TEAM, MON)
    karan, riya = by_owner(ext, "Karan")[0], by_owner(ext, "Riya")[0]
    assert (karan.task, karan.due) == ("Do the QA pass", "2026-10-20")
    assert (riya.task, riya.due) == ("Handle the API docs.", None)  # did not inherit Karan's deadline


def test_deadline_words_leave_the_task_text_but_not_the_due_date():
    ext = extract_rules(parse_transcript("Garv: Riya, please finish the landing page by Friday.\nGarv: Aman tum backend kal tak kar do."),
                        TEAM, MON)
    assert by_owner(ext, "Riya")[0].task == "Finish the landing page." and by_owner(ext, "Riya")[0].due == "2026-10-16"
    assert by_owner(ext, "Aman Sharma")[0].task == "Backend kar do." and by_owner(ext, "Aman Sharma")[0].due == "2026-10-13"


def test_short_fragments_are_not_open_questions():
    ext = extract_rules(parse_transcript("Garv: We are not sure yet which payment provider fits us.\nGarv: Not sure."), TEAM, MON)
    assert len(ext.questions) == 1 and "payment provider" in ext.questions[0]
