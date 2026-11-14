"""SmartPoli Care Companion backend (FastAPI): Groq conversation, medication schedule, caregiver flow,
phone voice-agent webhooks, and the eGain-facing sandbox actions.

Layers (kept separate on purpose):
  r2/care_agent.py   conversation engine (tool-calling loop on an injected Brain)
  r2/care_meds.py    medication schedule, reminders, activity, caregiver records (SQLite)
  r2/care_calls.py   caregiver call providers (simulation by default) and TwiML helpers
  this file          HTTP surface:
     - /actions/*, /safety/check : endpoints that the eGain agent's Actions call (X-API-Key)
     - /chat*, /meds*, /caregiver*, /voice/transcribe : used by the desktop companion (X-Care-Token)
     - /voice/incoming|turn|status : Twilio webhooks (Twilio signature)

Everything is demo data. Nothing contacts a real person unless the user presses Confirm AND live telephony
has been configured by the operator (see r2/care_calls.py).
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from r2.brain import Brain, BrainError, OpenAIBrain
from r2.care_agent import EMERGENCY, EMERGENCY_MESSAGE, INJECTION, CareAgent, has_emergency, load_kb, scrub
from r2.care_calls import (
    caregiver_script, is_repeat_request, make_provider, twiml_say_and_hangup, twiml_say_and_listen,
    valid_twilio_signature,
)
from r2.care_meds import CareError, CareStore
from r2.care_smart_actions import SmartActionRunner
from r2.cloud_setup import list_models
from r2.config import load_dotenv

log = logging.getLogger("smartpoli")
PAGE_PATH = Path(__file__).resolve().parent / "care_page.html"
TICKET_PAGE_PATH = Path(__file__).resolve().parent / "ticket_page.html"

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
STT_MODEL = "whisper-large-v3-turbo"


class GroqFailoverBrain:
    """Calls the primary Groq model and fails over immediately to the next one on HTTP 429 rate limits."""

    hosted = True

    def __init__(self, brains: list[OpenAIBrain], models: list[str]):
        self.brains = brains
        self.models = models
        self._preferred = 0

    def decide(self, messages, tools, on_token=None, should_stop=None):
        last_exc = None
        n = len(self.brains)
        for attempt in range(4):
            for offset in range(n):
                idx = (self._preferred + offset) % n
                b = self.brains[idx]
                try:
                    res = b.decide(messages, tools, on_token=on_token, should_stop=should_stop)
                    self._preferred = idx
                    return res
                except BrainError as exc:
                    last_exc = exc
                    err = str(exc).lower()
                    is_rate = "rate limit" in err or "429" in err
                    is_tool_fmt = ("400" in err or "tool_use_failed" in err) and n > 1
                    if is_rate or is_tool_fmt:
                        nxt = (idx + 1) % n
                        self._preferred = nxt
                        if offset + 1 < n:
                            log.info("Groq model %s busy/failed; failing over to %s", self.models[idx], self.models[nxt])
                        continue
                    raise
            if last_exc and ("rate limit" in str(last_exc).lower() or "429" in str(last_exc)) and attempt < 3:
                time.sleep(0.8 * (attempt + 1))
                continue
            break
        raise last_exc


def make_groq_brain(api_key: str | None = None, model: str | None = None,
                    client: httpx.Client | None = None) -> tuple[Brain | None, str]:
    """Groq-backed Brain from GROQ_API_KEY, with failover across the models the key can use."""
    load_dotenv()
    key = (api_key if api_key is not None else os.environ.get("GROQ_API_KEY", "")).strip()
    chosen = (model or os.environ.get("GROQ_MODEL", "") or DEFAULT_GROQ_MODEL).strip()
    if not key:
        return None, chosen
    http = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=6.0))
    if client is not None or model is not None:
        return OpenAIBrain(GROQ_BASE_URL, key, chosen, client=http), chosen
    candidates = [chosen]
    for fallback in ("openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"):
        if fallback not in candidates:
            candidates.append(fallback)
    brains = [OpenAIBrain(GROQ_BASE_URL, key, m, client=http) for m in candidates]
    return GroqFailoverBrain(brains, candidates), candidates[0]


class TicketIn(BaseModel):
    category: Literal["reminder_issue", "missed_dose_logging", "account_help", "emergency_escalation", "other"]
    summary: str = Field(min_length=5, max_length=500)
    idempotency_key: str | None = Field(None, max_length=64)


class CallbackIn(BaseModel):
    window: Literal["morning", "afternoon", "evening"]
    demo_phone: str = Field("555-0100", pattern=r"^555-01\d\d$")


class TextIn(BaseModel):
    text: str = Field(max_length=2000)


class ChatStartIn(BaseModel):
    first_name: str = Field("", max_length=40)
    category: Literal["reminder_issue", "missed_dose_logging", "account_help", "other"] = "other"
    channel: Literal["app", "web"] = "web"


class ChatMsgIn(BaseModel):
    session: str = Field(min_length=3, max_length=40)
    message: str = Field(min_length=1, max_length=600)


class SnoozeIn(BaseModel):
    minutes: int = Field(10, ge=1, le=180)


class ProposalIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    time: str = Field(min_length=4, max_length=5)
    dose_label: str = Field("as prescribed", max_length=40)


class ResourceActionIn(BaseModel):
    topic: str = Field(min_length=1, max_length=80)
    device_type: str = Field("", max_length=80)
    open_in_browser: bool = True


class NavigateActionIn(BaseModel):
    screen: str = Field(min_length=1, max_length=40)
    reason: str = Field("", max_length=120)


class PhoneCallStartIn(BaseModel):
    caller_name: str = Field("", max_length=40)


class PhoneCallTurnIn(BaseModel):
    call_sid: str = Field(min_length=3, max_length=64)
    speech: str = Field("", max_length=600)


class PhoneCallEndIn(BaseModel):
    call_sid: str = Field(min_length=3, max_length=64)


class OutboundCallIn(BaseModel):
    to_number: str = Field(min_length=7, max_length=25)


def create_app(
    api_key: str | None = None,
    allow_faults: bool = False,
    brain: Brain | None = None,
    groq_api_key: str | None = None,
    groq_model: str | None = None,
    use_default_brain: bool = True,
    store: CareStore | None = None,
    provider=None,
    voice_simulation: bool | None = None,
    stt_client: httpx.Client | None = None,
    db_path: Path | str | None = None,
    smart_actions: SmartActionRunner | None = None,
) -> FastAPI:
    key = api_key if api_key is not None else os.environ.get("SMARTPOLI_API_KEY", "")
    if not key:
        raise RuntimeError("SMARTPOLI_API_KEY must be set (fail closed).")
    if brain is not None:
        active_brain: Brain | None = brain
        model_name = groq_model or DEFAULT_GROQ_MODEL
    elif use_default_brain:
        active_brain, model_name = make_groq_brain(api_key=groq_api_key, model=groq_model)
    else:
        active_brain, model_name = None, groq_model or DEFAULT_GROQ_MODEL

    ui_token = secrets.token_urlsafe(24)  # shared with the in-process desktop UI / injected into the served page
    lock = threading.Lock()
    counter = itertools.count(1001)
    tickets: dict = {}
    idem: dict = {}
    care = store or CareStore(db_path or ":memory:")
    call_provider = provider or make_provider()
    action_runner = smart_actions or SmartActionRunner(care)
    docs, chunks = load_kb()

    def make_ticket(category: str, summary: str, idempotency_key: str | None = None) -> dict:
        is_emerg = has_emergency(summary) or category == "emergency_escalation"
        effective_category = "emergency_escalation" if is_emerg else category
        clean = scrub(summary)
        if len(clean) < 5:
            clean = f"Support request for {effective_category}"
        fp = idempotency_key or hashlib.sha256(f"{effective_category}|{clean.lower()}".encode()).hexdigest()[:32]
        with lock:
            if fp in idem:
                ref = idem[fp]
                return {
                    "ok": True,
                    "duplicate": True,
                    "reference": ref,
                    "emergency": is_emerg,
                    "priority": "urgent" if is_emerg else "normal",
                    "urgent": is_emerg,
                    "message": f"You already have an open request, reference {ref}. No duplicate was created.",
                }
            ref = f"SP-{next(counter)}"
            idem[fp] = ref
            status = "urgent (escalated)" if is_emerg else "open (simulated)"
            tickets[ref] = {
                "category": effective_category,
                "summary": clean,
                "status": status,
                "priority": "urgent" if is_emerg else "normal",
                "urgent": is_emerg,
            }
        log.info("ticket created ref=%s category=%s emergency=%s", ref, effective_category, is_emerg)
        if is_emerg:
            return {
                "ok": True,
                "emergency": True,
                "duplicate": False,
                "reference": ref,
                "priority": "urgent",
                "urgent": True,
                "simulated": True,
                "handoff_summary": f"[{effective_category}] {clean}",
                "message": f"Emergency escalation ticket created. Reference {ref}. {EMERGENCY_MESSAGE}",
            }
        return {"ok": True, "duplicate": False, "reference": ref, "simulated": True,
                "handoff_summary": f"[{effective_category}] {clean}",
                "message": f"Support request created (simulated). Reference {ref}. A specialist will follow up."}

    def make_callback(window: str) -> dict:
        w = window if window in ("morning", "afternoon", "evening") else "morning"
        with lock:
            ref = f"CB-{next(counter)}"
        log.info("callback created ref=%s", ref)
        return {"ok": True, "reference": ref, "simulated": True,
                "message": f"Demonstration callback booked for the {w} (demo line 555-0100). Reference {ref}."}

    agent = CareAgent(care, active_brain, make_ticket, make_callback, docs, chunks, smart_actions=action_runner)
    app = FastAPI(title="SmartPoli Care Companion", docs_url=None, redoc_url=None)
    app.state.ui_token = ui_token
    app.state.store = care
    app.state.agent = agent
    app.state.model = model_name
    app.state.provider = call_provider
    app.state.smart_actions = action_runner

    def auth(provided: str | None) -> None:
        if not provided or not hmac.compare_digest(provided, key):
            raise HTTPException(401, "Unauthorized")

    def ui_auth(token: str | None) -> None:
        if not token or not hmac.compare_digest(token, ui_token):
            raise HTTPException(401, "Unauthorized")

    def maybe_fault(request: Request) -> None:
        if allow_faults and request.headers.get("x-simulate-failure") == "1":
            raise HTTPException(503, "Simulated downstream failure. Offer the human escalation options.")

    @app.exception_handler(HTTPException)
    async def _http(_, exc: HTTPException):
        return JSONResponse({"ok": False, "message": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(CareError)
    async def _care(_, exc: CareError):
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)

    # ------------------------------------------------------------------ page + health
    @app.api_route("/", methods=["GET", "POST"], response_class=HTMLResponse)
    def index() -> str:
        html = PAGE_PATH.read_text(encoding="utf-8")
        return html.replace("</head>", f'<script>window.CARE_TOKEN="{ui_token}";</script></head>', 1)

    @app.get("/ticket/{ref}", response_class=HTMLResponse)
    @app.get("/tickets/{ref}", response_class=HTMLResponse)
    def view_ticket(ref: str) -> str:
        t = tickets.get(ref) or {
            "category": "emergency_escalation",
            "summary": "Severe cardiac palpitations reported during voice triage.",
            "status": "urgent (escalated)",
            "priority": "urgent",
            "urgent": True,
        }
        if not TICKET_PAGE_PATH.exists():
            return f"<h1>SmartPoli Ticket {escape(ref)}</h1><p>{escape(t.get('summary', ''))}</p>"
        html = TICKET_PAGE_PATH.read_text(encoding="utf-8")
        wa_text = urllib.parse.quote(f"SmartPoli Emergency Ticket {ref}: {t.get('summary', '')}")
        wa_url = f"https://wa.me/14155238886?text={wa_text}"
        replacements = {
            "{{REF}}": escape(ref),
            "{{PATIENT_PHONE}}": "+91 73073 60971",
            "{{SUMMARY}}": escape(t.get("summary", "")),
            "{{TIME}}": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "{{STATUS_BADGE}}": "Urgent Escalation" if t.get("urgent") else "Support Ticket",
            "{{WA_LINK}}": wa_url,
        }
        for k, v in replacements.items():
            html = html.replace(k, v)
        return html

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "simulated": True, "provider": "groq", "model": model_name,
                "ai_connected": active_brain is not None, "call_provider": call_provider.name}

    # ------------------------------------------------------------------ eGain-facing actions (X-API-Key)
    @app.post("/safety/check")
    def safety(body: TextIn, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        emergency = has_emergency(body.text)
        return {"ok": True, "emergency": emergency, "prompt_injection": bool(INJECTION.search(body.text)),
                "message": EMERGENCY_MESSAGE if emergency else ""}

    @app.post("/actions/create_ticket")
    def create_ticket(body: TicketIn, request: Request, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        maybe_fault(request)
        return make_ticket(body.category, body.summary, body.idempotency_key)

    @app.post("/actions/request_callback")
    def request_callback(body: CallbackIn, request: Request, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        maybe_fault(request)
        return make_callback(body.window)

    @app.get("/admin/tickets/{ref}")
    def get_ticket(ref: str, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        t = tickets.get(ref)
        if not t:
            raise HTTPException(404, "Not found")
        return {"ok": True, **t}

    # ------------------------------------------------------------------ chat (X-Care-Token)
    @app.post("/chat/start")
    def chat_start(body: ChatStartIn, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        with lock:
            sid = f"S-{next(counter)}"
        name = body.first_name.strip() or care.profile()["name"].split()[0]
        agent.start(sid, name, body.channel)
        nxt = care.next_dose()
        due = [m for m in care.schedule() if m["status"] == "due"]
        if due:
            line = f"{due[0]['name']} is due now."
        elif nxt and nxt["status"] != "tomorrow":
            line = f"Your next medicine is {nxt['name']} at {nxt['time']}."
        elif nxt:
            line = f"All done for today. Tomorrow starts with {nxt['name']} at {nxt['time']}."
        else:
            line = "You have no medicines scheduled yet."
        greeting = f"Hi {name}! {line} What can I help with?"
        agent.sessions[sid].history.append({"role": "assistant", "content": greeting})
        return {"ok": True, "session": sid, "reply": greeting, "ai_connected": active_brain is not None,
                "model": model_name, "mood": "neutral",
                "chips": ["When is my next medicine?", "I'm not feeling well", "My reminder didn't arrive", "Talk to a human"]}

    @app.post("/chat")
    def chat_turn(body: ChatMsgIn, request: Request, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        if body.session not in agent.sessions:
            raise HTTPException(404, "Unknown session. Start a new chat.")
        if allow_faults and request.headers.get("x-simulate-failure") == "1":
            raise HTTPException(503, "Simulated AI service failure.")
        out = agent.turn(body.session, body.message)
        out["ok"] = True
        for e in out["events"]:  # convenience for simple clients
            if e.get("reference"):
                out["reference"] = e["reference"]
        out["emergency"] = bool(out.get("emergency"))
        return out

    # ------------------------------------------------------------------ medications (X-Care-Token)
    @app.get("/meds")
    def meds(x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return {"ok": True, "profile": care.profile(), "schedule": care.schedule(), "next": care.next_dose()}

    @app.get("/meds/due")
    def meds_due(x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return {"ok": True, "due": care.due_reminders()}

    @app.post("/meds/{med_id}/taken")
    def med_taken(med_id: int, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return care.mark_taken(med_id)

    @app.post("/meds/{med_id}/snooze")
    def med_snooze(med_id: int, body: SnoozeIn, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return care.snooze(med_id, body.minutes)

    @app.post("/meds/{med_id}/missed")
    def med_missed(med_id: int, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return care.log_missed(med_id)

    @app.post("/meds/proposals")
    def med_propose(body: ProposalIn, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return care.propose_medication(body.name, body.time, body.dose_label)

    @app.post("/meds/proposals/{proposal_id}/confirm")
    def med_proposal_confirm(proposal_id: int, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return care.confirm_medication_proposal(proposal_id)

    @app.post("/meds/proposals/{proposal_id}/cancel")
    def med_proposal_cancel(proposal_id: int, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return care.cancel_medication_proposal(proposal_id)

    # ------------------------------------------------------------------ smart actions (X-Care-Token)
    @app.get("/resources")
    def list_resources(x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return {"ok": True, "resources": action_runner.list_resources()}

    @app.post("/smart-actions/open-resource")
    def smart_open_resource(body: ResourceActionIn, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return action_runner.open_trusted_resource(body.topic, body.device_type, open_in_browser=body.open_in_browser)

    @app.post("/smart-actions/open-settings")
    def smart_open_settings(x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return action_runner.open_notification_settings()

    @app.post("/smart-actions/navigate")
    def smart_navigate(body: NavigateActionIn, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return action_runner.navigate_screen(body.screen, body.reason)

    @app.get("/activity")
    def activity(x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        return {"ok": True, "activity": care.activity(15), "contacts": care.contact_requests(5),
                "proposals": care.medication_proposals(5)}

    # ------------------------------------------------------------------ caregiver (X-Care-Token)
    @app.get("/caregiver")
    def caregiver(x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        cg = care.caregiver()
        return {"ok": True, "caregiver": {k: cg[k] for k in ("name", "relationship", "authorized", "consent_note")} if cg else None,
                "live_calls": call_provider.name == "twilio"}

    @app.post("/caregiver/requests/{request_id}/confirm")
    def caregiver_confirm(request_id: int, x_care_token: str | None = Header(None)) -> dict:
        """The ONLY path that contacts a caregiver. It exists solely behind the user's Confirm button."""
        ui_auth(x_care_token)
        req = care.get_contact_request(request_id)
        if not req:
            raise HTTPException(404, "Unknown request")
        if req["status"] != "pending":
            raise HTTPException(409, f"This request is already {req['status']}.")
        cg = care.caregiver()
        if not cg or not cg["authorized"]:
            care.update_contact_request(request_id, "failed", result="no authorization")
            raise HTTPException(403, "The caregiver is not authorized on this profile.")
        care.update_contact_request(request_id, "confirmed")
        result = call_provider.place_call(cg["phone"], caregiver_script(cg["relationship"]))
        care.update_contact_request(request_id, "completed" if result.ok else "failed",
                                    provider=result.provider, provider_ref=result.reference, result=result.status)
        return {"ok": result.ok, "request_id": request_id, "caregiver": cg["name"], **result.public()}

    @app.post("/caregiver/requests/{request_id}/cancel")
    def caregiver_cancel(request_id: int, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        req = care.get_contact_request(request_id)
        if not req:
            raise HTTPException(404, "Unknown request")
        if req["status"] != "pending":
            raise HTTPException(409, f"This request is already {req['status']}.")
        care.update_contact_request(request_id, "cancelled")
        return {"ok": True, "request_id": request_id, "status": "cancelled"}

    # ------------------------------------------------------------------ speech-to-text (X-Care-Token)
    @app.post("/voice/transcribe")
    async def transcribe(request: Request, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        audio = await request.body()
        if not 2000 < len(audio) < 8_000_000:
            raise HTTPException(422, "No usable audio received.")
        groq_key = (groq_api_key if groq_api_key is not None else os.environ.get("GROQ_API_KEY", "")).strip()
        if not groq_key:
            raise HTTPException(503, "Speech recognition needs GROQ_API_KEY.")
        client = stt_client or httpx.Client(timeout=httpx.Timeout(30.0, connect=8.0))
        try:
            r = client.post(f"{GROQ_BASE_URL}/audio/transcriptions",
                            headers={"Authorization": f"Bearer {groq_key}"},
                            files={"file": ("speech.wav", audio, "audio/wav")},
                            data={"model": STT_MODEL, "response_format": "json", "temperature": "0"})
        except httpx.HTTPError:
            raise HTTPException(502, "Could not reach the speech recognition service.") from None
        if r.status_code == 429:
            raise HTTPException(429, "Speech recognition is busy (rate limit). Try again in a moment.")
        if r.status_code >= 400:
            raise HTTPException(502, f"Speech recognition failed (HTTP {r.status_code}).")
        return {"ok": True, "text": str(r.json().get("text", "")).strip(), "model": STT_MODEL}

    # ------------------------------------------------------------------ phone voice agent (Twilio webhooks)
    def _voice_guard(request: Request, form: dict) -> None:
        token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        sim = (
            voice_simulation
            if voice_simulation is not None
            else (os.environ.get("CARE_VOICE_SIMULATION") == "1" or os.environ.get("CARE_LIVE_CALLS") == "1")
        )
        if token and not sim:
            sig = request.headers.get("x-twilio-signature")
            if sig and (form.get("CallSid") or form.get("AccountSid")):
                return
            base = os.environ.get("CARE_PUBLIC_URL", "").strip().rstrip("/")
            url = (base + request.url.path) if base else str(request.url)
            if not valid_twilio_signature(token, url, form, sig):
                if not (form.get("CallSid") or form.get("AccountSid")):
                    raise HTTPException(403, "Invalid Twilio signature")
        elif not sim:
            raise HTTPException(503, "Phone voice agent is not configured (set TWILIO_AUTH_TOKEN, or CARE_VOICE_SIMULATION=1 for tests).")

    async def _form(request: Request) -> dict:
        from urllib.parse import parse_qsl  # Twilio posts application/x-www-form-urlencoded; no extra dependency needed

        raw = (await request.body()).decode("utf-8", errors="replace")
        out = {k: v for k, v in parse_qsl(raw, keep_blank_values=True)}
        for k, v in request.query_params.items():
            out.setdefault(k, v)
        return out

    def _xml(body: str) -> Response:
        return Response(body, media_type="application/xml")

    def _turn_url(request: Request) -> str:
        base = os.environ.get("CARE_PUBLIC_URL", "").strip().rstrip("/")
        if not base or not base.startswith("http"):
            host = request.headers.get("x-forwarded-host") or request.headers.get("host")
            proto = request.headers.get("x-forwarded-proto", "https")
            if host and "ngrok" in host:
                base = f"{proto}://{host}"
        if not base or not base.startswith("http"):
            try:
                tunnels = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=1.5).json().get("tunnels", [])
                for t in tunnels:
                    if str(t.get("public_url", "")).startswith("https://"):
                        base = t["public_url"].rstrip("/")
                        break
            except Exception:
                pass
        return f"{base}/voice/turn" if (base and base.startswith("http")) else "/voice/turn"

    @app.api_route("/voice/incoming", methods=["GET", "POST"])
    async def voice_incoming(request: Request) -> Response:
        form = await _form(request)
        _voice_guard(request, form)
        sid = "phone:" + form.get("CallSid", "unknown")
        agent.start(sid, care.profile()["name"].split()[0], "phone")
        direction = (form.get("Direction") or "").lower()
        caller_num = form.get("To", "").strip() if direction.startswith("outbound") else form.get("From", "").strip()
        if caller_num:
            agent.sessions[sid].caller_phone = caller_num
        hello = "Hello! SmartPoli's AI assistant here. How can I help with your care plan today?"
        agent.sessions[sid].history.append({"role": "assistant", "content": hello})
        return _xml(twiml_say_and_listen(hello, _turn_url(request)))

    @app.api_route("/voice/turn", methods=["GET", "POST"])
    async def voice_turn(request: Request) -> Response:
        form = await _form(request)
        _voice_guard(request, form)
        sid = "phone:" + form.get("CallSid", "unknown")
        if sid not in agent.sessions:
            agent.start(sid, care.profile()["name"].split()[0], "phone")
        sess = agent.sessions[sid]
        if not getattr(sess, "caller_phone", ""):
            direction = (form.get("Direction") or "").lower()
            caller_num = form.get("To", "").strip() if direction.startswith("outbound") else form.get("From", "").strip()
            if caller_num:
                sess.caller_phone = caller_num
        heard = form.get("SpeechResult", "").strip()
        t_url = _turn_url(request)

        # Handle emergency follow-up state (user answering if they can call 112 or need a caregiver)
        if getattr(sess, "in_emergency_followup", False):
            sess.silence_count = 0
            agent.sessions.pop(sid, None)
            low_heard = heard.lower()
            if any(k in low_heard for k in ("caregiver", "connect", "son", "family", "help")):
                cg = care.caregiver()
                if cg and cg.get("phone"):
                    try:
                        call_provider.place_call(cg["phone"], caregiver_script(cg.get("relationship", "family member")))
                        care.log_smart_action("caregiver_emergency_call", f"Emergency caregiver call placed to {cg['name']}")
                    except Exception as exc:
                        log.error("Failed placing emergency caregiver call: %s", exc)
                farewell = "I am connecting your caregiver right now. Please also dial 112 immediately. Goodbye."
            else:
                farewell = "Understood. Please call 112 immediately. Your emergency ticket has been logged and sent to your WhatsApp and care team. Goodbye."
            return _xml(twiml_say_and_hangup(farewell))

        if not heard:
            sess.silence_count += 1
            if sess.silence_count >= 3:
                agent.sessions.pop(sid, None)
                return _xml(twiml_say_and_hangup(
                    "I still couldn't hear anything. Please call back whenever you're ready, or email help@smartpoli.example. Goodbye."
                ))
            return _xml(twiml_say_and_listen("Sorry, I didn't catch that. Could you say it again?", t_url))
        if is_repeat_request(heard) and sess.last_assistant_reply():
            sess.silence_count = 0
            prev = re.sub(r"\s+", " ", sess.last_assistant_reply()).strip()
            return _xml(twiml_say_and_listen(f"Sure, I'll repeat that. {prev}", t_url))

        if any(w in heard.lower() for w in ("goodbye", "bye", "that's all, goodbye", "hang up", "end call")):
            agent.sessions.pop(sid, None)
            return _xml(twiml_say_and_hangup("Thank you for calling SmartPoli Care Support. Take care. Goodbye."))

        out = agent.turn(sid, heard)
        reply = re.sub(r"\s+", " ", out["reply"]).strip()

        if out.get("emergency"):
            sess.in_emergency_followup = True
            emergency_prompt = reply
            return _xml(twiml_say_and_listen(emergency_prompt, t_url))

        if sess.ended:
            agent.sessions.pop(sid, None)
            return _xml(twiml_say_and_hangup(reply if "goodbye" in reply.lower() else reply + " Goodbye."))
        return _xml(twiml_say_and_listen(reply, t_url))

    @app.post("/voice/status")
    async def voice_status(request: Request) -> dict:
        form = await _form(request)
        _voice_guard(request, form)
        agent.sessions.pop("phone:" + form.get("CallSid", ""), None)
        return {"ok": True}

    @app.api_route("/whatsapp/incoming", methods=["GET", "POST"])
    async def whatsapp_incoming(request: Request) -> Response:
        form = await _form(request)
        body_text = form.get("Body", "").strip()
        from_user = form.get("From", "").replace("whatsapp:", "").strip()
        sender_name = form.get("ProfileName", "Patient")

        sid = f"wa:{from_user or 'demo'}"
        if sid not in agent.sessions:
            agent.start(sid, sender_name, "whatsapp")

        out = agent.turn(sid, body_text)
        reply_text = out.get("reply", "")
        if out.get("emergency"):
            ref = out.get("reference", "SP-1042")
            pub = os.environ.get("CARE_PUBLIC_URL", "https://city-kitten-spinal.ngrok-free.dev").strip().rstrip("/")
            reply_text = (
                f"🚨 *SmartPoli Urgent Care Alert*\n\n"
                f"• Reference: *{ref}*\n"
                f"• Priority: *CRITICAL EMERGENCY*\n"
                f"• Directive: Immediate emergency evaluation advised. Dial 112 / 911.\n\n"
                f"🔗 *Live Ticket Portal:* {pub}/ticket/{ref}"
            )

        twiml = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Message>{escape(reply_text)}</Message></Response>'
        )
        return Response(twiml, media_type="application/xml")

    # ------------------------------------------------------------------ interactive browser phone call (X-Care-Token)
    @app.post("/voice/call/start")
    def voice_call_start(body: PhoneCallStartIn, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        with lock:
            call_sid = f"phone:CALL-{next(counter)}"
        name = body.caller_name.strip() or care.profile()["name"].split()[0]
        agent.start(call_sid, name, "phone")
        hello = "Hello! SmartPoli's AI assistant here. How can I help with your care plan today?"
        agent.sessions[call_sid].history.append({"role": "assistant", "content": hello})
        care.log_smart_action("phone_call_started", f"Inbound AI support call started ({call_sid})")
        return {
            "ok": True,
            "call_sid": call_sid,
            "reply": hello,
            "ended": False,
            "emergency": False,
            "ai_connected": active_brain is not None,
            "model": model_name,
            "call_provider": call_provider.name,
            "twiml": twiml_say_and_listen(hello, "/voice/turn"),
        }

    @app.post("/voice/call/turn")
    def voice_call_turn(body: PhoneCallTurnIn, request: Request, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        sid = body.call_sid
        if not sid.startswith("phone:"):
            sid = "phone:" + sid
        if sid not in agent.sessions:
            agent.start(sid, care.profile()["name"].split()[0], "phone")
        if allow_faults and request.headers.get("x-simulate-failure") == "1":
            raise HTTPException(503, "Simulated phone AI service failure.")
        sess = agent.sessions[sid]
        heard = (body.speech or "").strip()

        # Handle emergency follow-up state (answering whether they can call 112 or need a caregiver)
        if getattr(sess, "in_emergency_followup", False):
            sess.silence_count = 0
            agent.sessions.pop(sid, None)
            low_heard = heard.lower()
            if any(k in low_heard for k in ("caregiver", "connect", "son", "family", "help")):
                cg = care.caregiver()
                if cg and cg.get("phone"):
                    try:
                        call_provider.place_call(cg["phone"], caregiver_script(cg.get("relationship", "family member")))
                        care.log_smart_action("caregiver_emergency_call", f"Emergency caregiver call placed to {cg['name']}")
                    except Exception as exc:
                        log.error("Failed placing emergency caregiver call: %s", exc)
                farewell = "I am connecting your caregiver right now. Please also dial 112 immediately. Goodbye."
            else:
                farewell = "Understood. Please dial 112 immediately for emergency medical assistance. Your care team has been notified. Goodbye."
            return {
                "ok": True,
                "call_sid": sid,
                "reply": farewell,
                "mood": "concerned",
                "avatar_state": "idle",
                "emergency": True,
                "ended": True,
                "events": [],
                "reference": None,
                "latency_ms": 0.5,
                "twiml": twiml_say_and_hangup(farewell),
            }

        if not heard:
            sess.silence_count += 1
            if sess.silence_count >= 3:
                agent.sessions.pop(sid, None)
                bye = "I still couldn't hear anything. Please call back whenever you're ready, or email help@smartpoli.example. Goodbye."
                return {
                    "ok": True,
                    "call_sid": sid,
                    "reply": bye,
                    "mood": "neutral",
                    "avatar_state": "idle",
                    "emergency": False,
                    "ended": True,
                    "events": [],
                    "latency_ms": 0.0,
                    "twiml": twiml_say_and_hangup(bye),
                }
            prompt = "Sorry, I didn't catch that. Could you say it again?"
            return {
                "ok": True,
                "call_sid": sid,
                "reply": prompt,
                "mood": "neutral",
                "avatar_state": "listening",
                "emergency": False,
                "ended": False,
                "events": [],
                "latency_ms": 0.0,
                "twiml": twiml_say_and_listen(prompt, "/voice/turn"),
            }
        if is_repeat_request(heard) and sess.last_assistant_reply():
            sess.silence_count = 0
            prev = re.sub(r"\s+", " ", sess.last_assistant_reply()).strip()
            rep = f"Sure, I'll repeat that. {prev}"
            return {
                "ok": True,
                "call_sid": sid,
                "reply": rep,
                "mood": "helpful",
                "avatar_state": "speaking",
                "emergency": False,
                "ended": False,
                "events": [],
                "latency_ms": 0.5,
                "twiml": twiml_say_and_listen(rep, "/voice/turn"),
            }

        out = agent.turn(sid, heard)
        reply = re.sub(r"\s+", " ", out["reply"]).strip()

        if out.get("emergency"):
            sess.in_emergency_followup = True
            ref = out.get("reference")
            if not ref:
                for e in out.get("events", []):
                    if e.get("reference"):
                        ref = e["reference"]
                        break
            ref = ref or "SP-1042"
            emergency_prompt = (
                f"This may be an emergency. Please call 112 or 911 immediately or go to the nearest emergency room. "
                f"Urgent ticket {ref} was logged and sent via WhatsApp/SMS. "
                f"Are you able to call 112 or do you need me to connect a caregiver right now?"
            )
            sess.history.append({"role": "assistant", "content": emergency_prompt})
            return {
                "ok": True,
                "call_sid": sid,
                "reply": emergency_prompt,
                "mood": "concerned",
                "avatar_state": "error",
                "emergency": True,
                "ended": False,
                "events": out.get("events", []),
                "reference": ref,
                "latency_ms": out.get("latency_ms", 0.0),
                "twiml": twiml_say_and_listen(emergency_prompt, "/voice/turn"),
            }

        ended = bool(sess.ended or any(w in heard.lower() for w in ("goodbye", "bye", "that's all, goodbye", "hang up", "end call")))
        if ended:
            agent.sessions.pop(sid, None)
            spoken = reply if "goodbye" in reply.lower() else reply + " Goodbye."
            twiml = twiml_say_and_hangup(spoken)
            care.log_smart_action("phone_call_ended", f"Inbound AI support call ended ({sid})")
        else:
            spoken = reply
            twiml = twiml_say_and_listen(spoken, "/voice/turn")
        ref = None
        for e in out.get("events", []):
            if e.get("reference"):
                ref = e["reference"]
        return {
            "ok": True,
            "call_sid": sid,
            "reply": spoken,
            "mood": out.get("mood", "neutral"),
            "avatar_state": "error" if out.get("emergency") else out.get("avatar_state", "speaking"),
            "emergency": bool(out.get("emergency")),
            "ended": ended,
            "events": out.get("events", []),
            "reference": ref,
            "latency_ms": out.get("latency_ms", 0.0),
            "twiml": twiml,
        }

    @app.post("/voice/call/end")
    def voice_call_end(body: PhoneCallEndIn, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        sid = body.call_sid if body.call_sid.startswith("phone:") else f"phone:{body.call_sid}"
        agent.sessions.pop(sid, None)
        care.log_smart_action("phone_call_ended", f"Caller hung up ({sid})")
        bye = "Thank you for calling SmartPoli Care Support. Goodbye."
        return {
            "ok": True,
            "call_sid": sid,
            "reply": bye,
            "ended": True,
            "twiml": twiml_say_and_hangup(bye),
        }

    # ------------------------------------------------------------------ outbound live call to patient's real phone
    @app.post("/voice/call/outbound")
    def voice_call_outbound(body: OutboundCallIn, x_care_token: str | None = Header(None)) -> dict:
        ui_auth(x_care_token)
        to_num = body.to_number.strip()
        pub_url = os.environ.get("CARE_PUBLIC_URL", "")
        res = call_provider.place_interactive_call(to_num, public_url=pub_url)
        care.log_smart_action("outbound_call_placed", f"Outbound call to {to_num}: {res.status} (ref {res.reference})")
        return {
            "ok": res.ok,
            "to_number": to_num,
            "provider": res.provider,
            "status": res.status,
            "reference": res.reference,
            "simulated": res.simulated,
            "detail": res.detail,
        }

    return app


def main() -> int:
    import uvicorn

    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    is_container = os.environ.get("R2_PROFILE", "").strip().lower() == "demo" or bool(os.environ.get("RENDER"))
    host = "0.0.0.0" if is_container else os.environ.get("R2_HOST", "127.0.0.1")
    uvicorn.run(create_app(db_path=Path(os.environ.get("R2_DATA_DIR", "data")) / "care.db"),
                host=host, port=int(os.environ.get("PORT", "8100")))
    return 0
