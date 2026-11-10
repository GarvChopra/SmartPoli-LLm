"""SmartPoli Care Navigator: Groq-powered AI support bot & eGain sandbox actions service (part of R2).

- Uses Groq (`https://api.groq.com/openai/v1`, default model `llama-3.3-70b-versatile`) via R2's
  `OpenAIBrain` (`r2/brain.py`), reading `GROQ_API_KEY` from the environment / `.env`.
- Never uses the local Ollama model for SmartPoli Care Navigator.
- Grounds answers in the 10 SmartPoli knowledge documents (`egain/knowledge/*.md`) with top-K document
  retrieval and strict abstention on unsupported questions.
- Maintains multi-turn conversation memory (`history`) and adaptive troubleshooting state per session,
  allowing natural topic switching, follow-up questions, and resumption of unfinished workflows.
- Handles API failures, rate limits (429), and timeouts gracefully without exposing secrets.
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import logging
import math
import os
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from r2.brain import Brain, BrainError, OpenAIBrain
from r2.cloud_setup import list_models, recommend
from r2.config import load_dotenv

log = logging.getLogger("smartpoli")
KB_DIR = Path(__file__).resolve().parent.parent / "egain" / "knowledge"
PAGE_PATH = Path(__file__).resolve().parent / "care_page.html"

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"

EMERGENCY = re.compile(
    r"chest pain|can'?t breathe|cannot breathe|trouble breathing|overdos|took too (much|many)|suicid|"
    r"kill myself|unconscious|unresponsive|seizure|stroke|severe allergic|anaphyla|bleeding (heavily|a lot)|"
    r"swollen (face|throat|tongue)|passed out|poison", re.I)
INJECTION = re.compile(
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules)|system prompt|developer mode|"
    r"you are now|reveal (your|the) (instructions|prompt)|disregard.*(rules|polic)", re.I)
HUMAN = re.compile(r"\b(human|agent|person|representative|specialist|callback|call me|talk to someone)\b", re.I)
REMINDER_INTENT = re.compile(
    r"\b(not getting|missing|stopped|no (medication )?alert|no (medication )?reminder|my reminders are late|"
    r"didn'?t get .*reminder|haven'?t received .*reminder|reminders? aren'?t working)\b", re.I)
PII = [(re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
       (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "[number]")]
EMERGENCY_MESSAGE = ("This may be an emergency. I can't assess symptoms or advise on medication. Call your local "
                     "emergency number now (911 in the US) or go to the nearest emergency department. "
                     "Poison Control (US): 1-800-222-1222.")
STOP = set("a an the is are do does i my me to of for how what can you your it in on and or if at be this that "
           "with will which have has not no there their about get each when after".split())

CHECK_LABELS = {
    "notifications": "phone notifications are allowed for the SmartPoli app",
    "dnd": "Do Not Disturb is off (or SmartPoli is allowed through it)",
    "timezone": "your phone time zone matches your SmartPoli account time zone",
}

CARE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_support_ticket",
            "description": (
                "Create a simulated SmartPoli support ticket (returns reference SP-xxxx) when the user asks to "
                "open a ticket/support request, or when all 3 reminder-troubleshooting checks (notifications, "
                "Do Not Disturb, time zone) have been verified and reminders are still not working."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["reminder_issue", "missed_dose_logging", "account_help", "other"],
                    },
                    "summary": {
                        "type": "string",
                        "description": "Concise privacy-safe summary of the user's issue (5-500 chars).",
                    },
                },
                "required": ["category", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_callback",
            "description": (
                "Book a simulated callback (returns reference CB-xxxx) when the user wants a callback and has "
                "chosen a time window: morning, afternoon, or evening."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                },
                "required": ["window"],
            },
        },
    },
]


def scrub(text: str) -> str:
    for rx, repl in PII:
        text = rx.sub(repl, text)
    return text


def _toks(s: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9']+", s.lower()):
        if w not in STOP:
            out.append(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def _load_kb_corpus() -> tuple[list[tuple[str, str]], list[str]]:
    docs: list[tuple[str, str]] = []
    chunks: list[str] = []
    if KB_DIR.exists():
        for p in sorted(KB_DIR.glob("*.md")):
            raw = p.read_text(encoding="utf-8").strip()
            docs.append((p.name, raw))
            for line in raw.splitlines():
                if line.strip() and not line.startswith("#"):
                    chunks.append(line.strip())
    return docs, chunks


def _retrieve_relevant_kb(query: str, history: list[dict], docs: list[tuple[str, str]], chunks: list[str]) -> tuple[str, float]:
    """Retrieve the most relevant KB sections and compute top recall score for grounding."""
    recent_user = " ".join(m["content"] for m in history[-4:] if m.get("role") == "user")
    combined_q = f"{query} {recent_user}".strip()
    qt = set(_toks(combined_q))
    if not qt or not chunks:
        full = "\n\n".join(f"[{name}]\n{text}" for name, text in docs)
        return full, 0.0
    df = Counter(w for c in chunks for w in set(_toks(c)))
    idf = lambda w: math.log((len(chunks) + 1) / (df.get(w, 0) + 0.5))
    q_only = set(_toks(query)) or qt
    total = sum(idf(w) for w in q_only) or 1.0
    best_recall = 0.0
    for c in chunks:
        ct = set(_toks(c))
        recall = sum(idf(w) for w in q_only & ct) / total
        if recall > best_recall:
            best_recall = recall

    # Rank documents by token overlap with current query + recent context, always including full KB (it's concise)
    ranked = sorted(
        docs,
        key=lambda item: sum(idf(w) for w in qt & set(_toks(item[1]))),
        reverse=True,
    )
    formatted = "\n\n".join(f"[{name}]\n{text}" for name, text in ranked)
    return formatted, best_recall


def _kb_answer(query: str, chunks: list[str]) -> str | None:
    qt = set(_toks(query))
    if not qt or not chunks:
        return None
    df = Counter(w for c in chunks for w in set(_toks(c)))
    idf = lambda w: math.log((len(chunks) + 1) / (df.get(w, 0) + 0.5))
    total = sum(idf(w) for w in qt) or 1.0
    best_recall, best_rank, best_line = 0.0, -1.0, None
    for c in chunks:
        ct = set(_toks(c))
        recall = sum(idf(w) for w in qt & ct) / total
        rank = recall + 0.1 * (len(qt & ct) / (len(ct) or 1))
        if rank > best_rank:
            best_recall, best_rank, best_line = recall, rank, c
    return best_line if best_recall >= 0.30 else None


class GroqFailoverBrain:
    """Calls the primary Groq model and automatically fails over to secondary Groq models on HTTP 429 rate limits."""

    hosted = True

    def __init__(self, brains: list[OpenAIBrain], models: list[str]):
        self.brains = brains
        self.models = models

    def decide(self, messages, tools, on_token=None, should_stop=None):
        last_exc = None
        for idx, b in enumerate(self.brains):
            try:
                return b.decide(messages, tools, on_token=on_token, should_stop=should_stop)
            except BrainError as exc:
                last_exc = exc
                err_str = str(exc).lower()
                if ("rate limit" in err_str or "429" in err_str) and idx + 1 < len(self.brains):
                    log.info("Groq model %s rate-limited; failing over to %s", self.models[idx], self.models[idx + 1])
                    continue
                raise
        raise last_exc


def make_groq_brain(
    api_key: str | None = None,
    model: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[Brain | None, str]:
    """Create a Groq-backed Brain from GROQ_API_KEY with automatic rate-limit failover across Groq models."""
    load_dotenv()
    groq_key = (api_key if api_key is not None else os.environ.get("GROQ_API_KEY", "")).strip()
    groq_model = (model or os.environ.get("GROQ_MODEL", "") or DEFAULT_GROQ_MODEL).strip()
    if not groq_key:
        return None, groq_model
    http_client = client or httpx.Client(timeout=httpx.Timeout(25.0, connect=8.0))
    if client is not None or model is not None:
        return OpenAIBrain(GROQ_BASE_URL, groq_key, groq_model, client=http_client), groq_model

    candidates = [groq_model]
    for fallback in ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"):
        if fallback not in candidates:
            candidates.append(fallback)
    try:
        avail = set(list_models(GROQ_BASE_URL, groq_key))
        if avail:
            candidates = [m for m in candidates if m in avail] or sorted(avail)
    except Exception:
        pass
    brains = [OpenAIBrain(GROQ_BASE_URL, groq_key, m, client=http_client) for m in candidates]
    return GroqFailoverBrain(brains, candidates), candidates[0]


def _update_troubleshoot_state(session: dict, user_text: str) -> None:
    """Adaptively extract what the user already checked or reported from natural language."""
    low = user_text.lower()
    checks: dict = session.setdefault("checks", {"notifications": None, "dnd": None, "timezone": None})
    if REMINDER_INTENT.search(user_text):
        session["workflow"] = "troubleshoot"

    if re.search(r"notif", low):
        if re.search(r"\b(on|allow|enabled|checked|yes|fine|working)\b", low) and not re.search(r"\b(not|off|disabled|blocked)\b", low):
            checks["notifications"] = "ok"
        elif re.search(r"\b(off|disabled|blocked|no|not)\b", low):
            checks["notifications"] = "needs_fix"
    if re.search(r"do not disturb|\bdnd\b|focus mode", low):
        if re.search(r"\b(off|allow|checked|disabled|no dnd|not on|yes)\b", low):
            checks["dnd"] = "ok"
        elif re.search(r"\b(on|enabled|blocking)\b", low):
            checks["dnd"] = "needs_fix"
    if re.search(r"time\s*zone|clock", low):
        if re.search(r"\b(match|same|correct|checked|yes|right)\b", low) and not re.search(r"\b(not|wrong|different|mismatch|off)\b", low):
            checks["timezone"] = "ok"
        elif re.search(r"\b(wrong|different|mismatch|off|no)\b", low):
            checks["timezone"] = "needs_fix"
    if re.search(r"checked (all|everything)|all three|already did all", low):
        for k in checks:
            checks[k] = "ok"

    pending = session.get("pending_check")
    if pending in checks and re.fullmatch(r"\s*(yes|yep|yeah|sure|it is|they are|i did|checked|no|nope|not yet|it isn't|they aren't)[.!]?\s*", low):
        is_yes = bool(re.match(r"\s*(yes|yep|yeah|sure|it is|they are|i did|checked)", low))
        checks[pending] = "ok" if is_yes else "needs_fix"


def _next_unchecked(session: dict) -> str | None:
    checks = session.get("checks") or {}
    for k in ("notifications", "dnd", "timezone"):
        if checks.get(k) is None:
            return k
    return None


def _build_system_prompt(kb_text: str, session: dict, top_recall: float) -> str:
    who = session.get("name") or "Visitor"
    cat = session.get("category") or "general"
    wf = session.get("workflow") or "none"
    checks = session.get("checks") or {}
    unchecked = _next_unchecked(session)
    state_summary = (
        f"Visitor name: {who} | Initial topic: {cat} | Active workflow: {wf}\n"
        f"Reminder troubleshooting state: notifications={checks.get('notifications')}, "
        f"dnd={checks.get('dnd')}, timezone={checks.get('timezone')} | Next unchecked item: {unchecked or 'ALL_CHECKED'}\n"
        f"Knowledge retrieval match score for latest message: {top_recall:.2f}"
    )
    return (
        "You are SmartPoli Care Navigator, a warm, intelligent, conversational customer-service AI agent for the "
        "fictional SmartPoli medication-reminder service.\n\n"
        "STRICT GROUNDING & SAFETY RULES:\n"
        "1. Answer ONLY using facts from the SmartPoli Knowledge Base below. If the user asks about something not "
        "in the Knowledge Base (for example: pricing/subscription cost, Apple Watch/smartwatch apps, pharmacy networks, "
        "Epic EHR integration, Spanish/language translation, weather, or caregiver sharing), explicitly say you don't "
        "have that information in the SmartPoli knowledge base rather than guessing, and offer a demonstration callback "
        "or support email (help@smartpoli.example).\n"
        "2. Never diagnose conditions, recommend or change medications/doses, or advise whether to take a missed dose. "
        "Always advise asking a pharmacist or care team for dose questions.\n"
        "3. CONVERSATIONAL & ADAPTIVE WORKFLOW:\n"
        "   - Use the conversation history to understand follow-up questions ('why?', 'what does that mean?', "
        "'what if I already checked that?') and topic switches.\n"
        "   - When troubleshooting missing or late reminders, guide the user conversationally through the 3 checks "
        "(1: phone notifications allowed for SmartPoli, 2: Do Not Disturb off or SmartPoli allowed, 3: phone time zone "
        "matches account time zone). Acknowledge any checks the user already mentioned, answer side questions naturally, "
        "and then ask about the next unchecked item.\n"
        "   - If all 3 checks are confirmed ('ALL_CHECKED') and reminders are still missing, or if the user asks to "
        "open a support ticket, call the `create_support_ticket` tool.\n"
        "   - If the user asks for a callback and specifies morning, afternoon, or evening, call `request_callback`.\n"
        "   - Keep replies concise (2-4 sentences), empathetic, and natural.\n\n"
        f"CURRENT SESSION STATE:\n{state_summary}\n\n"
        f"SMARTPOLI KNOWLEDGE BASE (AUTHORIZED SOURCE):\n{kb_text}"
    )


def _contextual_chips(session: dict, last_reply: str) -> list[str]:
    low = last_reply.lower()
    if "morning" in low and "afternoon" in low:
        return ["Callback morning", "Callback afternoon", "Callback evening", "Email support"]
    if session.get("workflow") == "troubleshoot":
        nxt = _next_unchecked(session)
        if nxt == "notifications":
            return ["Yes, notifications are on", "No, they were off", "Why would notifications stop?"]
        if nxt == "dnd":
            return ["Do Not Disturb is off", "It was on, I turned it off", "How late can a reminder arrive?"]
        if nxt == "timezone":
            return ["Yes, time zones match", "No, my time zone was different", "Open a support ticket"]
    return ["How late can a reminder arrive?", "I'm not getting reminders", "How do I log a missed dose?", "Talk to a human"]


class TicketIn(BaseModel):
    category: Literal["reminder_issue", "missed_dose_logging", "account_help", "other"]
    summary: str = Field(min_length=5, max_length=500)
    idempotency_key: str | None = Field(None, max_length=64)


class CallbackIn(BaseModel):
    window: Literal["morning", "afternoon", "evening"]
    demo_phone: str = Field("555-0100", pattern=r"^555-01\d\d$")


class TextIn(BaseModel):
    text: str = Field(max_length=2000)


class ChatStartIn(BaseModel):
    first_name: str = Field("", max_length=40)
    category: Literal["reminder_issue", "missed_dose_logging", "account_help", "other"]


class ChatMsgIn(BaseModel):
    session: str = Field(min_length=3, max_length=32)
    message: str = Field(min_length=1, max_length=500)


def create_app(
    api_key: str | None = None,
    allow_faults: bool = False,
    brain: Brain | None = None,
    groq_api_key: str | None = None,
    groq_model: str | None = None,
    use_default_brain: bool = True,
) -> FastAPI:
    key = api_key if api_key is not None else os.environ.get("SMARTPOLI_API_KEY", "")
    if not key:
        raise RuntimeError("SMARTPOLI_API_KEY must be set (fail closed).")
    state: dict = {"tickets": {}, "idem": {}, "sessions": {}}
    docs, chunks = _load_kb_corpus()
    if brain is not None:
        active_brain: Brain | None = brain
        model_name = groq_model or DEFAULT_GROQ_MODEL
    elif use_default_brain:
        active_brain, model_name = make_groq_brain(api_key=groq_api_key, model=groq_model)
    else:
        active_brain, model_name = None, groq_model or DEFAULT_GROQ_MODEL

    lock = threading.Lock()
    counter = itertools.count(1001)
    app = FastAPI(title="SmartPoli Care Navigator (Groq AI + eGain Actions)", docs_url=None, redoc_url=None)

    def auth(provided: str | None) -> None:
        if not provided or not hmac.compare_digest(provided, key):
            raise HTTPException(401, "Unauthorized")

    def maybe_fault(request: Request) -> None:
        if allow_faults and request.headers.get("x-simulate-failure") == "1":
            raise HTTPException(503, "Simulated downstream failure. Offer the human escalation options.")

    @app.exception_handler(HTTPException)
    async def _http(_, exc: HTTPException):
        return JSONResponse({"ok": False, "message": exc.detail}, status_code=exc.status_code)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE_PATH.read_text(encoding="utf-8")

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "simulated": True,
            "provider": "groq",
            "model": model_name,
            "ai_connected": active_brain is not None,
        }

    @app.post("/safety/check")
    def safety(body: TextIn, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        emergency = bool(EMERGENCY.search(body.text))
        return {"ok": True, "emergency": emergency, "prompt_injection": bool(INJECTION.search(body.text)),
                "message": EMERGENCY_MESSAGE if emergency else ""}

    def _make_ticket(category: str, summary: str, idempotency_key: str | None = None) -> dict:
        if EMERGENCY.search(summary):
            return {"ok": False, "emergency": True, "message": EMERGENCY_MESSAGE}
        clean = scrub(summary)
        if len(clean) < 5:
            clean = f"Support request for {category}"
        fp = idempotency_key or hashlib.sha256(f"{category}|{clean.lower()}".encode()).hexdigest()[:32]
        with lock:
            if fp in state["idem"]:
                ref = state["idem"][fp]
                return {"ok": True, "duplicate": True, "reference": ref,
                        "message": f"You already have an open request, reference {ref}. No duplicate was created."}
            ref = f"SP-{next(counter)}"
            state["idem"][fp] = ref
            state["tickets"][ref] = {"category": category, "summary": clean, "status": "open (simulated)"}
        log.info("ticket created ref=%s category=%s", ref, category)
        return {"ok": True, "duplicate": False, "reference": ref, "simulated": True,
                "handoff_summary": f"[{category}] {clean}",
                "message": f"Support request created (simulated). Reference {ref}. A specialist will follow up."}

    def _make_callback(window: str) -> dict:
        w = window if window in ("morning", "afternoon", "evening") else "morning"
        with lock:
            ref = f"CB-{next(counter)}"
        log.info("callback created ref=%s", ref)
        return {"ok": True, "reference": ref, "simulated": True,
                "message": f"Demonstration callback booked for the {w} (demo line 555-0100). Reference {ref}."}

    @app.post("/actions/create_ticket")
    def create_ticket(body: TicketIn, request: Request, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        maybe_fault(request)
        return _make_ticket(body.category, body.summary, body.idempotency_key)

    @app.post("/actions/request_callback")
    def request_callback(body: CallbackIn, request: Request, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        maybe_fault(request)
        return _make_callback(body.window)

    @app.get("/admin/tickets/{ref}")
    def get_ticket(ref: str, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        t = state["tickets"].get(ref)
        if not t:
            raise HTTPException(404, "Not found")
        return {"ok": True, **t}

    @app.post("/chat/start")
    def chat_start(body: ChatStartIn) -> dict:
        with lock:
            sid = f"S-{next(counter)}"
            is_rem = body.category == "reminder_issue"
            sess = {
                "name": body.first_name.strip(),
                "category": body.category,
                "workflow": "troubleshoot" if is_rem else None,
                "checks": {"notifications": None, "dnd": None, "timezone": None},
                "pending_check": "notifications" if is_rem else None,
                "history": [],
            }
            state["sessions"][sid] = sess
        who = f" {body.first_name.strip()}" if body.first_name.strip() else ""
        if is_rem:
            greeting = (
                f"Hi{who}! I'm SmartPoli Care Navigator. I can help figure out why your medication reminders aren't "
                "arriving, or answer any question you have. To start, are notifications allowed for the SmartPoli app "
                "in your phone settings (or tell me what you've already checked)?"
            )
        elif body.category == "missed_dose_logging":
            greeting = (
                f"Hi{who}! I can walk you through logging or editing a missed dose in SmartPoli, or answer any other "
                "questions. What would you like to know?"
            )
        else:
            greeting = (
                f"Hi{who}! I'm SmartPoli Care Navigator. Ask me anything about medication reminders, missed-dose "
                "logging, privacy, or support options."
            )
        sess["history"].append({"role": "assistant", "content": greeting})
        return {
            "ok": True,
            "session": sid,
            "reply": greeting,
            "chips": _contextual_chips(sess, greeting),
            "ai_connected": active_brain is not None,
            "model": model_name,
        }

    def _run_groq_turn(sess: dict, user_text: str) -> dict | None:
        if active_brain is None:
            return None
        kb_ranked, top_recall = _retrieve_relevant_kb(user_text, sess["history"], docs, chunks)
        messages = [
            {"role": "system", "content": _build_system_prompt(kb_ranked, sess, top_recall)},
            *sess["history"][-12:],
        ]
        try:
            decision = active_brain.decide(messages, CARE_TOOLS)
        except BrainError as exc:
            err_str = str(exc)
            log.warning("groq brain error: %s", err_str)
            if "rate limit" in err_str.lower() or "429" in err_str:
                return {
                    "ok": True,
                    "ai_error": "rate_limit",
                    "reply": (
                        "The Groq AI service is temporarily rate-limited. While we wait a moment, I can still "
                        "book a demonstration callback or share our support email (help@smartpoli.example)."
                    ),
                }
            return None

        if decision.tool_call is not None:
            tc = decision.tool_call
            args = tc.args or {}
            if tc.name == "create_support_ticket":
                cat = args.get("category") if args.get("category") in ("reminder_issue", "missed_dose_logging", "account_help", "other") else "reminder_issue"
                summ = str(args.get("summary") or f"User reported {cat} issue in chat")[:500]
                res = _make_ticket(cat, summ)
                sess["workflow"] = None
                reply = (
                    f"{res['message']} Please remember not to skip or change medication doses because of missing "
                    "reminders—check with your pharmacist or care team if you're unsure."
                )
                return {"ok": True, "reference": res.get("reference"), "reply": reply}
            if tc.name == "request_callback":
                win = str(args.get("window") or "morning").lower()
                res = _make_callback(win)
                sess["workflow"] = None
                return {"ok": True, "reference": res.get("reference"), "reply": res["message"]}

        text = (decision.final or "").strip()
        if not text:
            return None
        return {"ok": True, "reply": text}

    @app.post("/chat")
    def chat_turn(body: ChatMsgIn, request: Request) -> dict:
        sess = state["sessions"].get(body.session)
        if not sess:
            raise HTTPException(404, "Unknown session. Start a new chat.")
        if allow_faults and request.headers.get("x-simulate-failure") == "1":
            raise HTTPException(503, "Simulated AI service failure. Please try again or use Email support (help@smartpoli.example).")
        text = body.message.strip()
        low = text.lower()

        # 1. Hard safety boundary: Emergency always intercepts immediately
        if EMERGENCY.search(text):
            sess["workflow"] = None
            sess["history"].append({"role": "user", "content": text})
            sess["history"].append({"role": "assistant", "content": EMERGENCY_MESSAGE})
            return {"ok": True, "emergency": True, "reply": EMERGENCY_MESSAGE, "chips": []}

        # 2. Prompt injection defense
        if INJECTION.search(text):
            reply = "I can only assist with SmartPoli medication-reminder support using our authorized documentation."
            sess["history"].append({"role": "user", "content": text})
            sess["history"].append({"role": "assistant", "content": reply})
            return {"ok": True, "reply": reply, "chips": _contextual_chips(sess, reply)}

        # 3. Update adaptive troubleshooting state from natural language
        _update_troubleshoot_state(sess, text)
        sess["history"].append({"role": "user", "content": text})

        # 4. Direct callback booking when user specifies callback + window
        if re.search(r"\b(callback|call me)\b", low) or sess.get("workflow") == "escalation":
            for w in ("morning", "afternoon", "evening"):
                if w in low:
                    res = _make_callback(w)
                    sess["workflow"] = None
                    sess["history"].append({"role": "assistant", "content": res["message"]})
                    return {"ok": True, "reference": res["reference"], "reply": res["message"],
                            "chips": _contextual_chips(sess, res["message"])}
            if "email" in low:
                sess["workflow"] = None
                reply = "You can email our support team at help@smartpoli.example (demo address). Support hours are Monday to Friday, 8 AM to 6 PM Pacific time."
                sess["history"].append({"role": "assistant", "content": reply})
                return {"ok": True, "reply": reply, "chips": _contextual_chips(sess, reply)}

        # 5. If user explicitly asks for a human/agent, open escalation options
        if HUMAN.search(text):
            sess["workflow"] = "escalation"
            reply = (
                "I'd be happy to connect you with a human specialist. You can either: (1) Book a demonstration "
                "callback (just tell me morning, afternoon, or evening), or (2) Email help@smartpoli.example "
                "(Mon–Fri, 8 AM–6 PM Pacific). Which would you prefer?"
            )
            sess["history"].append({"role": "assistant", "content": reply})
            return {"ok": True, "reply": reply,
                    "chips": ["Callback morning", "Callback afternoon", "Callback evening", "Email support"]}

        # 6. If in troubleshooting and all 3 checks are confirmed 'ok', guarantee ticket creation
        if sess.get("workflow") == "troubleshoot" and _next_unchecked(sess) is None:
            t = _make_ticket("reminder_issue", "Visitor verified notifications, Do Not Disturb, and time zone; reminders still missing")
            sess["workflow"] = None
            sess["pending_check"] = None
            reply = (
                f"Since your notifications, Do Not Disturb, and time zone settings are all set properly and reminders "
                f"still aren't arriving, I've created a support ticket for you: {t['message']} Please don't skip or "
                "change doses because of missing reminders—ask your pharmacist or care team if needed."
            )
            sess["history"].append({"role": "assistant", "content": reply})
            return {"ok": True, "reference": t["reference"], "reply": reply,
                    "chips": ["What are your support hours?", "Talk to a human"]}

        # 7. Primary path: Real Groq API conversation grounded in KB + session memory + tools
        groq_out = _run_groq_turn(sess, text)
        if groq_out is not None:
            nxt = _next_unchecked(sess) if sess.get("workflow") == "troubleshoot" else None
            sess["pending_check"] = nxt
            reply = groq_out["reply"]
            sess["history"].append({"role": "assistant", "content": reply})
            out = {"ok": True, "reply": reply, "chips": _contextual_chips(sess, reply)}
            if groq_out.get("reference"):
                out["reference"] = groq_out["reference"]
            if groq_out.get("ai_error"):
                out["ai_error"] = groq_out["ai_error"]
            return out

        # 8. Deterministic KB + workflow fallback (when GROQ_API_KEY is not configured yet or Groq is unreachable)
        if sess.get("workflow") == "troubleshoot":
            checks = sess["checks"]
            for k, label in CHECK_LABELS.items():
                if checks.get(k) == "needs_fix":
                    checks[k] = "fixed_pending"
                    sess["pending_check"] = k
                    reply = f"Let's fix that first: please make sure {label}, then let me know if your reminders start working or if we should keep checking."
                    sess["history"].append({"role": "assistant", "content": reply})
                    return {"ok": True, "reply": reply, "chips": ["It's working now", "Still not working"]}
            nxt = _next_unchecked(sess)
            if nxt is not None:
                sess["pending_check"] = nxt
                idx = ("notifications", "dnd", "timezone").index(nxt) + 1
                reply = f"Step {idx} of 3: Can you check whether {CHECK_LABELS[nxt]}?"
                sess["history"].append({"role": "assistant", "content": reply})
                return {"ok": True, "reply": reply, "chips": ["Yes", "No", "Talk to a human"]}

        ans = _kb_answer(text, chunks)
        if ans:
            sess["history"].append({"role": "assistant", "content": ans})
            return {"ok": True, "reply": ans, "chips": _contextual_chips(sess, ans)}
        sess["workflow"] = "escalation"
        reply = "I don't have a reliable answer to that in my information. I'd rather not guess. I can book a demonstration callback or share the support email."
        sess["history"].append({"role": "assistant", "content": reply})
        return {"ok": True, "reply": reply,
                "chips": ["Callback morning", "Callback evening", "Email support"]}

    return app


def main() -> int:
    import uvicorn

    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", "8100")))
    return 0
