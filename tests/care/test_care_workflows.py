import re

import pytest

from walker import ACTIONS, ROOT, WF, run


def test_every_step_target_exists():
    for wid, w in WF["workflows"].items():
        steps = w["steps"]
        assert w["entry"] in steps
        for name, s in steps.items():
            for key in ("next", "yes", "no", "on_success", "on_error", "on_invalid", "goto"):
                t = s.get(key)
                if t is None or (key == "goto" and t.startswith("wf_")):
                    continue
                assert t in steps or t in WF["workflows"], f"{wid}.{name}.{key} -> {t}"
            for t in s.get("choices", {}).values():
                assert t in steps


def test_actions_defined_in_service_and_have_error_paths():
    from r2.care_actions import create_app
    routes = {r.path for r in create_app(api_key="k").routes}
    for a in ACTIONS.values():
        assert a["path"] in routes
    for wid, w in WF["workflows"].items():
        for name, s in w["steps"].items():
            if "action" in s:
                assert s["action"] in ACTIONS and "on_error" in s, f"{wid}.{name}"


def test_pre_chat_minimal_and_routes_every_category():
    f = {x["name"]: x for x in WF["pre_chat"]["fields"]}
    assert f["category"]["required"]
    assert not set(f) & set(WF["pre_chat"]["forbidden_fields"])
    assert set(f["category"]["options"]) == set(WF["pre_chat"]["routing"])


def test_process_workflow_is_connected_to_an_intent_with_examples():
    mapped = {i["workflow"] for i in WF["intents"]}
    assert {"wf_reminder_troubleshoot", "wf_escalation"} <= mapped
    for i in WF["intents"]:
        assert len(i["utterances"]) >= 4 and i["negative"]
        assert not set(map(str.lower, i["utterances"])) & set(map(str.lower, i["negative"]))


def test_two_escalation_options_and_global_rules():
    assert len(WF["workflows"]["wf_escalation"]["steps"]["menu"]["choices"]) >= 2
    rules = " ".join(WF["global_rules"]).lower()
    for must in ("emergency", "diagnose", "untrusted", "simulated", "knowledge base"):
        assert must in rules


def test_emergency_workflow_outputs_real_emergency_guidance(client):
    r = run(client, "wf_emergency")
    assert "911" in r.text and "1-800-222-1222" in r.text and "EMERGENCY_MESSAGE" not in r.text


def test_no_unnecessary_phone_number_checks_in_workflows():
    for wid, w in WF["workflows"].items():
        for name, s in w["steps"].items():
            for field in s.get("ask", []):
                assert "phone" not in field, f"unexpected phone prompt in {wid}.{name}"
            assert "mobile number" not in s.get("ask_yes_no", "").lower(), f"unexpected phone check in {wid}.{name}"


# ---- the one guided workflow: reminder troubleshooting (3 device checks -> ticket) ----
@pytest.mark.parametrize("answers,fix", [
    (["no", "yes"], "allow notifications"),
    (["yes", "no", "yes"], "Do Not Disturb"),
    (["yes", "yes", "no", "yes"], "time zone"),
])
def test_each_fix_resolves_without_ticket(client, answers, fix):
    r = run(client, "wf_reminder_troubleshoot", yes_no=answers)
    assert fix.lower() in r.text.lower() and r.actions == []


def test_all_checks_pass_creates_ticket_with_safe_advice(client):
    r = run(client, "wf_reminder_troubleshoot", yes_no=["yes"] * 3)
    assert r.actions == [("create_ticket", 200)] and "don't skip or change doses" in r.text.lower() and re.search(r"SP-\d{4}", r.text)


def test_fix_did_not_work_creates_ticket(client):
    assert run(client, "wf_reminder_troubleshoot", yes_no=["no", "no"]).actions == [("create_ticket", 200)]


def test_ticket_failure_falls_back_to_human_options(client):
    r = run(client, "wf_reminder_troubleshoot", yes_no=["yes"] * 3, faults={"create_ticket"}, ctx={"choice": "email"})
    assert ("create_ticket", 503) in r.actions and "wf_escalation.menu" in r.path and "help@smartpoli.example" in r.text


def test_repeat_run_does_not_duplicate_ticket(client):
    a = run(client, "wf_reminder_troubleshoot", yes_no=["yes"] * 3)
    b = run(client, "wf_reminder_troubleshoot", yes_no=["yes"] * 3)
    assert re.search(r"SP-\d{4}", a.text).group() == re.search(r"SP-\d{4}", b.text).group()


# ---- two escalation options (callback + email) ----
def test_escalation_callback_without_phone_prompt(client):
    r = run(client, "wf_escalation", {"window": "evening"}, ctx={"choice": "callback"})
    assert r.actions == [("request_callback", 200)] and re.search(r"CB-\d{4}", r.text)


def test_callback_rejects_invalid_window_then_accepts_valid(client):
    r = run(client, "wf_escalation", {"window": ["midnight", "morning"]}, ctx={"choice": "callback"})
    assert "invalid window" in r.text and r.actions == [("request_callback", 200)]


def test_escalation_email(client):
    r = run(client, "wf_escalation", ctx={"choice": "email"})
    assert r.actions == [] and "help@smartpoli.example" in r.text


def test_callback_failure_gives_contact_fallback(client):
    r = run(client, "wf_escalation", {"window": "morning"}, faults={"request_callback"}, ctx={"choice": "callback"})
    assert "couldn't complete that" in r.text and "555-0100" in r.text


def test_workflow_spec_contains_no_secrets_or_real_contacts():
    raw = (ROOT / "config" / "workflows.json").read_text(encoding="utf-8")
    assert not re.search(r"sk-|api[_-]?key\s*[:=]|bearer", raw, re.I)
    for email in re.findall(r"[\w.+-]+@[\w.-]+", raw):
        assert email.endswith("smartpoli.example")
