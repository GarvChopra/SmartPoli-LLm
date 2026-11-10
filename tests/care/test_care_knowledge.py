"""Knowledge-base quality: grounding coverage, abstention, consistency, no PII/secrets.

The retrieval harness measures whether the SOURCE CONTENT contains and surfaces the right facts. It does not
measure eGain's own answer quality; that must be tested in eGain's Test AI Agent (docs/test-plan.md).
"""
import json
import math
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KB = ROOT / "egain" / "knowledge"
QA = json.loads((ROOT / "tests" / "care" / "fixtures" / "qa.json").read_text(encoding="utf-8"))
STOP = set("a an the is are do does i my me to of for how what can you your it in on and or if at be this that "
           "with will which have has not no there their about get each when after".split())
THRESHOLD = 0.30


def toks(s: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9']+", s.lower()):
        if w in STOP:
            continue
        out.append(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def chunks() -> list[tuple[str, str]]:
    out = []
    for p in sorted(KB.glob("*.md")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                out.append((p.name, line.strip()))
    return out


CH = chunks()
DF = Counter(w for _, c in CH for w in set(toks(c)))


def idf(w: str) -> float:
    return math.log((len(CH) + 1) / (DF.get(w, 0) + 0.5))


def search(q: str) -> tuple[float, str, str]:
    """Return (recall, source, line) of the best line. Ranking adds a precision bonus; abstention uses recall."""
    qt = set(toks(q))
    best, best_rank = (0.0, "", ""), -1.0
    total = sum(idf(w) for w in qt) or 1.0
    for src, c in CH:
        ct = set(toks(c))
        recall = sum(idf(w) for w in qt & ct) / total
        precision = len(qt & ct) / (len(ct) or 1)  # prefer focused lines
        rank = recall + 0.1 * precision
        if rank > best_rank:
            best, best_rank = (recall, src, c), rank
    return best


def doc_text(name: str) -> str:
    return (KB / name).read_text(encoding="utf-8")


def test_grounded_answer_accuracy():
    failures = []
    for item in QA["answerable"]:
        score, src, chunk = search(item["q"])
        ok = score >= THRESHOLD and item["fact"].lower() in doc_text(src).lower()
        if not ok:
            failures.append((item["q"], round(score, 2), src))
    n = len(QA["answerable"])
    print(f"\nGROUNDING: {n - len(failures)}/{n} passed; failures={failures}")
    assert not failures


def test_unsupported_questions_abstain():
    wrong = []
    for q in QA["unsupported"]:
        score, src, _ = search(q)
        if score >= THRESHOLD:
            wrong.append((q, round(score, 2), src))
    n = len(QA["unsupported"])
    print(f"\nABSTENTION: {n - len(wrong)}/{n} abstained; answered anyway={wrong}")
    assert not wrong


def test_no_conflicting_facts_across_sources():
    text = {p.name: p.read_text(encoding="utf-8") for p in KB.glob("*.md")}
    facts = {
        "support hours": r"(\d+ AM to \d+ PM Pacific)",
        "reminders per day": r"up to (\d+) reminders per day",
        "late reminder": r"up to (\d+) minutes late",
        "edit window": r"edited for (\d+ hours)|Yes, for (\d+ hours)",
    }
    for name, rx in facts.items():
        vals = {m for t in text.values() for g in re.findall(rx, t, re.I) for m in (g if isinstance(g, tuple) else (g,)) if m}
        assert len(vals) == 1, f"conflicting values for {name}: {vals}"


def test_knowledge_has_no_pii_or_secrets():
    for p in KB.glob("*.md"):
        t = p.read_text(encoding="utf-8")
        for email in re.findall(r"[\w.+-]+@[\w.-]+", t):
            assert email.endswith("smartpoli.example"), f"{p.name}: non-demo email {email}"
        for ph in re.findall(r"\b\d{3}-\d{3,4}\b|\b1-\d{3}-\d{3}-\d{4}\b", t):
            assert ph.startswith(("555-01", "1-800-222-1222")), f"{p.name}: unexpected number {ph}"
        assert not re.search(r"(api[_-]?key|password\s*[:=]|secret|token)\s*[:=]", t, re.I)


def test_all_required_topics_have_a_document():
    names = " ".join(p.name for p in KB.glob("*.md"))
    for topic in ["overview", "features", "onboarding", "reminder", "missed-dose", "privacy",
                  "escalation", "limitations", "emergency", "faq"]:
        assert topic in names, topic


def test_mock_capabilities_are_labelled_simulated():
    assert "SIMULATED" in (KB / "01-service-overview.md").read_text(encoding="utf-8")
