"""SmartPoli Care Companion: caregiver call providers and the phone voice-agent webhooks.

Providers
- SimulationProvider (default): places NO call. Returns an unmistakably labelled simulated result.
- TwilioProvider: places a REAL call through Twilio's REST API. It is only constructed when
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER are set AND CARE_LIVE_CALLS=1. Even then the
  caller (r2.care_actions) only invokes it after the user pressed Confirm in the app. The result recorded
  is exactly what Twilio returns ("queued", a call SID, or an error): never an assumed success.
  NOTE: this class has NOT been exercised against Twilio in this repository (no credentials were available).

Phone voice agent (incoming calls)
  Phone audio -> Twilio speech recognition (<Gather input="speech">) -> Groq conversation engine
  (r2.care_agent) -> Twilio text-to-speech (<Say>) -> phone audio.
  The webhooks below only build TwiML from the engine's reply. They are protected by Twilio's request
  signature (X-Twilio-Signature) when TWILIO_AUTH_TOKEN is set.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import os
from dataclasses import dataclass
from xml.sax.saxutils import escape

import httpx

AI_DISCLOSURE = "Hello, this is SmartPoli's AI care assistant."


def caregiver_script(relationship: str = "family member") -> str:
    """What the AI says to the caregiver: it identifies itself, shares NO medical details."""
    return (f"{AI_DISCLOSURE} I'm calling because your {relationship} asked me to let you know they would "
            "like you to check in on them. Could you please contact them when you can? Thank you. Goodbye.")


@dataclass
class CallResult:
    ok: bool
    provider: str  # "simulation" | "twilio"
    status: str    # simulation: "simulated_completed" | twilio: queued/ringing/... or "failed"
    reference: str
    simulated: bool
    detail: str = ""

    def public(self) -> dict:
        return {"ok": self.ok, "provider": self.provider, "status": self.status, "reference": self.reference,
                "simulated": self.simulated, "detail": self.detail,
                "label": "SIMULATED: no real call or message was placed." if self.simulated
                         else "LIVE: handed to the telephony provider."}


class SimulationProvider:
    name = "simulation"
    _ids = itertools.count(1)

    def place_call(self, to_number: str, script: str) -> CallResult:
        ref = f"SIM-{next(self._ids):04d}"
        return CallResult(True, self.name, "simulated_completed", ref, True,
                          "Simulated call script (not delivered to anyone): " + script)


class TwilioProvider:
    name = "twilio"
    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"

    def __init__(self, sid: str, token: str, from_number: str, client: httpx.Client | None = None):
        self._sid, self._token, self._from = sid, token, from_number
        self._client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=8.0))

    def place_call(self, to_number: str, script: str) -> CallResult:
        twiml = f'<Response><Say voice="Polly.Joanna">{escape(script)}</Say></Response>'
        try:
            r = self._client.post(self.API.format(sid=self._sid), auth=(self._sid, self._token),
                                  data={"To": to_number, "From": self._from, "Twiml": twiml})
        except httpx.HTTPError as exc:
            return CallResult(False, self.name, "failed", "", False, f"Could not reach Twilio: {type(exc).__name__}")
        if r.status_code >= 300:
            try:
                msg = str(r.json().get("message", ""))[:160]
            except ValueError:
                msg = r.text[:160]
            return CallResult(False, self.name, "failed", "", False, f"Twilio error {r.status_code}: {msg}")
        body = r.json()
        return CallResult(True, self.name, str(body.get("status", "queued")), str(body.get("sid", "")), False,
                          "Call handed to Twilio. Delivery is not confirmed until its status says completed.")


def live_calls_configured(env=os.environ) -> bool:
    return bool(env.get("TWILIO_ACCOUNT_SID") and env.get("TWILIO_AUTH_TOKEN") and env.get("TWILIO_FROM_NUMBER")
                and env.get("CARE_LIVE_CALLS") == "1")


def make_provider(env=os.environ):
    if live_calls_configured(env):
        return TwilioProvider(env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"], env["TWILIO_FROM_NUMBER"])
    return SimulationProvider()


# --------------------------------------------------------------------------- Twilio webhook helpers
def twilio_signature(auth_token: str, url: str, params: dict) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def valid_twilio_signature(auth_token: str, url: str, params: dict, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(twilio_signature(auth_token, url, params), signature)


def twiml_say_and_listen(text: str, action: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Gather input="speech" speechTimeout="auto" timeout="6" action="{escape(action)}" method="POST">'
            f'<Say voice="Polly.Joanna">{escape(text)}</Say></Gather>'
            f'<Say voice="Polly.Joanna">I didn\'t hear anything. Goodbye.</Say><Hangup/></Response>')


def twiml_say_and_hangup(text: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Say voice="Polly.Joanna">{escape(text)}</Say><Hangup/></Response>')
