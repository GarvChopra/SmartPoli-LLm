"""Caregiver call providers, speech-to-text endpoint and the phone voice-agent webhooks."""
import httpx
import pytest
from fastapi.testclient import TestClient

from r2.brain import FakeBrain
from r2.care_actions import create_app
from r2.care_calls import (
    AI_DISCLOSURE, SimulationProvider, TwilioProvider, caregiver_script, live_calls_configured, make_provider,
    send_whatsapp_message, twilio_signature, valid_twilio_signature,
)
from r2.care_meds import CareStore
from r2.types import Decision, ToolCall
from walker import KEY


# ---------------------------------------------------------------- providers
def test_simulation_provider_is_clearly_labelled_and_places_nothing():
    r = SimulationProvider().place_call("555-0142", "hi")
    pub = r.public()
    assert r.simulated and r.status == "simulated_completed" and r.reference.startswith("SIM-")
    assert pub["label"].startswith("SIMULATED")


def test_caregiver_script_identifies_ai_and_shares_no_medical_information():
    s = caregiver_script("son")
    assert s.startswith(AI_DISCLOSURE) and "son" in s
    for word in ("dizzy", "medicine", "medication", "symptom", "dose", "pill"):
        assert word not in s.lower()


def test_live_calls_need_all_credentials_and_the_explicit_opt_in_flag():
    base = {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "t", "TWILIO_FROM_NUMBER": "+15550001111"}
    assert not live_calls_configured({})
    assert not live_calls_configured(base)  # credentials alone do NOT enable real calls
    assert not live_calls_configured({**base, "CARE_LIVE_CALLS": "0"})
    assert live_calls_configured({**base, "CARE_LIVE_CALLS": "1"})
    assert isinstance(make_provider(base), SimulationProvider)
    assert isinstance(make_provider({**base, "CARE_LIVE_CALLS": "1"}), TwilioProvider)


def test_twilio_provider_reports_exactly_what_twilio_returned():
    seen = {}

    def ok(request):
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(201, json={"sid": "CA123", "status": "queued"})

    p = TwilioProvider("AC1", "tok", "+15550001111", client=httpx.Client(transport=httpx.MockTransport(ok)))
    r = p.place_call("+15550002222", "Hello <b>there</b>")
    assert r.ok and r.status == "queued" and r.reference == "CA123" and r.simulated is False
    assert "queued" in r.detail or "not confirmed" in r.detail
    assert "%3CResponse%3E" in seen["body"] and "%26lt%3Bb%26gt%3B" in seen["body"]  # script is XML-escaped

    bad = TwilioProvider("AC1", "tok", "+1", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"message": "The number is unverified"}))))
    rb = bad.place_call("+1", "x")
    assert not rb.ok and rb.status == "failed" and "unverified" in rb.detail

    def boom(request):
        raise httpx.ConnectError("down")

    rc = TwilioProvider("AC1", "tok", "+1", client=httpx.Client(transport=httpx.MockTransport(boom))).place_call("+1", "x")
    assert not rc.ok and rc.status == "failed"


# ---------------------------------------------------------------- speech to text
def test_transcribe_posts_audio_to_groq_whisper_and_returns_text():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["has_model"] = b"whisper-large-v3-turbo" in request.content
        return httpx.Response(200, json={"text": " When is my next medicine? "})

    app = create_app(api_key=KEY, use_default_brain=False, groq_api_key="gsk_test", store=CareStore(),
                     stt_client=httpx.Client(transport=httpx.MockTransport(handler)))
    c = TestClient(app)
    ui = {"X-Care-Token": app.state.ui_token}
    r = c.post("/voice/transcribe", content=b"RIFF" + b"\0" * 5000, headers=ui).json()
    assert r["text"] == "When is my next medicine?"
    assert seen["url"].endswith("/audio/transcriptions") and seen["auth"] == "Bearer gsk_test" and seen["has_model"]


def test_transcribe_guards_token_audio_size_key_and_provider_errors():
    app = create_app(api_key=KEY, use_default_brain=False, groq_api_key="gsk_test", store=CareStore(),
                     stt_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={}))))
    c = TestClient(app)
    ui = {"X-Care-Token": app.state.ui_token}
    assert c.post("/voice/transcribe", content=b"x" * 5000).status_code == 401
    assert c.post("/voice/transcribe", content=b"x", headers=ui).status_code == 422
    assert c.post("/voice/transcribe", content=b"x" * 5000, headers=ui).status_code == 429


def test_transcribe_without_a_key_says_what_is_missing(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    app = create_app(api_key=KEY, use_default_brain=False, groq_api_key="", store=CareStore())
    c = TestClient(app)
    r = c.post("/voice/transcribe", content=b"x" * 5000, headers={"X-Care-Token": app.state.ui_token})
    assert r.status_code == 503 and "GROQ_API_KEY" in r.json()["message"]


# ---------------------------------------------------------------- phone voice agent
def phone_app(script, **kw):
    brain = FakeBrain(script)
    app = create_app(api_key=KEY, brain=brain, store=CareStore(), **kw)
    return TestClient(app), brain


def test_phone_agent_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CARE_VOICE_SIMULATION", raising=False)
    c, _ = phone_app([])
    assert c.post("/voice/incoming", data={"CallSid": "CA1"}).status_code == 503


def test_phone_call_flow_greets_listens_replies_remembers_and_ends():
    c, brain = phone_app([Decision(final="[mood:neutral] Your next medicine is Examplol at 08:00."),
                          Decision(final="[mood:neutral] Of course."),
                          Decision(tool_call=ToolCall("end_call", {})), Decision(final="[mood:happy] Take care. Goodbye.")],
                         voice_simulation=True)
    hello = c.post("/voice/incoming", data={"CallSid": "CA1"})
    assert hello.headers["content-type"].startswith("application/xml")
    assert "SmartPoli's AI assistant" in hello.text and '<Gather input="speech"' in hello.text
    t1 = c.post("/voice/turn", data={"CallSid": "CA1", "SpeechResult": "when is my next medicine"})
    assert "Examplol at 08:00" in t1.text and "<Gather" in t1.text and "[mood" not in t1.text
    c.post("/voice/turn", data={"CallSid": "CA1", "SpeechResult": "thank you"})
    assert "when is my next medicine" in " ".join(m["content"] for m in brain.calls[1])  # call memory
    bye = c.post("/voice/turn", data={"CallSid": "CA1", "SpeechResult": "that's all, goodbye"})
    assert "<Hangup/>" in bye.text and "Goodbye" in bye.text and "<Gather" not in bye.text
    phone_tools = [t["function"]["name"] for t in brain.tools_seen[0]]
    assert "end_call" in phone_tools and "add_medication_reminder" not in phone_tools


def test_phone_emergency_speaks_guidance_and_listens_then_hangs_up_gracefully():
    c, brain = phone_app([], voice_simulation=True)
    c.post("/voice/incoming", data={"CallSid": "CA2"})
    r = c.post("/voice/turn", data={"CallSid": "CA2", "SpeechResult": "I think I took too many pills"})
    assert ("911" in r.text or "112" in r.text) and "<Gather" in r.text and brain.calls == []
    assert "SP-" in r.text
    assert "connect a caregiver" in r.text

    # Answer gracefully hangs up
    r2 = c.post("/voice/turn", data={"CallSid": "CA2", "SpeechResult": "I can call 112, thank you"})
    assert "<Hangup/>" in r2.text and "Goodbye" in r2.text


def test_phone_emergency_connect_caregiver_flow():
    c, brain = phone_app([], voice_simulation=True)
    c.post("/voice/incoming", data={"CallSid": "CA-EMERG-CG"})
    r = c.post("/voice/turn", data={"CallSid": "CA-EMERG-CG", "SpeechResult": "chest pain and trouble breathing"})
    assert "<Gather" in r.text
    r2 = c.post("/voice/turn", data={"CallSid": "CA-EMERG-CG", "SpeechResult": "please connect my caregiver"})
    assert "<Hangup/>" in r2.text
    assert "connecting your caregiver" in r2.text.lower()


def test_phone_silence_prompts_again_and_caregiver_contact_is_not_available_by_phone():
    c, brain = phone_app([ToolDecision := Decision(tool_call=ToolCall("request_caregiver_contact", {"reason": "x"})),
                          Decision(final="I can only do that from the app.")], voice_simulation=True)
    assert "didn't catch that" in c.post("/voice/turn", data={"CallSid": "CA3", "SpeechResult": ""}).text
    r = c.post("/voice/turn", data={"CallSid": "CA3", "SpeechResult": "call my son"})
    assert "only do that from the app" in r.text
    assert any(m.get("role") == "tool" and m["content"].startswith("ERROR:") for m in brain.calls[1])


def test_twilio_signature_is_enforced_when_the_auth_token_is_set(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("CARE_PUBLIC_URL", "https://care.example.test")
    monkeypatch.delenv("CARE_VOICE_SIMULATION", raising=False)
    c, _ = phone_app([])
    form = {"CallSid": "CA9"}
    assert c.post("/voice/incoming", data=form).status_code == 403
    assert c.post("/voice/incoming", data=form, headers={"X-Twilio-Signature": "forged"}).status_code == 403
    sig = twilio_signature("secret-token", "https://care.example.test/voice/incoming", form)
    assert valid_twilio_signature("secret-token", "https://care.example.test/voice/incoming", form, sig)
    assert c.post("/voice/incoming", data=form, headers={"X-Twilio-Signature": sig}).status_code == 200


def test_phone_repeat_request_and_multi_silence_hangup():
    c, brain = phone_app(
        [Decision(final="[mood:neutral] Please check if notifications are enabled for SmartPoli.")],
        voice_simulation=True,
    )
    c.post("/voice/incoming", data={"CallSid": "CA-REP"})
    t1 = c.post("/voice/turn", data={"CallSid": "CA-REP", "SpeechResult": "my reminders are missing"})
    assert "notifications are enabled" in t1.text

    # Asking to repeat should repeat the last assistant reply without calling the LLM again
    rep = c.post("/voice/turn", data={"CallSid": "CA-REP", "SpeechResult": "Can you repeat that please?"})
    assert "repeat that" in rep.text.lower() and "notifications are enabled" in rep.text
    assert len(brain.calls) == 1

    # Three consecutive empty/silent turns should politely hang up
    s1 = c.post("/voice/turn", data={"CallSid": "CA-REP", "SpeechResult": ""})
    assert "<Gather" in s1.text
    s2 = c.post("/voice/turn", data={"CallSid": "CA-REP", "SpeechResult": ""})
    assert "<Gather" in s2.text
    s3 = c.post("/voice/turn", data={"CallSid": "CA-REP", "SpeechResult": ""})
    assert "<Hangup/>" in s3.text and "Goodbye" in s3.text


def test_interactive_browser_phone_call_lifecycle_and_ticket_creation():
    c, brain = phone_app(
        [
            Decision(final="[mood:helpful] Are notifications enabled for SmartPoli on your phone?"),
            Decision(final="[mood:helpful] Since all three checks passed, would you like me to create a support ticket?"),
            Decision(tool_call=ToolCall("create_support_ticket", {"category": "reminder_issue", "summary": "Notifications, DND, and timezone checked on phone call"})),
            Decision(final="[mood:happy] I created ticket SP-1002 for you. Would you also like a support callback?"),
        ],
        voice_simulation=True,
    )
    ui = {"X-Care-Token": c.app.state.ui_token}
    assert c.post("/voice/call/start", json={"caller_name": "Alex"}).status_code == 401

    start = c.post("/voice/call/start", json={"caller_name": "Alex"}, headers=ui).json()
    assert start["ok"] and start["call_sid"].startswith("phone:CALL-")
    assert "SmartPoli's AI assistant" in start["reply"] and "<Gather" in start["twiml"]
    sid = start["call_sid"]

    # Turn 1: Missing reminders
    t1 = c.post("/voice/call/turn", json={"call_sid": sid, "speech": "My reminders are missing"}, headers=ui).json()
    assert t1["ok"] and "notifications enabled" in t1["reply"] and isinstance(t1["latency_ms"], (int, float))

    # Repeat command on browser phone call
    rep = c.post("/voice/call/turn", json={"call_sid": sid, "speech": "repeat that"}, headers=ui).json()
    assert "notifications enabled" in rep["reply"]

    # Turn 2: Caller confirms all checks tried
    t2 = c.post("/voice/call/turn", json={"call_sid": sid, "speech": "Yes, notifications are on, Do Not Disturb is off, and timezone matches"}, headers=ui).json()
    assert "create a support ticket" in t2["reply"]

    # Turn 3: Caller approves ticket creation -> returns SP-xxxx reference
    t3 = c.post("/voice/call/turn", json={"call_sid": sid, "speech": "Yes please create the ticket"}, headers=ui).json()
    assert t3["reference"] and t3["reference"].startswith("SP-")
    assert any(e["type"] == "ticket" for e in t3["events"])

    # End call cleanly
    end = c.post("/voice/call/end", json={"call_sid": sid}, headers=ui).json()
    assert end["ok"] and end["ended"] is True and "<Hangup/>" in end["twiml"]


# ---------------------------------------------------------------- WhatsApp & SMS dispatch
def test_send_whatsapp_message_direct_wa_link_without_credentials():
    res = send_whatsapp_message("+91 73073-60971", "Emergency Alert: SP-1042", env={})
    assert res["ok"] is True
    assert res["to"] == "+917307360971"
    assert res["status"] == "queued"
    assert res["provider"] == "smartpoli_dispatch"
    assert res["wa_link"].startswith("https://wa.me/917307360971?text=")
    assert "Emergency%20Alert%3A%20SP-1042" in res["wa_link"]
    assert "sms_fallback" not in res


def test_send_whatsapp_message_success():
    reqs = []

    def mock_handler(request: httpx.Request):
        reqs.append(request)
        sid = f"SM_ID_{len(reqs)}"
        return httpx.Response(201, json={"sid": sid, "status": "sent"})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    env = {"TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "tok_test"}
    res = send_whatsapp_message("+917307360971", "Test message", env=env, client=client)

    assert res["ok"] is True
    assert res["whatsapp_sent"] is True
    assert res["whatsapp_sid"] == "SM_ID_1"
    assert res["sms_sent"] is True
    assert res["sms_fallback"]["sent"] is True
    assert res["sms_fallback"]["sid"] == "SM_ID_2"
    assert res["provider"] == "twilio_dual_dispatch"
    assert res["wa_link"].startswith("https://wa.me/917307360971?text=")
    assert len(reqs) == 2
    assert "From=whatsapp%3A%2B14155238886" in reqs[0].content.decode()
    assert "From=%2B17372508034" in reqs[1].content.decode()


def test_send_whatsapp_message_captures_21654_and_triggers_sms_fallback():
    calls = []

    def mock_handler(request: httpx.Request):
        calls.append(request)
        if len(calls) == 1:
            # First request: WhatsApp fails with 21654 ContentSid Required
            return httpx.Response(
                400,
                json={
                    "code": 21654,
                    "message": "ContentSid Required",
                    "more_info": "https://www.twilio.com/docs/errors/21654",
                    "status": 400,
                },
            )
        # Second request: SMS fallback succeeds
        return httpx.Response(201, json={"sid": "SM_SMS_456", "status": "sent"})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    env = {"TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "tok_test"}
    res = send_whatsapp_message("+917307360971", "*Emergency* alert", env=env, client=client)

    assert res["ok"] is True
    assert res["whatsapp_sent"] is False
    assert res["whatsapp_error"]["code"] == 21654
    assert res["whatsapp_error"]["status_code"] == 400
    assert "ContentSid Required" in res["whatsapp_error"]["message"]
    # Direct click-to-chat WhatsApp link is ALWAYS returned for 1-click fallback
    assert res["wa_link"].startswith("https://wa.me/917307360971?text=")
    # SMS fallback executed and succeeded
    assert res["sms_sent"] is True
    assert res["sms_fallback"]["attempted"] is True
    assert res["sms_fallback"]["sent"] is True
    assert res["sms_fallback"]["sid"] == "SM_SMS_456"
    assert res["sms_fallback"]["from"] == "+17372508034"
    assert len(calls) == 2


def test_send_whatsapp_message_captures_sms_failure_payload():
    calls = []

    def mock_handler(request: httpx.Request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(400, json={"code": 21654, "message": "ContentSid Required"})
        return httpx.Response(400, json={"code": 572006, "message": "Invalid template name. Trial accounts can only use predefined SMS templates."})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    env = {"TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "tok_test"}
    res = send_whatsapp_message("+917307360971", "Emergency alert", env=env, client=client)

    assert res["ok"] is True
    assert res["whatsapp_sent"] is False
    assert res["sms_sent"] is False
    assert res["whatsapp_error"]["code"] == 21654
    assert res["sms_fallback"]["attempted"] is True
    assert res["sms_fallback"]["sent"] is False
    assert res["sms_fallback"]["error"]["code"] == 572006
    assert "Trial accounts can only use predefined SMS templates" in res["sms_fallback"]["error"]["message"]
    assert res["wa_link"].startswith("https://wa.me/917307360971?text=")
