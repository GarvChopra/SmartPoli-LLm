# R2 Plans 2–4 (condensed): tools, server/daemon, voice/overlay

> Executed inline right after Plan 1, test-first. This is a condensed task list rather than the
> full step-by-step form used in Plan 1, because the user asked for all phases to be completed in one run.
> Spec: `docs/superpowers/specs/2026-10-10-r2-local-assistant-design.md`

## Cross-cutting change: argument-dependent risk
`ToolSpec` gains optional `classify(args) -> RiskInfo`. The agent calls `spec.risk_for(args)`.
If `classify` raises, the risk is CONFIRM. This lets one tool be SAFE for a text field and CONFIRM for a
"Post" button, and lets `run_shell` return BLOCKED for destructive commands. The model still cannot
change any risk: classification is code.

## Plan 2: tools
| # | Unit | Risk behaviour |
|---|---|---|
| 2.1 | `risk_for` + agent wiring | — |
| 2.2 | files: `find_files` (SAFE), `write_file` (CONFIRM write), `delete_file` (CONFIRM delete, files only) | |
| 2.3 | `shell.run_shell` | BLOCKED for destructive patterns (format, diskpart, rmdir /s on drive roots, reg delete HKLM, bcdedit, disabling Defender); otherwise CONFIRM shell |
| 2.4 | `apps.open_app` | SAFE only for a plain app name (no path, no dangerous extension); otherwise CONFIRM |
| 2.5 | `screen`: `screenshot`, `list_windows`, `ui_elements` | SAFE |
| 2.6 | `input`: `type_text` (SAFE), `scroll` (SAFE), `click` (CONFIRM click), `press_key` (nav keys SAFE, Enter CONFIRM submit, anything else CONFIRM keys) | |
| 2.7 | `web`: `web_search` (DuckDuckGo HTML), `web_fetch` (http/https only, loopback and private addresses refused) | SAFE |
| 2.8 | `browser` (Playwright on a dedicated thread): `browser_open`, `browser_read`, `browser_fill`, `browser_click` | `browser_click` classifies the button text: submit/confirm → CONFIRM submit; post/publish/share → CONFIRM post; send → CONFIRM send; pay/buy/checkout → CONFIRM payment; delete/remove → CONFIRM delete; else SAFE. `browser_fill` refuses password fields. |
| 2.9 | `full_registry(memory)` | |

## Plan 3: brain choice, server, phone, daemon
| # | Unit |
|---|---|
| 3.1 | `.env` loader (no new dependency), `.env.example`, `CloudBrain` (Anthropic Messages API over httpx), `make_brain(settings)` |
| 3.2 | `ApprovalBroker` (blocks the agent thread, 120 s expiry → denied), `TaskManager` (one task at a time, Stop event, status) |
| 3.3 | `auth`: one-time pairing codes (10 min, single use), long-lived tokens stored as SHA-256 hashes, revoke |
| 3.4 | `server`: FastAPI app — pair, tasks, status, approve, stop, screenshot, facts, actions, phone page (static HTML). Binds 127.0.0.1; phone reaches it through `tailscale serve`. |
| 3.5 | `daemon`: tray icon (pystray), starts Ollama when needed, runs the server; `scripts/install-autostart.ps1` / `uninstall-autostart.ps1` (Task Scheduler at logon, restart on failure); `r2 pair` prints a link and QR code |

## Plan 4: voice and overlay
| # | Unit |
|---|---|
| 4.1 | `voice`: wake-word → record → speech-to-text → agent → text-to-speech pipeline with injectable parts; real parts (openWakeWord, faster-whisper, sounddevice, pyttsx3) are lazy optional imports; mute toggle |
| 4.2 | `overlay`: small frameless always-on-top tkinter window that talks to the local server (status, text input, approve/deny, stop); logic separated from the view for testing |

## Honest limits
Hardware-dependent parts (microphone, wake word, tray, overlay, a real Chrome/Edge session, a live
Anthropic call, Tailscale) can be tested here only through fakes or smoke runs. Each is reported as such.
