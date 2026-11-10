# R2 — Local PC Assistant: Design

Date: 2026-10-10
Status: draft, awaiting user review

## 1. Purpose

R2 is an always-on assistant that runs on the user's Windows PC. It starts at login with no manual step, can see the screen, operates the machine (files, apps, shell, mouse/keyboard, browser), can search and read the web, remembers past work, and can be driven from the PC or from a phone. Later versions add a wake word ("Hey R2") with voice replies and a floating overlay.

The main feature is the **local machine handler**. Internet access is one tool among the others.

### Success criteria (v1)

1. After login, R2 is running without the user opening anything.
2. From the PC chat or the phone page, "fill this form" completes a real browser form, stopping to ask before posting/sending unless the user's message explicitly granted it.
3. R2 recalls facts and past tasks from earlier sessions.
4. The user can stop any running task from the phone.
5. The same agent works with a local model or a cloud model, chosen by config.

### Constraints

- Machine: Windows 11, 16 GB RAM, NVIDIA MX550 with 2 GB VRAM. Local models are limited to roughly 1–4B parameters; larger ones run on CPU and are slow.
- Hybrid brain: everything except the optional cloud model call runs on the PC. Memory never leaves the PC.
- Python for all of v1.

### Non-goals (v1)

Voice and wake word, floating overlay, embedding-based memory search, multi-user support, running on other operating systems.

## 2. Architecture

One Python service, split into units with one job each. Every input channel produces text for the same `agent`.

| Unit | Job | Depends on |
|---|---|---|
| `brain` | One interface over the model: local Ollama backend and optional cloud backend. Returns a tool call or a final answer. | config |
| `agent` | The loop: observe, recall, decide, check, act, repeat. | brain, tools, memory, gate |
| `tools/` | One module per capability: files, shell, apps, screen, mouse/keyboard, browser, web. Each is a typed function with a schema. | OS libraries |
| `gate` | Assigns a risk level to each action and enforces approval. | none |
| `memory` | SQLite: recent chat, long-term facts, action log. | none |
| `server` | FastAPI app and phone-friendly web page: chat, status, approvals, Stop. | agent |
| `daemon` | Auto-start, tray icon, crash restart. | server |
| `voice` (later) | Wake word, speech-to-text, text-to-speech. | agent |

Stack: FastAPI, Playwright (browser), `pywinauto` and `pyautogui` (desktop), Ollama (local model), SQLite, Tailscale (phone link).

## 3. Agent loop

For each task:

1. **Observe.** Screenshot plus structured UI data: the page elements through Playwright, or the Windows accessibility tree. Screenshots alone are the fallback.
2. **Recall.** Relevant long-term facts and the last ~20 chat turns.
3. **Decide.** The brain returns exactly one typed tool call.
4. **Check.** The gate evaluates the action.
5. **Act.** Run the tool and write the action and result to the log.
6. **Repeat** until the brain returns "done", the step limit is reached (default 25), or the user presses Stop.

Failure handling: on a tool error, or when the screen does not change after an action, retry once with a different approach, then stop and report what was seen. The loop never retries silently.

## 4. Safety gate

Risk levels come from a fixed table in code. The model cannot change them. Unknown tools default to Confirm.

| Level | Examples | Behaviour |
|---|---|---|
| Safe | read file, screenshot, web search, scroll, type into a form field | runs immediately |
| Confirm | post, send, submit, delete or move files, shell commands, installs, anything touching passwords or payments | pauses; shows the exact action; waits for approval on PC or phone |
| Blocked | formatting disks, editing system folders, disabling security tools | never runs |

### Explicit grants

- If the user's own message names an action type (for example "fill it and submit"), that action type is pre-approved **for that task only**.
- Matching is done in code on the user's literal words from an authenticated channel. The model cannot declare a grant.
- Text read from web pages, files, or tool output can never create a grant (prompt-injection defence).
- Grants do not apply to delete, payment or password actions, or to Blocked actions. Those always ask or never run.

### Approvals

Pending approvals expire after 2 minutes. The log records each action, its result, whether it was granted or approved, and when.

## 5. Memory

One SQLite file on the PC.

| Layer | Holds | Used for |
|---|---|---|
| Recent chat | last ~20 turns | conversation coherence |
| Long-term facts | user-stated and learned facts, each with timestamp and source | injected into the prompt when relevant |
| Action log | every action, result, approval | audit and "what did you do yesterday?" |

- Retrieval is keyword plus recency in v1.
- R2 tells the user when it saves a fact. The web page lists, edits and deletes facts.
- Passwords and card numbers are never stored, even if visible on screen.

## 6. Phone access

- The FastAPI server listens only on the PC. The phone connects over Tailscale, so the server is never exposed publicly. ngrok is the fallback.
- Pairing: scan a QR code containing a one-time token. The phone then holds a long-lived token the user can revoke.
- Phone page: chat, live status with the latest screenshot, approve/deny buttons, and a Stop button.

## 7. Auto-start

A Windows Task Scheduler entry runs the daemon at login. The daemon starts Ollama and the server, shows a tray icon (status, pause, quit), and restarts itself on crash.

## 8. Brain configuration

- Default: a small tool-calling model through Ollama (a ~3B model such as Qwen2.5 3B; the exact choice is confirmed by testing on this machine).
- Optional: a cloud model enabled by an API key in `.env` (never committed). Used by choice or for tasks the local model fails.
- The wake word and assistant name are config values (default "R2").

## 9. Error handling and testing

- Gate: unit tests for every risk-table row, grant matching, grant scope (per task, per action type), and a test that tool output and web text never create a grant.
- Agent: tests with a fake brain that returns scripted tool calls (step limit, retry-once, Stop).
- Memory: tests for save/recall/delete and for the never-store-secrets rule.
- Tools: smoke tests against a local test page for browser form filling.
- Manual: end-to-end run of the form-fill demo from the PC and from the phone.

## 10. Build order

1. `gate` and `memory` (pure logic, fully testable).
2. `brain` and `agent` with a fake brain, then the real Ollama backend.
3. `tools/`: files, shell, web, browser, then screen and mouse/keyboard.
4. `server` and phone page.
5. `daemon` and auto-start.
6. Later: `voice`, then the overlay.

## 11. Risks

- A 2 GB-VRAM machine limits local model quality; the cloud option exists for that reason.
- Vision-only clicking is unreliable on small models, so structured UI data comes first.
- An always-on PC-control agent is high-impact. The gate, action log and Stop button are part of v1, not later work.
