"""Penny support backend for the eGain hackathon.

eGain hosts the chat agent, intents and workflows. This small service gives that agent real actions:
eGain's "Actions" call these HTTP endpoints. Everything is fictional demo data (Maple Credit Union).

Run: r2 support   (default http://127.0.0.1:8090, docs at /docs)
Expose with a tunnel (cloudflared / ngrok) or deploy the Dockerfile so eGain can reach it.
"""
from __future__ import annotations

import itertools
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

# Demo cards only. Never put real card data here.
CARDS: dict[str, dict] = {
    "4242": {"holder": "Demo Member", "status": "active"},
    "1111": {"holder": "Demo Member", "status": "active"},
}
SPEEDS = {"standard": {"days": "5-7 business days", "fee": 0}, "expedited": {"days": "2 business days", "fee": 15}}
MAX_AUTO_DISPUTE_USD = 500  # above this a human must review

_lock = threading.Lock()
_ref = itertools.count(1001)
_log: list[dict] = []
KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "docs" / "support-kb"


def _record(kind: str, **data) -> str:
    with _lock:
        ref = f"MCU-{next(_ref)}"
        _log.append({"ref": ref, "kind": kind, "at": datetime.now(timezone.utc).isoformat(), **data})
    return ref


def _check_last4(last4: str) -> str:
    if not re.fullmatch(r"\d{4}", last4 or ""):
        raise HTTPException(422, "last4 must be exactly 4 digits. Never send a full card number or PIN.")
    if last4 not in CARDS:
        raise HTTPException(404, "No demo card with those last 4 digits. Escalate to a human agent.")
    return last4


class FreezeIn(BaseModel):
    last4: str
    reason: str = Field("lost", pattern="^(lost|stolen|fraud|other)$")


class ReplaceIn(BaseModel):
    last4: str
    speed: str = Field("standard", pattern="^(standard|expedited)$")


class CallbackIn(BaseModel):
    name: str
    phone: str
    window: str = "morning"


class DisputeIn(BaseModel):
    last4: str
    amount_usd: float = Field(gt=0)
    merchant: str


class StartIn(BaseModel):
    name: str = ""
    topic: str = "other"
    urgent: bool = False


class ChatIn(BaseModel):
    session: str
    message: str


def create_app(api_key: str = "") -> FastAPI:
    app = FastAPI(title="Penny support actions (demo)")

    def auth(key: str | None) -> None:
        if api_key and key != api_key:
            raise HTTPException(401, "bad or missing X-API-Key")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/actions/freeze_card")
    def freeze_card(body: FreezeIn, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        last4 = _check_last4(body.last4)
        CARDS[last4]["status"] = "frozen"
        ref = _record("freeze", last4=last4, reason=body.reason)
        msg = f"Card ending {last4} is frozen. Reference {ref}."
        if body.reason in ("stolen", "fraud"):
            msg += " Fraud team flagged. Please review recent transactions."
        return {"ok": True, "reference": ref, "status": "frozen", "message": msg}

    @app.post("/actions/order_replacement")
    def order_replacement(body: ReplaceIn, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        last4 = _check_last4(body.last4)
        if CARDS[last4]["status"] != "frozen":
            raise HTTPException(409, "Freeze the card first, then order a replacement.")
        opt = SPEEDS[body.speed]
        ref = _record("replacement", last4=last4, speed=body.speed)
        return {"ok": True, "reference": ref, "arrives": opt["days"], "fee_usd": opt["fee"],
                "message": f"Replacement ordered, arrives in {opt['days']}, fee ${opt['fee']}. Reference {ref}."}

    @app.post("/actions/book_callback")
    def book_callback(body: CallbackIn, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        if not re.fullmatch(r"[0-9+()\- ]{7,20}", body.phone):
            raise HTTPException(422, "invalid phone number")
        ref = _record("callback", name=body.name, window=body.window)  # phone deliberately not logged
        return {"ok": True, "reference": ref, "message": f"Callback booked for the {body.window}. Reference {ref}."}

    @app.post("/actions/open_dispute")
    def open_dispute(body: DisputeIn, x_api_key: str | None = Header(None)) -> dict:
        auth(x_api_key)
        last4 = _check_last4(body.last4)
        if body.amount_usd > MAX_AUTO_DISPUTE_USD:
            # Good judgment: large disputes are not auto-filed.
            return {"ok": False, "escalate": True,
                    "message": f"Disputes over ${MAX_AUTO_DISPUTE_USD} need a human reviewer. Offer live chat or a callback."}
        ref = _record("dispute", last4=last4, amount=body.amount_usd, merchant=body.merchant)
        return {"ok": True, "reference": ref, "message": f"Dispute opened with {body.merchant}. Reference {ref}."}

    @app.get("/actions/log")
    def log(x_api_key: str | None = Header(None)) -> list[dict]:
        auth(x_api_key)
        return list(_log)

    @app.post("/actions/reset")
    def reset(x_api_key: str | None = Header(None)) -> dict:
        """Unfreeze demo cards between demo takes."""
        auth(x_api_key)
        for c in CARDS.values():
            c["status"] = "active"
        with _lock:
            _log.clear()
        return {"ok": True}

    # ---- self-contained demo chat (pre-chat + guided workflows + escalation + actions) ----
    from fastapi.responses import HTMLResponse

    from r2 import support_chat

    kb = support_chat.load_kb()
    sessions: dict[str, support_chat.Session] = {}
    handlers = {"freeze_card": (FreezeIn, freeze_card), "order_replacement": (ReplaceIn, order_replacement),
                "book_callback": (CallbackIn, book_callback), "open_dispute": (DisputeIn, open_dispute)}

    def act(name: str, payload: dict) -> dict:
        model, fn = handlers[name]
        try:
            return fn(model(**payload), x_api_key=api_key or None)
        except HTTPException as e:
            return {"ok": False, "message": str(e.detail)}

    @app.post("/chat/start")
    def chat_start(body: StartIn) -> dict:
        sid = _record("session", topic=body.topic, urgent=body.urgent)
        s = sessions[sid] = support_chat.Session(body.name, body.topic, body.urgent)
        return {"session": sid, "reply": support_chat.greet(s)}

    @app.post("/chat")
    def chat(body: ChatIn) -> dict:
        s = sessions.get(body.session)
        if not s:
            raise HTTPException(404, "unknown session, call /chat/start")
        return {"reply": support_chat.handle(s, body.message[:500], kb, act)}

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return (Path(__file__).parent / "support_page.html").read_text(encoding="utf-8")

    return app


def main() -> int:
    import os

    import uvicorn

    port = int(os.environ.get("SUPPORT_PORT", "8090"))
    uvicorn.run(create_app(os.environ.get("SUPPORT_API_KEY", "")), host="0.0.0.0", port=port)
    return 0
