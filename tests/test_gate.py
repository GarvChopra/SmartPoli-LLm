import pytest

from r2.gate import Gate, Verdict, parse_grants
from r2.types import Risk, RiskInfo

SAFE = RiskInfo(Risk.SAFE)
SUBMIT = RiskInfo(Risk.CONFIRM, "submit")
POST = RiskInfo(Risk.CONFIRM, "post")
SEND = RiskInfo(Risk.CONFIRM, "send")
DELETE = RiskInfo(Risk.CONFIRM, "delete")
BLOCKED = RiskInfo(Risk.BLOCKED, "format_disk")


def test_safe_is_allowed():
    assert Gate("anything").evaluate(SAFE) is Verdict.ALLOW


@pytest.mark.parametrize("info", [SUBMIT, POST, SEND, DELETE])
def test_confirm_asks_by_default(info):
    assert Gate("do the task").evaluate(info) is Verdict.ASK


def test_blocked_is_denied_even_when_user_says_submit():
    assert Gate("submit it").evaluate(BLOCKED) is Verdict.DENY


def test_unknown_tool_asks():
    assert Gate("anything").evaluate(None) is Verdict.ASK


def test_submit_grant_allows_submit_only():
    g = Gate("fill the form and submit")
    assert g.evaluate(SUBMIT) is Verdict.ALLOW
    assert g.evaluate(POST) is Verdict.ASK
    assert g.evaluate(SEND) is Verdict.ASK


def test_post_as_a_noun_is_not_a_grant():
    assert Gate("fill this post").evaluate(POST) is Verdict.ASK
    assert Gate("yeh post fill kar do").evaluate(POST) is Verdict.ASK


@pytest.mark.parametrize("msg", ["fill it and then post it", "post it", "write it and post"])
def test_post_verb_grants_post(msg):
    assert Gate(msg).evaluate(POST) is Verdict.ALLOW


def test_send_me_is_not_a_grant_but_send_it_is():
    assert Gate("send me a summary").evaluate(SEND) is Verdict.ASK
    assert Gate("write the reply and send it").evaluate(SEND) is Verdict.ALLOW


@pytest.mark.parametrize(
    "msg",
    [
        "don't submit it",
        "do not submit",
        "never submit this",
        "fill it but dont submit",
        "without submitting",
        "fill it but don’t submit",
    ],
)
def test_negated_requests_do_not_grant(msg):
    assert Gate(msg).evaluate(SUBMIT) is Verdict.ASK


def test_delete_is_never_granted():
    assert Gate("delete it and submit").evaluate(DELETE) is Verdict.ASK


def test_grants_do_not_leak_between_tasks():
    Gate("submit")
    assert Gate("hello").evaluate(SUBMIT) is Verdict.ASK


def test_parse_grants_returns_action_types():
    assert parse_grants("please submit") == frozenset({"submit"})
    assert parse_grants("hello") == frozenset()
