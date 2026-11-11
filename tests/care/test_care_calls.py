"""Caregiver call providers, speech-to-text endpoint and the phone voice-agent webhooks."""
import httpx
import pytest
from fastapi.testclient import TestClient

from r2.brain import FakeBrain
from r2.care_actions import create_app
from r2.care_calls import (
    AI_DISCLOSURE, SimulationProvider, TwilioProvider, caregiver_script, live_calls_configured, make_provider,
    twilio_signature, valid_twilio_signature,
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


def test_phone_emergency_speaks_guidance_then_hangs_up_without_asking_the_model():
    c, brain = phone_app([], voice_simulation=True)
    c.post("/voice/incoming", data={"CallSid": "CA2"})
    r = c.post("/voice/turn", data={"CallSid": "CA2", "SpeechResult": "I think I took too many pills"})
    assert "911" in r.text and "<Hangup/>" in r.text and brain.calls == []


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
