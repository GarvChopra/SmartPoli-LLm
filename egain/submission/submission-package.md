# Submission Package: SmartPoli Care Navigator

- **Agent Name:** SmartPoli Care Navigator
- **Industry:** Healthcare Services
- **Use Case:** Medication-reminder customer support (grounded Q&A, guided reminder troubleshooting, automated support ticket action, and safe human escalation)
- **Pre-existing Code Disclosure:** The sandbox action service (`r2 care` in `r2/care_actions.py`) runs inside the pre-existing R2 repository (`FastAPI`/`Uvicorn`) and is isolated under `egain/`, `r2/care_actions.py`, and `tests/care/`.

---

## 1. Submission Description (115 words — limit is 150 words)
SmartPoli Care Navigator helps patients who miss medication reminders, a common reason people fall behind on routines. Visitors choose their issue in a short pre-chat form. The agent answers questions only from SmartPoli's own content and says so when it doesn't know. For missing reminders it guides a three-check troubleshooting workflow, and if that fails it creates a support ticket and returns a reference number, so the patient never has to explain twice. It offers a callback or email, and it stops and points to emergency care for urgent symptoms. It never gives medical advice. This reduces repeat contacts and gives support staff a clear summary. All data is fictional and the actions are simulated.

---

## 2. 60-Second Demo Video Script & Storyboard

| Time | Screen / Visual | Action Taken | Voiceover |
|---|---|---|---|
| **0:00–0:08** | Open published eGain demo link showing clean healthcare theme and Pre-Chat form (`first_name`, `category`). | Enter `Sam`, select `reminder_issue`, click Start. | *"When patients miss medication reminders, calling support takes too long. Meet SmartPoli Care Navigator, built on eGain."* |
| **0:08–0:20** | Chat window opens. | Ask: `"How late can a reminder arrive?"` | *"First, grounded knowledge: it answers directly from our authorized docs—reminders can arrive up to five minutes late—and refuses to guess on unsupported questions."* |
| **0:20–0:40** | Trigger process workflow (`"I'm not getting reminders"`). | Walk through the 3 yes/no checks (notifications, Do Not Disturb, time zone), answering `Yes` to all three. | *"Next, our guided process workflow walks through three device checks step by step."* |
| **0:40–0:50** | eGain Action calls `POST /actions/create_ticket`. | Bot displays generated ticket reference `SP-1001` and safe pharmacist reminder. | *"When all checks pass, SmartPoli takes action—creating a privacy-scrubbed support ticket with reference SP-1001 so the patient never has to repeat themselves."* |
| **0:50–1:00** | Type `"I have chest pain"` (shows 911 stop), then `"I want to talk to a human"` (shows callback `CB-1002` + email `help@smartpoli.example`). | Show emergency stop and two human escalation options. | *"It stops immediately for emergencies, never gives medical advice, and offers callback or email escalation. Fast, safe support."* |

---

## 3. Final Verification & Submission Checklist

- [x] Simplified healthcare use case isolated inside R2 (`egain/`, `r2/care_actions.py`, `tests/care/`).
- [x] Existing R2 features preserved and regression-tested (`779 passed`).
- [x] 10 original, synthetic knowledge documents created (`egain/knowledge/`).
- [x] Pre-chat, reminder-troubleshooting process workflow, and 2-option escalation workflow specified (`egain/config/workflows.json`).
- [x] Meaningful action service (`r2 care`: `/actions/create_ticket`, `/actions/request_callback`, `/safety/check`) implemented and HTTP smoke-tested (`SP-1001`).
- [x] 55/55 automated tests passing for knowledge grounding (`13/13`), abstention (`7/7`), workflows, actions, PII scrubbing, duplicates, and emergency safety.
- [ ] **Requires your eGain account:** Upload `egain/knowledge/*.md` and configure workflows/actions in eGain following `egain/docs/egain-configuration.md`.
- [ ] **Requires your eGain account + approval:** Publish the eGain agent, wait 5 minutes, verify the live demo URL, and record the 60-second video.
