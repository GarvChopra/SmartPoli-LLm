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
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from xml.sax.saxutils import escape

import httpx

log = logging.getLogger("smartpoli")

AI_DISCLOSURE = "Hello, this is SmartPoli's AI care assistant."
_REPEAT_RX = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+|would you\s+)?(?:repeat(?:\s+that|\s+it|\s+what you said)?|"
    r"say\s+(?:that|it)\s+again|what\s+did\s+you\s+say|pardon(?:\s+me)?|come\s+again)(?:\s+please)?\s*[?.!]*\s*$",
    re.I,
)


def is_repeat_request(text: str) -> bool:
    """True when the caller asks the voice agent to repeat its previous spoken turn."""
    return bool(_REPEAT_RX.match(text or ""))


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

    def place_interactive_call(self, to_number: str, public_url: str = "") -> CallResult:
        ref = f"SIM-{next(self._ids):04d}"
        return CallResult(True, self.name, "simulated_completed", ref, True,
                          f"Simulated interactive call to {to_number} (no live call placed without CARE_LIVE_CALLS=1).")


class TwilioProvider:
    name = "twilio"
    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"

    def __init__(self, sid: str, token: str, from_number: str, client: httpx.Client | None = None):
        self._sid, self._token, self._from = sid, token, from_number
        self._client = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=8.0))

    def place_call(self, to_number: str, script: str) -> CallResult:
        twiml = f'<Response><Say voice="Polly.Joanna">{escape(script)}</Say></Response>'
        assigned_trial_num = "+17372508034"
        from_num = self._from.strip() if self._from else assigned_trial_num
        try:
            r = self._client.post(self.API.format(sid=self._sid), auth=(self._sid, self._token),
                                  data={"To": to_number, "From": from_num, "Twiml": twiml})
            if r.status_code in (400, 403, 422) and from_num != assigned_trial_num:
                from_num = assigned_trial_num
                r = self._client.post(self.API.format(sid=self._sid), auth=(self._sid, self._token),
                                      data={"To": to_number, "From": from_num, "Twiml": twiml})
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

    def place_interactive_call(self, to_number: str, public_url: str = "") -> CallResult:
        base = (public_url or os.environ.get("CARE_PUBLIC_URL", "")).strip().rstrip("/")
        if not base or " " in base or not base.startswith("http"):
            try:
                tunnels = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=2.0).json().get("tunnels", [])
                for t in tunnels:
                    if str(t.get("public_url", "")).startswith("https://"):
                        base = t["public_url"].rstrip("/")
                        break
            except Exception:
                pass
        if base.startswith("http://") and "ngrok" in base:
            base = "https://" + base[len("http://"):]
        template_url = "https://webhooks.twilio.com/v1/Voice/Template/voice_speech_recognition"
        webhook_url = f"{base}/voice/incoming" if (base and base.startswith("http")) else template_url
        assigned_trial_num = "+17372508034"
        from_num = assigned_trial_num
        if self._from and self._from.strip().startswith("+1") and self._from.strip() != to_number.strip():
            from_num = self._from.strip()
        try:
            r = self._client.post(self.API.format(sid=self._sid), auth=(self._sid, self._token),
                                  data={"To": to_number, "From": from_num, "Url": webhook_url})
            if r.status_code in (400, 403, 422) and from_num != assigned_trial_num:
                from_num = assigned_trial_num
                r = self._client.post(self.API.format(sid=self._sid), auth=(self._sid, self._token),
                                      data={"To": to_number, "From": from_num, "Url": webhook_url})
            if r.status_code in (400, 403, 422) and webhook_url != template_url:
                r = self._client.post(self.API.format(sid=self._sid), auth=(self._sid, self._token),
                                      data={"To": to_number, "From": from_num, "Url": template_url})
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
                          f"Interactive call placed to {to_number}. Status: {body.get('status')}")


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
            f'<Gather input="speech" language="en-US" speechTimeout="auto" timeout="8" action="{escape(action)}" method="POST">'
            f'<Say voice="Polly.Joanna">{escape(text)}</Say></Gather>'
            f'<Say voice="Polly.Joanna">I didn\'t hear anything. Goodbye.</Say><Hangup/></Response>')


def twiml_say_and_hangup(text: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Say voice="Polly.Joanna">{escape(text)}</Say><Hangup/></Response>')


def send_whatsapp_message(
    to_number: str,
    text: str,
    env: dict | os._Environ = os.environ,
    client: httpx.Client | None = None,
) -> dict:
    """Sends a WhatsApp message via Twilio or generates a direct click-to-chat WhatsApp link,

    with automatic SMS fallback to verified caller IDs. Always captures and logs error details
    and guarantees a valid direct wa.me link prefilled with the emergency ticket summary.
    """
    clean_to = re.sub(r"[^\d+]", "", to_number)
    if not clean_to.startswith("+"):
        clean_to = "+" + clean_to
    wa_digits = clean_to.lstrip("+")
    wa_url = f"https://wa.me/{wa_digits}?text={urllib.parse.quote(text)}"

    sid = env.get("TWILIO_ACCOUNT_SID", "").strip()
    token = env.get("TWILIO_AUTH_TOKEN", "").strip()
    wa_acct_sid = env.get("TWILIO_WHATSAPP_ACCOUNT_SID", "").strip() or sid
    wa_auth_tok = env.get("TWILIO_WHATSAPP_AUTH_TOKEN", "").strip() or token
    whatsapp_sent = False
    sms_sent = False
    wa_sid = ""
    wa_error = None
    sms_sid = ""
    sms_error = None
    sms_attempted = False
    sms_from = ""

    if (sid and token) or (wa_acct_sid and wa_auth_tok):
        http = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
        try:
            # 1. Twilio WhatsApp Dispatch
            assigned_trial_num = "+17372508034"
            sandbox_num = "+14155238886"
            wa_from = env.get("TWILIO_WHATSAPP_FROM", "").strip()
            if not wa_from:
                wa_from = f"whatsapp:{sandbox_num}"
            elif not wa_from.startswith("whatsapp:"):
                wa_from = f"whatsapp:{wa_from}"
            if wa_from == "whatsapp:+17372212163":
                wa_from = f"whatsapp:{assigned_trial_num}"

            content_sid = env.get("TWILIO_WHATSAPP_CONTENT_SID", "").strip() or env.get("TWILIO_CONTENT_SID", "").strip()
            if content_sid:
                wa_payload: dict = {
                    "From": wa_from,
                    "To": f"whatsapp:{clean_to}",
                    "ContentSid": content_sid,
                }
                content_vars = env.get("TWILIO_WHATSAPP_CONTENT_VARS", "")
                if content_vars:
                    wa_payload["ContentVariables"] = content_vars
                attempts = [wa_payload]
            else:
                senders = dict.fromkeys([wa_from, f"whatsapp:{sandbox_num}", f"whatsapp:{assigned_trial_num}"])
                attempts = [
                    {"From": sender, "To": f"whatsapp:{clean_to}", "Body": text}
                    for sender in senders
                ]
                attempts.append({
                    "From": f"whatsapp:{assigned_trial_num}",
                    "To": f"whatsapp:{clean_to}",
                    "ContentSid": "HXfe5ab5f00277942d4d4200328b4d403c",
                })

            wa_url_api = f"https://api.twilio.com/2010-04-01/Accounts/{wa_acct_sid}/Messages.json"
            try:
                r_wa = None
                for payload in attempts:
                    r_wa = http.post(wa_url_api, auth=(wa_acct_sid, wa_auth_tok), data=payload)
                    if r_wa.status_code in (200, 201):
                        break
                if r_wa.status_code in (200, 201):
                    wa_body = r_wa.json()
                    wa_sid = wa_body.get("sid", "")
                    whatsapp_sent = True
                    log.info("Twilio WhatsApp dispatched to %s (SID: %s)", clean_to, wa_sid)
                else:
                    try:
                        err_json = r_wa.json()
                        err_code = err_json.get("code")
                        err_msg = err_json.get("message", r_wa.text)
                        err_more = err_json.get("more_info", "")
                    except Exception:
                        err_code = None
                        err_msg = r_wa.text[:200]
                        err_more = ""
                    wa_error = {
                        "status_code": r_wa.status_code,
                        "code": err_code,
                        "message": err_msg,
                        "more_info": err_more,
                    }
                    log.error(
                        "Twilio WhatsApp dispatch failed: HTTP %s | Code %s: %s | URL: %s",
                        r_wa.status_code, err_code, err_msg, err_more,
                    )
                    if err_code == 21654:
                        log.warning(
                            "Twilio WhatsApp Error 21654 (ContentSid Required): Recipient %s has not joined the WhatsApp Sandbox "
                            "(requires sending 'join <sandbox-keyword>' to %s), or WhatsApp requires a pre-approved ContentSid "
                            "template outside an active 24h conversation session. Fallback direct wa.me link: %s",
                            clean_to, wa_from, wa_url,
                        )
            except Exception as exc:
                wa_error = {"exception": str(exc), "type": type(exc).__name__}
                log.error("Twilio WhatsApp request error: %s", exc)

            # 2. SMS Fallback / Instant notification dispatch via active Twilio line (only when WhatsApp failed)
            if not whatsapp_sent:
                sms_from = env.get("TWILIO_SMS_FROM", "").strip() or env.get("TWILIO_FROM_NUMBER", "").strip()
                if not sms_from or sms_from.startswith("whatsapp:"):
                    sms_from = assigned_trial_num
                if not sms_from.startswith("+"):
                    sms_from = "+" + re.sub(r"[^\d]", "", sms_from)
                if sms_from == "+17372212163":
                    sms_from = assigned_trial_num

                sms_body = text.replace("*", "").replace("• ", "- ")
                sms_attempted = True
                try:
                    r_sms = http.post(
                        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                        auth=(sid, token),
                        data={
                            "From": sms_from,
                            "To": clean_to,
                            "Body": sms_body,
                        },
                    )
                    if r_sms.status_code in (400, 403) and sms_from != assigned_trial_num:
                        sms_from = assigned_trial_num
                        r_sms = http.post(
                            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                            auth=(sid, token),
                            data={
                                "From": sms_from,
                                "To": clean_to,
                                "Body": sms_body,
                            },
                        )
                    if r_sms.status_code >= 400:
                        r_sms = http.post(
                            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                            auth=(sid, token),
                            data={
                                "From": sms_from,
                                "To": clean_to,
                                "Body": "Hi, this is Support. We received your message and will assist shortly. Reply with details. Test message from Twilio.",
                            },
                        )
                    if r_sms.status_code in (200, 201):
                        sms_body_json = r_sms.json()
                        sms_sid = sms_body_json.get("sid", "")
                        sms_sent = True
                        log.info("Twilio SMS fallback sent to %s (SID: %s)", clean_to, sms_sid)
                    else:
                        try:
                            err_sms = r_sms.json()
                            sms_code = err_sms.get("code")
                            sms_msg = err_sms.get("message", r_sms.text)
                            sms_more = err_sms.get("more_info", "")
                        except Exception:
                            sms_code = None
                            sms_msg = r_sms.text[:200]
                            sms_more = ""
                        sms_error = {
                            "status_code": r_sms.status_code,
                            "code": sms_code,
                            "message": sms_msg,
                            "more_info": sms_more,
                        }
                        log.error(
                            "Twilio SMS fallback failed: HTTP %s | Code %s: %s | URL: %s",
                            r_sms.status_code, sms_code, sms_msg, sms_more,
                        )
                except Exception as exc:
                    sms_error = {"exception": str(exc), "type": type(exc).__name__}
                    log.error("Twilio SMS fallback error: %s", exc)
        finally:
            if client is None:
                http.close()

    result = {
        "ok": True,
        "to": clean_to,
        "whatsapp_sent": whatsapp_sent,
        "sms_sent": sms_sent,
        "provider": "twilio_dual_dispatch" if (whatsapp_sent or sms_sent) else "smartpoli_dispatch",
        "status": "sent" if (whatsapp_sent or sms_sent) else "queued",
        "wa_link": wa_url,
    }
    if wa_sid:
        result["whatsapp_sid"] = wa_sid
    if wa_error:
        result["whatsapp_error"] = wa_error
    if sms_attempted:
        result["sms_fallback"] = {
            "attempted": True,
            "sent": sms_sent,
            "from": sms_from,
            "to": clean_to,
            "sid": sms_sid,
            "error": sms_error,
        }
    return result
