# SmartPoli Care Navigator

**eGain AI Agent Hackathon Submission**  
**Author:** Garv Chopra (`@GarvChopra`)  
**Industry:** Healthcare Services (Medication-Reminder Support)  
**Use Case:** Content-grounded patient support, adaptive 3-step reminder troubleshooting, automated support-ticket actions, emergency safety interception, and human escalation.

---

## 1. Overview & Problem Statement

Medication reminders only help patients when notifications actually arrive on time and users know how to handle missed doses safely. In practice, patients frequently stop receiving reminders due to phone notification permissions, Do Not Disturb settings, or time-zone mismatches—leading to long support calls, repeated explanations, or unsafe guesswork around missed doses.

**SmartPoli Care Navigator** is an AI customer-service agent designed for the **eGain AI Agent Hackathon**. It combines eGain's knowledge-grounded AI agent and guided workflows with a real-time conversational UI and sandbox action backend (`r2 care`, powered by the **Groq API** and **FastAPI**):

1. **Knowledge-Grounded Answers (`01 | Knowledge-based AI Agent`):** Answers strictly from 10 synthetic SmartPoli knowledge documents (`egain/knowledge/`) and explicitly abstains when a question is outside its authorized scope (such as pricing, smartwatch apps, or pharmacy networks).
2. **Pre-Chat Context Collection (`03 | Pre-chat workflow`):** Captures minimal, privacy-safe visitor context (`first_name` optional, `category` required) without ever asking for diagnoses, dates of birth, passwords, or personal phone numbers.
3. **Adaptive Process Workflow (`05 | Process workflow`):** Guides visitors who aren't receiving reminders through 3 device checks (*Phone notifications allowed* → *Do Not Disturb off/allowed* → *Time zone match*), adapting naturally if the user states what they already checked or asks side questions mid-workflow.
4. **Meaningful Automated Action (`06 | Actions`):** When all 3 checks pass and reminders are still missing (or upon request), automatically executes `POST /actions/create_ticket` to generate an idempotent, PII-scrubbed support ticket (`SP-1001`) so the patient never has to repeat themselves.
5. **Two Human Escalation Paths (`04 | Escalation workflow`):**
   - **Option 1:** Book a demonstration callback (`POST /actions/request_callback`, returning reference `CB-xxxx` for morning, afternoon, or evening).
   - **Option 2:** Direct support email (`help@smartpoli.example`, Mon–Fri 8 AM–6 PM Pacific).
6. **Clinical & Emergency Safety by Construction:** Immediately intercepts urgent symptom keywords (`chest pain`, `can't breathe`, `overdose`, `seizure`, `stroke`, `suicidal`, etc.), halts normal workflows, and directs the visitor to `911` and Poison Control (`1-800-222-1222`). Never diagnoses conditions or advises whether to take a missed dose.

---

## 2. Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│ eGain AI Agent Platform + SmartPoli Care Navigator Web UI (`GET /`)       │
│  • Pre-Chat Workflow (`first_name`, `category`)                           │
│  • Knowledge Hub (10 docs in `egain/knowledge/*.md`)                      │
│  • Process Workflow: 3-Step Reminder Troubleshooting (`Missing reminders`)│
│  • Escalation Workflow: Callback (`CB-xxxx`) + Email (`Talk to a person`) │
└─────────────────────────────────────┬─────────────────────────────────────┘
                                      │ HTTPS / JSON (`X-API-Key`)
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ SmartPoli Care Navigator Service (`python -m r2.cli care` on port 8100)   │
│  • Groq API Multi-Turn Conversational Engine (`r2/care_actions.py`)       │
│    - Session memory, top-K knowledge retrieval, automatic model failover  │
│  • Action Endpoints for eGain:                                            │
│    - `POST /actions/create_ticket`    (idempotent, PII-scrubbed `SP-xxxx`)│
│    - `POST /actions/request_callback` (books demo callback `CB-xxxx`)     │
│    - `POST /safety/check`             (emergency & injection guardrails)  │
│    - `GET  /health`                   (liveness & AI connection status)   │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Repository Structure

| Path | Purpose |
|---|---|
| `egain/knowledge/*.md` | 10 original, synthetic SmartPoli knowledge documents for eGain upload and local retrieval |
| `egain/config/workflows.json` | Complete specification of Pre-Chat, Intents, 3-Step Reminder Troubleshooting, Escalation, and Emergency workflows |
| `egain/config/actions.json` | Action endpoint contracts (`create_ticket`, `request_callback`, `safety_check`) |
| `egain/docs/` | Architecture, eGain step-by-step configuration guide, knowledge traceability matrix, and problem statement |
| `egain/submission/submission-package.md` | 115-word hackathon description, 60-second demo video script & storyboard, and submission checklist |
| `r2/care_actions.py` | FastAPI backend powering the Groq multi-turn conversational agent and eGain HTTP Actions |
| `r2/care_page.html` | Production-polished responsive dark/light web UI (`http://127.0.0.1:8100`) |
| `tests/care/` | 60 automated tests covering knowledge grounding (`13/13`), abstention (`7/7`), workflows, actions, UI (Playwright desktop & mobile), and 5 live multi-turn Groq conversations |

*(Note on Pre-Existing Code Disclosure: This repository builds on the author's R2 Python runtime and design system, isolating the SmartPoli Care Navigator hackathon implementation under `egain/`, `r2/care_actions.py`, `r2/care_page.html`, and `tests/care/`.)*

---

## 4. Quickstart (Run Locally in 60 Seconds)

### Prerequisites
- Python 3.12+
- A Groq API key (`https://console.groq.com/keys`)

### 1. Install dependencies
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 2. Configure `.env`
Copy `.env.example` to `.env` and set your keys:
```env
GROQ_API_KEY=gsk_your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b
SMARTPOLI_API_KEY=smartpoli-demo-key
```

### 3. Start SmartPoli Care Navigator
```powershell
.\.venv\Scripts\python.exe -m r2.cli care
```
Open **http://127.0.0.1:8100** in your browser.

### 4. Expose Action Endpoints for eGain Cloud (Optional Tunnel)
```powershell
ngrok http 8100
```
Follow [`egain/docs/egain-configuration.md`](egain/docs/egain-configuration.md) to connect the `ngrok` URL to eGain Actions.

---

## 5. Action API Reference

All `POST` action endpoints require the `X-API-Key: <SMARTPOLI_API_KEY>` header.

| Endpoint | Method | Request Body | Response |
|---|---|---|---|
| `/health` | `GET` | — | `{"ok": true, "simulated": true, "provider": "groq", "ai_connected": true}` |
| `/actions/create_ticket` | `POST` | `{"category": "reminder_issue", "summary": "..."}` | `{"ok": true, "reference": "SP-1001", "handoff_summary": "...", "message": "..."}` |
| `/actions/request_callback` | `POST` | `{"window": "morning"}` | `{"ok": true, "reference": "CB-1002", "message": "..."}` |
| `/safety/check` | `POST` | `{"text": "..."}` | `{"ok": true, "emergency": false, "prompt_injection": false, "message": ""}` |

---

## 6. Automated Testing & Verification

Run the complete SmartPoli test suite (knowledge grounding, abstention, workflow graph verification, action security, headless browser desktop/mobile layout, and 5 live multi-turn Groq conversations):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/care tests/test_support.py -q -s
```

- **60 passed, 0 failed**
- **Grounded Answer Accuracy:** `13/13` (`100%` on `tests/care/fixtures/qa.json`)
- **Unsupported Question Abstention:** `7/7` (`100%`)
- **Desktop (`1280×800`) & Mobile (`390×844`) Browser E2E:** Verified in headless Chromium with zero console or layout errors.

---

## 7. Hackathon Submission Summary (115 Words)

> SmartPoli Care Navigator helps patients who miss medication reminders, a common reason people fall behind on routines. Visitors choose their issue in a short pre-chat form. The agent answers questions only from SmartPoli's own content and says so when it doesn't know. For missing reminders it guides a three-check troubleshooting workflow, and if that fails it creates a support ticket and returns a reference number, so the patient never has to explain twice. It offers a callback or email, and it stops and points to emergency care for urgent symptoms. It never gives medical advice. This reduces repeat contacts and gives support staff a clear summary. All data is fictional and the actions are simulated.

---

## License

MIT License — Copyright (c) 2026 Garv Chopra.
