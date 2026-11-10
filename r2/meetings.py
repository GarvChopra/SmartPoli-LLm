"""Meeting summarizer: turns a transcript into a summary, decisions, open questions and action items with an
owner, a deadline and the exact words they came from, and saves it all to a project.

Two extractors work together, so it is useful with no API key and better with one:

  * rules      deterministic, offline. Finds "<Name> will/should/please ...", "assign X to <Name>", "I'll ...",
               and Hinglish ("Rahul tum backend kal tak kar do"), plus deadlines such as "by Friday", "kal tak",
               "in 3 days", "20 October".
  * model      the configured brain (for example Nemotron on Nebius Token Factory) writes the summary and finds
               items the rules miss. Its answer is checked: owners must be people on the team, and each item must
               quote words that really appear in the transcript, or it is marked "unverified".

Nothing is invented silently: an item with no clear owner is saved as "Unassigned" so you can fix it."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_TRANSCRIPT = 500_000  # characters accepted
MIN_TRANSCRIPT = 20
MAX_LLM_CHARS = 24_000  # more than this is not sent to the model; the rules still read all of it
UNASSIGNED = "Unassigned"


# --------------------------------------------------------------------------- people
@dataclass(frozen=True)
class Member:
    name: str
    aliases: tuple[str, ...] = ()

    def all_names(self) -> list[str]:
        names = {self.name, self.name.split()[0], *self.aliases}
        return sorted((n.strip() for n in names if n.strip()), key=len, reverse=True)

    def pattern(self) -> re.Pattern:
        return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in self.all_names()) + r")(?!\w)", re.I)


def parse_member(spec: str) -> Member:
    """`Aman Sharma=Aman|Ams` -> name 'Aman Sharma' with two nicknames. A plain `Riya` is just a name."""
    name, _, rest = spec.partition("=")
    return Member(name.strip(), tuple(a.strip() for a in rest.split("|") if a.strip()))


def find_member(members: list[Member], text: str) -> Member | None:
    text = (text or "").strip()
    for m in members:
        if text.lower() in {n.lower() for n in m.all_names()}:
            return m
    return None


# --------------------------------------------------------------------------- transcript
@dataclass(frozen=True)
class Turn:
    speaker: str | None
    text: str


_TIMELINE = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?\s*(?:-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?.*)?$")
_STAMP_PREFIX = re.compile(r"^\s*[\[(]?(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?[\])]?\s*[-–]?\s*")
_VOICE_TAG = re.compile(r"<v\s+([^>]+)>")
_ANY_TAG = re.compile(r"</?[^>]+>")
_SPEAKER_LINE = re.compile(r"^([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s*:\s+(\S.*)$")
_NOT_NAMES = {"Note", "Notes", "Action", "Agenda", "Decision", "Summary", "Todo", "Topic", "Update", "Reminder", "Important"}


def parse_transcript(raw: str) -> list[Turn]:
    """Plain text, `Name: text`, `[00:12] Name: text`, Google Meet captions, WebVTT and SRT."""
    turns: list[Turn] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip().lstrip("﻿")
        if not line or line.upper().startswith(("WEBVTT", "NOTE ", "STYLE", "REGION")) or line.isdigit():
            continue
        if _TIMELINE.match(line):
            continue
        speaker = None
        tagged = _VOICE_TAG.search(line)
        if tagged:
            speaker = tagged.group(1).strip()
        line = _ANY_TAG.sub("", line).strip()
        line = _STAMP_PREFIX.sub("", line).strip()
        if not line:
            continue
        if speaker is None:
            m = _SPEAKER_LINE.match(line)
            if m and m.group(1) not in _NOT_NAMES:
                speaker, line = m.group(1), m.group(2).strip()
        if turns and (speaker is None or speaker == turns[-1].speaker):
            previous = turns[-1]
            # a caption wrapped mid-sentence continues with a space; otherwise keep the line break
            wrapped = not previous.text.rstrip().endswith((".", "?", "!", "।")) and line[:1].islower()
            turns[-1] = Turn(previous.speaker, previous.text + (" " if wrapped else "\n") + line)
        else:
            turns.append(Turn(speaker, line))
    return turns


_SENTENCE = re.compile(r"(?<=[.!?।])\s+|\n+")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s and s.strip()]


def _norm(text: str) -> str:
    return re.sub(r"[^\wऀ-ॿ]+", " ", text.lower()).strip()


# --------------------------------------------------------------------------- deadlines
_WEEKDAYS = {
    "monday": 0, "mon": 0, "somvar": 0, "somwar": 0, "tuesday": 1, "tue": 1, "tues": 1, "mangalvar": 1, "mangalwar": 1,
    "wednesday": 2, "wed": 2, "budhvar": 2, "budhwar": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "guruvar": 3, "guruwar": 3, "brihaspativar": 3, "veervar": 3, "friday": 4, "fri": 4, "shukravar": 4, "shukrawar": 4,
    "saturday": 5, "sat": 5, "shanivar": 5, "shaniwar": 5, "sunday": 6, "sun": 6, "ravivar": 6, "raviwar": 6, "itwar": 6,
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_MON = "|".join(_MONTHS)
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))


def _to_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        return dt.date.fromisoformat(value.strip()[:10])
    return dt.date.today()


def _safe_date(year: int, month: int, day: int, held: dt.date) -> dt.date | None:
    try:
        d = dt.date(year, month, day)
    except ValueError:
        return None
    return d if d >= held - dt.timedelta(days=1) else _safe_date(year + 1, month, day, held) if year == held.year else None


def resolve_due(text: str, held_on) -> tuple[str | None, str | None]:
    """Find a deadline in a sentence. Returns (ISO date or None, the words that gave it)."""
    held = _to_date(held_on)
    t = text.lower()

    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON})[a-z]*\b", t) or None
    if m:
        d = _safe_date(held.year, _MONTHS[m.group(2)], int(m.group(1)), held)
        return (d.isoformat() if d else None), m.group(0)
    m = re.search(rf"\b({_MON})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if m:
        d = _safe_date(held.year, _MONTHS[m.group(1)], int(m.group(2)), held)
        return (d.isoformat() if d else None), m.group(0)
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31:
        year = int(m.group(3)) if m.group(3) else held.year
        year += 2000 if year < 100 else 0
        d = _safe_date(year, int(m.group(2)), int(m.group(1)), held)
        return (d.isoformat() if d else None), m.group(0)

    def at(days: int, match: re.Match) -> tuple[str, str]:
        return (held + dt.timedelta(days=days)).isoformat(), match.group(0)

    if m := re.search(r"\b(?:day after tomorrow|parso|parson)\b", t):
        return at(2, m)
    if m := re.search(r"\b(?:tomorrow|kal)\b", t):
        return at(1, m)
    if m := re.search(r"\b(?:in|within)\s+(\d{1,2})\s+days?\b", t) or re.search(r"\b(\d{1,2})\s+din\b", t):
        return at(int(m.group(1)), m)
    if m := re.search(rf"\bnext\s+({_WD})\b", t):
        ahead = (_WEEKDAYS[m.group(1)] - held.weekday()) % 7 or 7
        return at(ahead + (7 if ahead <= 3 and held.weekday() < _WEEKDAYS[m.group(1)] else 0), m)
    if m := re.search(rf"\b(?:by|before|on|till|until|tak|this)?\s*({_WD})\b", t):
        ahead = (_WEEKDAYS[m.group(1)] - held.weekday()) % 7 or 7
        return at(ahead, re.search(rf"\b{m.group(1)}\b", t))
    if m := re.search(r"\b(?:next week|agle hafte|agle week|agle haphte)\b", t):
        return at(7, m)
    if m := re.search(r"\b(?:this week|end of (?:the )?week|eow|is hafte|is week)\b", t):
        return at((4 - held.weekday()) % 7, m)
    if m := re.search(r"\b(?:today|tonight|eod|end of (?:the )?day|aaj|aaj raat|shaam tak|subah tak)\b", t):
        return at(0, m)
    return None, None


# --------------------------------------------------------------------------- rules extractor
@dataclass
class ItemDraft:
    owner: str
    task: str
    due: str | None = None
    due_text: str | None = None
    quote: str = ""
    verified: bool = True
    source: str = "rules"  # "rules" | "model"
    on_team: bool = True


@dataclass
class Extraction:
    summary: str = ""
    decisions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    items: list[ItemDraft] = field(default_factory=list)
    method: str = "rules"
    warnings: list[str] = field(default_factory=list)


_CUE = re.compile(
    r"(?<!\w)(?:will|shall|should|needs?\s+to|has\s+to|have\s+to|must|going\s+to|gonna|can\s+you|could\s+you|would\s+you|"
    r"please|take\s+(?:care|charge|over|up)|handle|owns?|responsible|working\s+on|assign(?:ed|ing)?|taking|todo|action\s+item|"
    r"kar\s*do|kar\s*dena|kar\s*dijiye|kar\s*dijiyega|karo|karna|kar\s*lena|kar\s*dunga|kar\s*dungi|kar\s*lunga|kar\s*lungi|"
    r"karunga|karungi|karega|karegi|kar\s*dega|kar\s*degi|dekh\s*lena|dekh\s*lo|dekhna|bana\s*do|bana\s*dena|banao|"
    r"bhej\s*do|bhej\s*dena|bhejna|sambhal\s*lena|sambhalo|zimmedari|zimmedaari|likh\s*do|likhna|submit\s*kar|test\s*kar|"
    r"fix\s*kar|deploy\s*kar|review\s*kar|update\s*kar|complete\s*kar|finish\s*kar|ready\s*kar|handle\s*kar|dena|lena)(?!\w)",
    re.I,
)
_STRONG_NO_OWNER = re.compile(r"(?<!\w)(?:need\s+to|needs\s+to|has\s+to|have\s+to|must|todo|action\s+item|someone\s+should|"
                              r"anyone\s+can|koi\s+\w+\s+kar)(?!\w)", re.I)
_SELF = re.compile(r"(?<!\w)(?:i'll|i\s+will|i\s+can|i\s+am\s+going\s+to|i'm\s+going\s+to|let\s+me)(?!\w)", re.I)
_SELF_HI = re.compile(r"(?<!\w)(?:main|mai)(?!\w).{0,40}?(?<!\w)(?:karunga|karungi|kar\s*dunga|kar\s*dungi|kar\s*lunga|kar\s*lungi|"
                      r"dekh\s*lunga|dekh\s*lungi|dekhunga|dekhungi|bhej\s*dunga|bhej\s*dungi|bana\s*dunga|bana\s*dungi|"
                      r"bana\s*lunga|bana\s*lungi)(?!\w)", re.I)
_ASSIGN_TO = re.compile(r"(?<!\w)assign(?:ed|ing)?\b.{0,60}?\bto\b\s*(?P<name>[A-Za-z][\w.'-]*(?:\s+[A-Z][\w.'-]*)?)", re.I)
_ASSIGNED_TO = re.compile(r"(?<!\w)(?:is|has\s+been|was|are)\s+assigned\s+to\s+(?P<name>[A-Za-z][\w.'-]*)", re.I)
_DECISION = re.compile(r"(?<!\w)(?:decided|decision|we(?:'ll|\s+will)\s+go\s+with|going\s+with|let'?s\s+go\s+with|finali[sz]ed?|agreed|"
                       r"approved|settled\s+on|tay\s+kiya|tay\s+hua|decide\s+kiya|final\s+hai|final\s+kar\s+diya)(?!\w)", re.I)
_OPEN = re.compile(r"(?<!\w)(?:open\s+question|pending|not\s+sure|tbd|to\s+be\s+decided|figure\s+out|decide\s+later|"
                   r"pata\s+nahi|baad\s+mein\s+dekhenge|unclear|blocked)(?!\w)", re.I)
_DUE_FOLLOWUP = re.compile(r"(?:we\s+need\s+(?:it|this|that)|need\s+(?:it|this|that)|it(?:'s|\s+is)\s+due|due|deadline|"
                           r"it\s+should\s+be\s+(?:done|ready)|by|before|isse?\s+\w+\s+tak|ye\s+\w+\s+tak)(?!\w)", re.I)
_LEAD = re.compile(r"^(?:ok(?:ay)?|so|and|also|then|right|alright|well|theek\s+hai|acha|achha|toh|to)\b[,\s]*", re.I)
_ADDRESS = re.compile(r"^(?:you|tum|tu|aap|please|can you|could you|would you|will you)\b[,\s]*", re.I)
_WELL_KNOWN_STOPWORDS = set(
    "the a an and or of to in on for with is are was were be been it this that we you i they he she at as by from "
    "have has had will would should can could not so if then than there their our your my me us do does did just "
    "ok okay yes no also about into over up out all any some more very hai hain ho tha thi the ka ki ke ko se mein me "
    "aur toh to bhi nahi kya yeh ye woh wo kar karo kuch ek jo par pe".split()
)


_MODAL = re.compile(
    r"^(?:i'll|i\s+will|i\s+can|i'm\s+going\s+to|i\s+am\s+going\s+to|let\s+me|is\s+going\s+to|are\s+going\s+to|going\s+to|gonna|"
    r"will|shall|should|must|needs?\s+to|has\s+to|have\s+to|can|could|would|to)\s+",
    re.I,
)


def _clean_task(sentence: str, names: list[str], due_text: str | None = None) -> str:
    """'Karan will do the QA pass before 20 October.' -> 'Do the QA pass.' The deadline is stored separately."""
    text = _LEAD.sub("", sentence.strip())
    for name in names:
        text = re.sub(rf"^{re.escape(name)}\b[,:]?\s*", "", text, flags=re.I)
    text = _ADDRESS.sub("", _LEAD.sub("", text)).strip(" ,;-")
    text = _MODAL.sub("", text)
    if due_text:
        text = re.sub(rf"\s*(?:\b(?:by|before|on|till|until|within|in)\s+)?{re.escape(due_text)}(?:\s+tak)?\b", "", text, count=1, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ,;-")
    text = re.sub(r"\s+([.?!])", r"\1", text)
    return (text[:1].upper() + text[1:])[:240] if text else sentence[:240]


def _clauses(sentence: str, name_alt: str) -> list[str]:
    """'Priya will do design and Karan will do QA' -> two clauses. Only split where the left part already
    has an assignment cue, so 'Aman and Riya will ...' stays together."""
    if not name_alt:
        return [sentence]
    parts = re.split(rf"(;|,?\s+(?:and|aur)\s+(?=(?:{name_alt})(?!\w)))", sentence, flags=re.I)
    out: list[str] = []
    current = parts[0]
    for i in range(1, len(parts), 2):
        sep, following = parts[i], parts[i + 1]
        if _CUE.search(current):
            out.append(current)
            current = following
        else:
            current = current + sep + following
    out.append(current)
    return [c.strip() for c in out if c.strip()]


def _assign(clause, sentence, speaker, is_question, members, patterns, held, items, seen_items, follow="") -> None:
    cue = _CUE.search(clause)
    mentions = sorted(((m.start(), mem) for mem, pat in patterns for m in pat.finditer(clause)), key=lambda x: x[0])
    owners: list = []
    addressed_request = re.search(r"(?<!\w)(?:can|could|would|will)\s+you(?!\w)", clause, re.I)
    if is_question and not addressed_request:
        return
    if (assigned := _ASSIGN_TO.search(clause) or _ASSIGNED_TO.search(clause)):
        target = find_member(members, assigned.group("name").split()[0]) or find_member(members, assigned.group("name"))
        if target:
            owners = [target]
    if not owners and cue and mentions:
        before = [mem for pos, mem in mentions if pos < cue.start()]
        owners = list(dict.fromkeys(before or [mentions[0][1]]))
    if not owners and (_SELF.search(clause) or _SELF_HI.search(clause)) and speaker:
        target = find_member(members, speaker) or find_member(members, speaker.split()[0])
        owners = [target] if target else [speaker]
    if not owners and cue and _STRONG_NO_OWNER.search(clause):
        owners = [UNASSIGNED]
    if not owners:
        return
    due, due_text = resolve_due(clause, held)  # only this clause's own deadline: "Karan ... before 20 Oct and Riya ..."
    if due is None and follow and _DUE_FOLLOWUP.match(follow):
        due, due_text = resolve_due(follow, held)
    all_names = [n for m in members for n in m.all_names()]
    task = _clean_task(clause, all_names, due_text if due and due_text and due_text.lower() in clause.lower() else None)
    for owner in owners:
        name = owner.name if isinstance(owner, Member) else owner
        key = (name, _norm(task))
        if key in seen_items:
            continue
        seen_items.add(key)
        items.append(ItemDraft(
            owner=name, task=task, due=due, due_text=due_text, quote=sentence[:300],
            on_team=isinstance(owner, Member) or owner == UNASSIGNED, source="rules",
        ))


def extract_rules(turns: list[Turn], members: list[Member], held_on, me: str | None = None) -> Extraction:
    held = _to_date(held_on)
    patterns = [(m, m.pattern()) for m in members]
    name_alt = "|".join(re.escape(n) for m in members for n in m.all_names())
    items: list[ItemDraft] = []
    decisions: list[str] = []
    questions: list[str] = []
    seen_items: set[tuple[str, str]] = set()

    for turn in turns:
        speaker = turn.speaker or me
        turn_sentences = sentences(turn.text)
        for index, sentence in enumerate(turn_sentences):
            words = len(sentence.split())
            is_question = sentence.rstrip().endswith("?")
            if _DECISION.search(sentence) and words >= 4 and not is_question and sentence not in decisions:
                decisions.append(sentence[:300])
            if ((_OPEN.search(sentence) and words >= 4) or (is_question and words >= 6)) and sentence not in questions:
                questions.append(sentence[:300])

            is_question_sentence = is_question
            for clause in _clauses(sentence, name_alt):
                _assign(clause, sentence, speaker, is_question_sentence, members, patterns, held, items, seen_items,
                        turn_sentences[index + 1] if index + 1 < len(turn_sentences) else "")

    return Extraction(
        summary=_extractive_summary(turns), decisions=decisions[:10], questions=questions[:8], items=items, method="rules",
    )


def _extractive_summary(turns: list[Turn], count: int = 5) -> str:
    """A plain extractive summary: the sentences that carry the most frequent meaningful words."""
    all_sentences = [s for t in turns for s in sentences(t.text) if 40 <= len(s) <= 300]
    if not all_sentences:
        return " ".join(t.text for t in turns)[:400]
    freq: dict[str, int] = {}
    for s in all_sentences:
        for w in _norm(s).split():
            if len(w) > 3 and w not in _WELL_KNOWN_STOPWORDS:
                freq[w] = freq.get(w, 0) + 1

    def score(s: str) -> float:
        ws = [w for w in _norm(s).split() if len(w) > 3 and w not in _WELL_KNOWN_STOPWORDS]
        return sum(freq.get(w, 0) for w in ws) / (len(ws) ** 0.5 or 1)

    top = sorted(range(len(all_sentences)), key=lambda i: score(all_sentences[i]), reverse=True)[:count]
    return " ".join(all_sentences[i] for i in sorted(top))


# --------------------------------------------------------------------------- model extractor
_SYSTEM = (
    "You write meeting minutes. Reply with ONLY one JSON object and nothing else. "
    "Use only what is actually said in the transcript; never invent tasks, people or dates."
)


def _prompt(transcript: str, members: list[Member], held: dt.date) -> str:
    team = ", ".join(m.name for m in members)
    return (
        f"Meeting date: {held.isoformat()} ({held.strftime('%A')}). Team members: {team}.\n"
        "The transcript may mix English and Hindi/Hinglish. Summarize the meeting and list the work that was assigned.\n"
        'JSON shape: {"summary": "3-6 sentences", "decisions": ["..."], "open_questions": ["..."], '
        '"action_items": [{"owner": "one of the team names, or Unassigned", "task": "short imperative task", '
        '"due": "YYYY-MM-DD or null", "due_text": "the deadline words or null", '
        '"quote": "the exact words from the transcript that assign it"}]}\n'
        "Rules: owner must be exactly one of the team names, otherwise \"Unassigned\". If two people share a task, make one "
        "item per person. Resolve words like tomorrow or Friday from the meeting date. Do not add an item unless someone "
        "was actually asked or promised to do it.\n\nTRANSCRIPT:\n" + transcript
    )


def _json_object(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def extract_model(brain, transcript: str, members: list[Member], held_on) -> Extraction | None:
    """Ask the brain for minutes and check them. Returns None on any failure so the rules result stands alone."""
    held = _to_date(held_on)
    warnings: list[str] = []
    body = transcript
    if len(body) > MAX_LLM_CHARS:
        body = body[:MAX_LLM_CHARS]
        warnings.append(f"Only the first {MAX_LLM_CHARS} characters were sent to the model; the rules read the whole transcript.")
    try:
        decision = brain.decide(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": _prompt(body, members, held)}], []
        )
    except Exception:
        return None
    data = _json_object(decision.final or "")
    if data is None:
        return None

    norm_transcript = _norm(transcript)
    items: list[ItemDraft] = []
    for raw in data.get("action_items") or []:
        if not isinstance(raw, dict) or not str(raw.get("task", "")).strip():
            continue
        member = find_member(members, str(raw.get("owner", ""))) or find_member(members, str(raw.get("owner", "")).split(" ")[0])
        owner = member.name if member else UNASSIGNED
        quote = str(raw.get("quote") or "").strip()
        verified = bool(quote) and _norm(quote) in norm_transcript
        due_text = str(raw["due_text"]).strip() if raw.get("due_text") else None
        due = None
        try:
            due = _to_date(str(raw["due"])).isoformat() if raw.get("due") else None
        except ValueError:
            due = None
        if due is None and due_text:
            due, _ = resolve_due(due_text, held)
        items.append(ItemDraft(owner=owner, task=str(raw["task"]).strip()[:240], due=due, due_text=due_text,
                               quote=quote[:300], verified=verified, source="model"))

    def texts(key: str) -> list[str]:
        return [str(x).strip()[:300] for x in (data.get(key) or []) if str(x).strip()][:10]

    summary = str(data.get("summary") or "").strip()
    if not summary and not items:
        return None
    return Extraction(summary=summary, decisions=texts("decisions"), questions=texts("open_questions"),
                      items=items, method="model", warnings=warnings)


def _tokens(text: str) -> set[str]:
    return {w for w in _norm(text).split() if w not in _WELL_KNOWN_STOPWORDS and len(w) > 2}


def merge(rules: Extraction, model: Extraction | None) -> Extraction:
    """Model items first; add any rule-found item the model missed (same owner and task not already present)."""
    if model is None:
        return rules
    items = list(model.items)
    for r in rules.items:
        duplicate = False
        for m in model.items:
            a, b = _tokens(r.task), _tokens(m.task)
            overlap = len(a & b) / max(1, len(a | b))
            if (m.owner == r.owner and overlap >= 0.35) or (r.quote and _norm(r.quote) == _norm(m.quote)):
                duplicate = True
                if not m.due and r.due:
                    m.due, m.due_text = r.due, r.due_text
                break
        if not duplicate:
            items.append(r)
    return Extraction(
        summary=model.summary or rules.summary,
        decisions=model.decisions or rules.decisions,
        questions=model.questions or rules.questions,
        items=items, method="model+rules", warnings=model.warnings,
    )


# --------------------------------------------------------------------------- storage
_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS meetings(
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, title TEXT NOT NULL, held_on TEXT NOT NULL,
    transcript TEXT NOT NULL, summary TEXT NOT NULL, decisions TEXT NOT NULL, questions TEXT NOT NULL,
    method TEXT NOT NULL, warnings TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id INTEGER NOT NULL, project_id INTEGER NOT NULL, owner TEXT NOT NULL,
    task TEXT NOT NULL, due TEXT, due_text TEXT, quote TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'rules', on_team INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'open',
    created REAL NOT NULL, done_at REAL);
"""


class MeetingError(ValueError):
    pass


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:48] or "project"


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    slug: str
    members: tuple[Member, ...]


@dataclass(frozen=True)
class Meeting:
    id: int
    project_id: int
    title: str
    held_on: str
    summary: str
    decisions: tuple[str, ...]
    questions: tuple[str, ...]
    method: str
    warnings: tuple[str, ...]
    items: tuple[dict, ...]


class MeetingStore:
    def __init__(self, path, export_root=None):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self.export_root = Path(export_root) if export_root else Path(path).parent / "projects"

    # ---- projects ----
    def create_project(self, name: str, members: list[str | Member]) -> Project:
        name = (name or "").strip()
        if not name or len(name) > 80:
            raise MeetingError("a project needs a name of up to 80 characters")
        team = [m if isinstance(m, Member) else parse_member(m) for m in members]
        team = [m for m in team if m.name]
        if not team or len(team) > 20:
            raise MeetingError("add the team members (for example: Aman, Riya, Karan)")
        if len({m.name.lower() for m in team}) != len(team):
            raise MeetingError("two team members have the same name")
        slug = slugify(name)
        with self._lock:
            if self._db.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                raise MeetingError(f"a project called '{name}' already exists")
            pid = self._db.execute("INSERT INTO projects(name, slug, created) VALUES (?,?,?)", (name, slug, time.time())).lastrowid
            for m in team:
                self._db.execute("INSERT INTO members(project_id, name, aliases) VALUES (?,?,?)", (pid, m.name, "|".join(m.aliases)))
            self._db.commit()
        return self.get_project(pid)

    def _members(self, project_id: int) -> tuple[Member, ...]:
        rows = self._db.execute("SELECT name, aliases FROM members WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()
        return tuple(Member(r["name"], tuple(a for a in r["aliases"].split("|") if a)) for r in rows)

    def get_project(self, ref) -> Project | None:
        with self._lock:
            if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
                row = self._db.execute("SELECT * FROM projects WHERE id = ?", (int(ref),)).fetchone()
            else:
                row = self._db.execute("SELECT * FROM projects WHERE slug = ? OR lower(name) = lower(?)",
                                       (slugify(str(ref)), str(ref))).fetchone()
            return Project(row["id"], row["name"], row["slug"], self._members(row["id"])) if row else None

    def list_projects(self) -> list[Project]:
        with self._lock:
            return [Project(r["id"], r["name"], r["slug"], self._members(r["id"]))
                    for r in self._db.execute("SELECT * FROM projects ORDER BY id").fetchall()]

    def add_member(self, project_id: int, spec: str) -> Member:
        member = parse_member(spec)
        project = self.get_project(project_id)
        if project is None or not member.name:
            raise MeetingError("no such project, or empty name")
        if any(m.name.lower() == member.name.lower() for m in project.members):
            raise MeetingError(f"{member.name} is already on the team")
        with self._lock:
            self._db.execute("INSERT INTO members(project_id, name, aliases) VALUES (?,?,?)",
                             (project_id, member.name, "|".join(member.aliases)))
            self._db.commit()
        return member

    # ---- meetings ----
    def save_meeting(self, project: Project, title: str, held_on: dt.date, transcript: str, ext: Extraction) -> Meeting:
        with self._lock:
            mid = self._db.execute(
                "INSERT INTO meetings(project_id, title, held_on, transcript, summary, decisions, questions, method, warnings, created)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (project.id, title, held_on.isoformat(), transcript, ext.summary, json.dumps(ext.decisions),
                 json.dumps(ext.questions), ext.method, json.dumps(ext.warnings), time.time()),
            ).lastrowid
            for it in ext.items:
                self._db.execute(
                    "INSERT INTO items(meeting_id, project_id, owner, task, due, due_text, quote, verified, source, on_team, created)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, project.id, it.owner, it.task, it.due, it.due_text, it.quote, int(it.verified), it.source,
                     int(it.on_team), time.time()),
                )
            self._db.commit()
        return self.get_meeting(mid)

    def get_meeting(self, meeting_id: int) -> Meeting | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            if r is None:
                return None
            items = tuple(self._item_dict(i) for i in self._db.execute(
                "SELECT * FROM items WHERE meeting_id = ? ORDER BY id", (meeting_id,)).fetchall())
            return Meeting(r["id"], r["project_id"], r["title"], r["held_on"], r["summary"], tuple(json.loads(r["decisions"])),
                           tuple(json.loads(r["questions"])), r["method"], tuple(json.loads(r["warnings"])), items)

    def list_meetings(self, project_id: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT m.id, m.title, m.held_on, m.method, (SELECT COUNT(*) FROM items i WHERE i.meeting_id = m.id) AS items "
                "FROM meetings m WHERE m.project_id = ? ORDER BY m.held_on DESC, m.id DESC", (project_id,)).fetchall()
            return [dict(r) for r in rows]

    def transcript(self, meeting_id: int) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT transcript FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            return r["transcript"] if r else None

    # ---- action items ----
    @staticmethod
    def _item_dict(r) -> dict:
        return {"id": r["id"], "meeting_id": r["meeting_id"], "project_id": r["project_id"], "owner": r["owner"],
                "task": r["task"], "due": r["due"], "due_text": r["due_text"], "quote": r["quote"],
                "verified": bool(r["verified"]), "source": r["source"], "on_team": bool(r["on_team"]),
                "status": r["status"]}

    def list_items(self, project_id: int, owner: str | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM items WHERE project_id = ?", [project_id]
        if owner:
            sql, args = sql + " AND lower(owner) = lower(?)", args + [owner]
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY (due IS NULL), due, id", args).fetchall()
            return [self._item_dict(r) for r in rows]

    def set_status(self, item_id: int, status: str) -> bool:
        if status not in ("open", "done"):
            raise MeetingError("status must be open or done")
        with self._lock:
            cur = self._db.execute("UPDATE items SET status = ?, done_at = ? WHERE id = ?",
                                   (status, time.time() if status == "done" else None, item_id))
            self._db.commit()
            return cur.rowcount > 0

    def reassign(self, item_id: int, owner: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT project_id FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return False
            member = find_member(list(self._members(row["project_id"])), owner) if owner != UNASSIGNED else None
            if owner != UNASSIGNED and member is None:
                raise MeetingError(f"{owner} is not on this project's team")
            self._db.execute("UPDATE items SET owner = ?, on_team = 1 WHERE id = ?", (member.name if member else UNASSIGNED, item_id))
            self._db.commit()
            return True

    def get_item(self, item_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            return self._item_dict(r) if r else None


# --------------------------------------------------------------------------- the whole pipeline
def process_meeting(
    store: MeetingStore, project: Project, transcript: str, brain=None, title: str | None = None,
    held_on=None, me: str | None = None, export: bool = True,
) -> Meeting:
    transcript = (transcript or "").strip()
    if len(transcript) < MIN_TRANSCRIPT:
        raise MeetingError("the transcript is empty or too short to summarize")
    if len(transcript) > MAX_TRANSCRIPT:
        raise MeetingError(f"the transcript is too long (limit {MAX_TRANSCRIPT} characters)")
    held = _to_date(held_on)
    turns = parse_transcript(transcript)
    members = list(project.members)
    rules = extract_rules(turns, members, held, me)
    model = extract_model(brain, "\n".join(f"{t.speaker + ': ' if t.speaker else ''}{t.text}" for t in turns), members, held) if brain else None
    ext = merge(rules, model)
    if brain is not None and model is None:
        ext.warnings.append("The model's answer could not be used, so this summary comes from the offline rules.")
    title = (title or "").strip()[:100] or f"Meeting {held.isoformat()}"
    meeting = store.save_meeting(project, title, held, transcript, ext)
    if export:
        export_meeting(store, project, meeting)
    return meeting


# --------------------------------------------------------------------------- markdown export
def _due_label(item: dict) -> str:
    if item["due"]:
        return item["due"] + (f" ({item['due_text']})" if item["due_text"] else "")
    return item["due_text"] or "no deadline"


def meeting_markdown(project: Project, meeting: Meeting) -> str:
    team = ", ".join(m.name for m in project.members)
    out = [f"# {meeting.title}", "", f"Project: **{project.name}** · Date: {meeting.held_on} · Team: {team} · Made by: {meeting.method}", ""]
    for w in meeting.warnings:
        out.append(f"> Note: {w}")
    out += ["", "## Summary", "", meeting.summary or "(no summary)", ""]
    if meeting.decisions:
        out += ["## Decisions", ""] + [f"- {d}" for d in meeting.decisions] + [""]
    out += ["## Action items", ""]
    if not meeting.items:
        out += ["No assignments were found.", ""]
    for owner in [m.name for m in project.members] + [UNASSIGNED]:
        mine = [i for i in meeting.items if i["owner"] == owner]
        if mine:
            out += [f"### {owner}", ""]
            for i in mine:
                flag = "" if i["verified"] else " *(unverified: check the quote)*"
                out.append(f"- [{'x' if i['status'] == 'done' else ' '}] {i['task']} — due: {_due_label(i)}{flag}")
                if i["quote"]:
                    out.append(f'  - said: "{i["quote"]}"')
            out.append("")
    others = [i for i in meeting.items if i["owner"] not in {m.name for m in project.members} | {UNASSIGNED}]
    if others:
        out += ["### Not on the team list", ""] + [f"- {i['owner']}: {i['task']} — due: {_due_label(i)}" for i in others] + [""]
    if meeting.questions:
        out += ["## Open questions", ""] + [f"- {q}" for q in meeting.questions] + [""]
    return "\n".join(out).rstrip() + "\n"


def tasks_markdown(store: MeetingStore, project: Project) -> str:
    items = store.list_items(project.id)
    out = [f"# {project.name}: tasks", ""]
    for owner in [m.name for m in project.members] + [UNASSIGNED]:
        mine = [i for i in items if i["owner"] == owner]
        if mine:
            out += [f"## {owner}", ""] + [
                f"- [{'x' if i['status'] == 'done' else ' '}] #{i['id']} {i['task']} — due: {_due_label(i)}" for i in mine] + [""]
    if len(out) == 2:
        out.append("No tasks yet.")
    return "\n".join(out).rstrip() + "\n"


def export_meeting(store: MeetingStore, project: Project, meeting: Meeting) -> Path:
    folder = store.export_root / project.slug
    (folder / "meetings").mkdir(parents=True, exist_ok=True)
    path = folder / "meetings" / f"{meeting.held_on}-{slugify(meeting.title)}-{meeting.id}.md"
    path.write_text(meeting_markdown(project, store.get_meeting(meeting.id)), encoding="utf-8")
    (folder / "TASKS.md").write_text(tasks_markdown(store, project), encoding="utf-8")
    return path


def refresh_exports(store: MeetingStore, project: Project) -> None:
    """Rewrite TASKS.md after an item changes (done / reassigned)."""
    folder = store.export_root / project.slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "TASKS.md").write_text(tasks_markdown(store, project), encoding="utf-8")


# --------------------------------------------------------------------------- reading files (text, captions, audio)
TEXT_SUFFIXES = {".txt", ".md", ".vtt", ".srt", ".text", ".log"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".mp4", ".webm", ".ogg", ".flac", ".mkv"}
MAX_FILE_BYTES = 8_000_000


def transcribe_audio(path: Path, model: str = "base") -> str:
    """Speech to text with faster-whisper (the optional `voice` extra). Runs on this PC; nothing is uploaded."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise MeetingError("Audio needs the voice extras: pip install -e \".[voice]\" (it transcribes on this PC). "
                           "Or paste the transcript text instead.") from None
    segments, _ = WhisperModel(model, device="cpu", compute_type="int8").transcribe(str(path), beam_size=1, vad_filter=True)
    return "\n".join(seg.text.strip() for seg in segments if seg.text.strip())


def read_transcript_file(path, whisper_model: str = "base") -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise MeetingError(f"no such file: {p}")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise MeetingError("that file is too large (limit 8 MB)")
    suffix = p.suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return transcribe_audio(p, whisper_model)
    if suffix and suffix not in TEXT_SUFFIXES:
        raise MeetingError(f"unsupported file type {suffix} (use .txt, .md, .vtt, .srt or an audio file)")
    return p.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- short text for chat / terminal
def summary_text(project: Project, meeting: Meeting, saved_to: Path | None = None) -> str:
    lines = [f"{meeting.title} ({meeting.held_on}) for {project.name}", "", meeting.summary or "(no summary)"]
    if meeting.decisions:
        lines += ["", "Decisions:"] + [f"  - {d}" for d in meeting.decisions]
    lines += ["", f"Action items ({len(meeting.items)}):"]
    if not meeting.items:
        lines.append("  none found")
    for i in meeting.items:
        flag = "" if i["verified"] else "  [unverified]"
        lines.append(f"  #{i['id']} {i['owner']}: {i['task']} (due: {_due_label(i)}){flag}")
    if meeting.questions:
        lines += ["", "Open questions:"] + [f"  - {q}" for q in meeting.questions]
    for w in meeting.warnings:
        lines.append(f"\nNote: {w}")
    if saved_to:
        lines.append(f"\nSaved: {saved_to}")
    return "\n".join(lines)
