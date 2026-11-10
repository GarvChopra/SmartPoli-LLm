# eGain Platform Configuration Guide

> **Note:** Follow the official eGain Hands-on Lab and eGain Workflow Library UI labels. Below is the exact configuration specification matching `egain/config/workflows.json` and `egain/config/actions.json`.

## 1. Knowledge & Intents (`01 | Knowledge-based AI Agent`)
1. Log in to your eGain trial/hackathon account.
2. Create an AI Agent named **SmartPoli Care Navigator** (Language: **English**).
3. Upload the 10 markdown files from `egain/knowledge/`.
4. Review generated intents and ensure two core intents are active and mapped:
   - **Missing reminders** → connected to Process Workflow `wf_reminder_troubleshoot`
     - Positive utterances: *"I'm not getting reminders"*, *"my reminders are late"*, *"the app stopped notifying me"*, *"no medication alerts"*
   - **Talk to a person** → connected to Escalation Workflow `wf_escalation`
     - Positive utterances: *"I want to talk to a human"*, *"connect me with an agent"*, *"I need to speak to someone"*, *"call me back"*
5. Paste the Global Rules from `egain/config/workflows.json` into the agent's system/custom instructions:
   - Run emergency check first; direct urgent symptoms to 911 / Poison Control (`1-800-222-1222`).
   - Never diagnose, recommend, or change medications or doses.
   - Answer strictly from the knowledge base; abstain and offer escalation when unsupported.
   - Label action outputs as simulated.

## 2. Pre-Chat Workflow (`03 | Pre-chat workflow`)
Configure before visitor enters chat:
- `first_name` (Text, optional, max 40 chars)
- `category` (Dropdown, required): `reminder_issue`, `missed_dose_logging`, `account_help`, `other`
- Route `reminder_issue` directly into the Reminder Troubleshooting workflow.

## 3. Process Workflow (`05 | Process workflow`)
Create `wf_reminder_troubleshoot` and link it to the **Missing reminders** intent:
1. Ask Yes/No: *"Are notifications allowed for SmartPoli in your phone settings?"* (If No → advise enabling → ask if resolved).
2. Ask Yes/No: *"Is Do Not Disturb off, or SmartPoli allowed through it?"* (If No → advise allowing → ask if resolved).
3. Ask Yes/No: *"Does your phone time zone match your SmartPoli account?"* (If No → advise matching → ask if resolved).
4. If all 3 are Yes (or issue remains unresolved) → execute Action `create_ticket` (`category="reminder_issue"`) and display the returned reference number (`SP-xxxx`) along with the reminder not to skip or change doses without consulting a pharmacist.

## 4. Escalation Workflow (`04 | Escalation workflow`)
Create `wf_escalation` with two contact options:
- **Option 1: Request a Demonstration Callback** → collect `window` (`morning`/`afternoon`/`evening`) → call Action `request_callback` (uses demo line `555-0100` by default) → return reference `CB-xxxx`.
- **Option 2: Email Support** → display `help@smartpoli.example` and hours (`Mon-Fri 8 AM-6 PM Pacific`).
- **Fallback on Action Error:** Display `help@smartpoli.example` and `555-0100` (demo).

## 5. Actions (`06 | Actions`)
1. Start the local action server:
   ```powershell
   $env:SMARTPOLI_API_KEY="your-secret-key"
   python -m r2.cli care
   ```
2. Start a public HTTPS tunnel:
   ```powershell
   ngrok http 8100
   ```
3. In eGain Actions, register:
   - `POST <ngrok-url>/actions/create_ticket` with header `X-API-Key: your-secret-key`
   - `POST <ngrok-url>/actions/request_callback` with header `X-API-Key: your-secret-key`

## 6. Personalization, Publishing & Validation (`02` & `07`)
1. Apply clean healthcare styling (white background, teal/green accent `#0d9488`, clear disclaimer banner).
2. Test all paths in **Test AI Agent**.
3. After explicit user approval, click **Publish**, wait up to 5 minutes for propagation, and retest via the shareable demo link.
