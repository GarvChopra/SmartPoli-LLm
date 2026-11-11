# SmartPoli Care Companion

**eGain AI Agent Hackathon Submission & Animated Desktop Healthcare Companion**  
**Author:** Garv Chopra (`@GarvChopra`)  
**Industry:** Healthcare Services (Medication-Reminder Support & Proactive Care)  
**Use Case:** Animated floating desktop AI companion, Groq-powered multi-turn conversation, Smart Actions & Proactive Care (verified health guides, OS settings launcher, screen navigation), scheduled medication reminders, explicit-confirmation caregiver escalation, and eGain knowledge/workflow/action integration.

---

## 1. Overview

**SmartPoli Care Companion** transforms traditional medication-reminder support into a warm, expressive, floating desktop AI healthcare companion built on the existing R2 runtime and layered on top of the **eGain AI Agent** knowledge base, process workflows, and action endpoints:

1. **Animated Floating Desktop Companion (`r2/care_companion.py`):**
   - Lives on the user's desktop as a draggable, always-on-top transparent character rendered with 4x supersampled Pillow graphics (`#010203` transparent key) and persistent position memory (`data/care_position.json`).
   - Features **11 expressive animation states**: `idle`, `listening`, `thinking`, `speaking`, `reminder`, `happy`, `concerned`, `escalating`, `helpful`, `error`, and `sleeping` (plus light/dark themes and reduced-motion accessibility).
   - Expands on click into a full conversational & proactive-care panel (`Chat`, `Meds`, `Help & Settings`, `Caregiver`, `Activity`).
2. **Smart Actions & Proactive Care (`r2/care_smart_actions.py`):**
   - Uses Groq structured function calling to take safe, allowlisted actions on the user's PC:
     - **Open Verified Health & Device Resources (`open_trusted_resource`):** Launches official NIH/NHLBI, MedlinePlus, American Heart Association, and American Lung Association guides/videos in the browser (e.g., metered-dose / dry-powder / soft-mist inhaler technique, NIH sleep-hygiene habits, AHA home blood pressure measurement)—never random unverified links.
     - **Step-by-Step Instruction Cards (`show_instruction_card`):** Displays clear device & self-care checklists directly in the companion.
     - **System Notification Settings (`open_system_notification_settings`):** Opens Windows Notification Settings (`ms-settings:notifications`) and switches to the SmartPoli troubleshooting screen when reminders fail.
     - **Companion Screen Navigation (`navigate_companion_screen`):** Automatically navigates between `chat`, `schedule`, `reminders`, `troubleshooting`, `caregiver`, and `activity`.
     - **Confirmed Medication Reminder Creation (`propose_medication_reminder`):** Proposes new medication reminders with an explicit Confirm/Cancel card before adding to the schedule.
3. **Medication Schedule & Reminder Engine (`r2/care_meds.py`):**
   - SQLite-backed schedule (`data/care.db`) with due-reminder detection, `Mark Taken`, `Snooze (1–180 min)`, `Log Missed Dose`, and strict clinical safety guardrails (never diagnoses or advises changing/skipping/doubling doses).
4. **Authorized Caregiver Escalation & Telephony (`r2/care_calls.py`):**
   - When a user reports feeling unwell (non-emergency), responds with empathy, asks a gentle severity check, and offers to contact their designated caregiver (`Rohan (demo son)`).
   - Requires explicit user confirmation (`POST /caregiver/requests/{id}/confirm`) before placing a clearly labeled simulated call (`SimulationProvider`) or live Twilio call (`TwilioProvider` when configured).
5. **Voice Input & Spoken Responses:**
   - Supports microphone speech-to-text (`/voice/transcribe` via Groq `whisper-large-v3-turbo` / Web Speech API) and spoken responses (`pyttsx3` / `speechSynthesis`) synchronized with the `listening` and `speaking` character animations.
6. **eGain AI Agent Hackathon Integration (`egain/` & `r2/care_actions.py`):**
   - Retains all 10 synthetic knowledge documents (`egain/knowledge/*.md`), Pre-Chat workflow, 3-Step Reminder Troubleshooting process workflow, Human Escalation paths, and HTTP Action endpoints (`/actions/create_ticket`, `/actions/request_callback`, `/safety/check`).

---

## 2. Repository Structure

| Path | Purpose |
|---|---|
| `r2/care_companion.py` | Floating always-on-top desktop mascot (11 animation states) & expandable `tkinter` companion panel |
| `r2/care_smart_actions.py` | Allowlisted Smart Actions engine (verified NIH/NHLBI/AHA/MedlinePlus catalog, browser/settings launchers, screen navigation) |
| `r2/care_agent.py` | Multi-turn Groq tool-calling conversation engine, KB retrieval, mood/avatar state tagging, and safety guardrails |
| `r2/care_meds.py` | SQLite medication schedule, due-reminder engine, reminder proposals, caregiver consent records, and activity log |
| `r2/care_calls.py` | Caregiver call orchestration (`SimulationProvider` by default, `TwilioProvider` optional) & TwiML voice webhooks |
| `r2/care_actions.py` | FastAPI backend powering `/chat*`, `/meds*`, `/caregiver*`, `/smart-actions/*`, `/voice/*`, and eGain `/actions/*` |
| `r2/care_page.html` | Animated Web Companion UI (`http://127.0.0.1:8100`) with SVG mascot states, Smart Action cards, and voice support |
| `egain/` | eGain knowledge base (`10` docs), workflow configs, action specs, and hackathon submission package |
| `tests/care/` | **161 automated tests** covering medication store, agent tool loop, Smart Actions, floating desktop companion, telephony, UI, and live multi-turn Groq API conversations |

---

## 3. Quickstart

### 1. Configure `.env`
```env
GROQ_API_KEY=gsk_your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b
SMARTPOLI_API_KEY=smartpoli-demo-key
```

### 2. Launch the Floating Desktop Companion
```powershell
.\.venv\Scripts\python.exe -m r2.cli companion
```

### 3. Launch the Web Companion & eGain Action Server
```powershell
.\.venv\Scripts\python.exe -m r2.cli care
```
Open **http://127.0.0.1:8100** in your browser.

---

## 4. Automated Testing & Verification

Run the complete test suite (unit, integration, desktop companion, Smart Actions, Playwright UI, and live multi-turn Groq API tests):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/care tests/test_support.py -q -p no:cacheprovider
```

- **161 passed, 0 failed**

---

## License

MIT License — Copyright (c) 2026 Garv Chopra.
