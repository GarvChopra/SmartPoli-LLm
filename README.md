# R2: a local assistant for your PC

R2 runs on your Windows PC and does things for you: opens apps, finds and reads files, fills in web
forms, searches the web, and remembers what you told it. You talk to it from a chat page on your PC,
from your phone, from a small floating window, or (optionally) by voice.

It is built to be **safe by construction**: reading and typing happen on their own, but anything that
posts, sends, deletes, pays, or runs commands stops and asks you first.

## eGain Hackathon: Penny support actions backend

For the eGain AI Agent hackathon, R2 includes a small backend that gives an eGain agent ("Penny", a fictional
credit union's card-support bot) real actions. eGain hosts the chat, knowledge, workflows and escalation; its
Actions call this service. All data is fictional demo data.

```
r2 support          # http://localhost:8090 (set SUPPORT_PORT / SUPPORT_API_KEY if needed)
```

| Endpoint | What it does |
|---|---|
| `POST /actions/freeze_card` | freezes a demo card (`last4`, `reason`) |
| `POST /actions/order_replacement` | orders a replacement, standard or expedited; card must be frozen first |
| `POST /actions/book_callback` | books a callback window |
| `POST /actions/open_dispute` | opens a dispute; over $500 returns `escalate: true` |
| `POST /actions/reset` | unfreezes demo cards between takes |

Only 4-digit card endings are accepted, never full numbers or PINs. Expose it to eGain with a tunnel such as
`cloudflared tunnel --url http://localhost:8090`. The full build plan, description and video script are in
[docs/egain-hackathon-kit.md](docs/egain-hackathon-kit.md).

## Nebius x NVIDIA Global AI Hackathon: Personal AI Track

R2 is an always-on personal assistant whose pitch is **a personal AI you can audit**. Most agents ask you to
trust that your data is handled well. R2 shows you: what was sent to a hosted model, what was masked first,
which model answered and why, and which tools it is allowed to use.

| Track requirement | How R2 meets it | Where |
|---|---|---|
| Always-on, private assistant | tray daemon + chat window + phone page; runs on your PC | `r2 daemon` |
| **NVIDIA open source model** | **Nemotron on Nebius Token Factory**, in three sizes (below) | `r2 cloud`, `r2/tiers.py` |
| Persistent memory | SQLite memory, plus `MEMORY.md` you can open, edit and import back | `r2 memory export` / `import` |
| Reusable skills | `skills/<name>/SKILL.md` ([agentskills.io](https://agentskills.io) layout); R2 *proposes* new ones, you approve | `r2 skills` |
| Tools you choose | per-tool on/off switch saved in `policy.json`; risky actions need your approval | `r2 tools` |
| Daily workflows | files, apps, browser, web search ([Tavily](https://tavily.com)), screen, **meeting summaries with tasks per teammate** | tools table below, `r2 meeting` |
| Secure | local PII masking, egress ledger, approval gates, OpenShell policy (see limits) | `r2 ledger` |
| Nebius services | Token Factory inference; container for Nebius Serverless Endpoints | `Dockerfile` |

### How Nemotron and Token Factory are used

R2 sends each request to the right-sized Nemotron model, chosen by plain code (never by a model):

| Request | Model class | Why |
|---|---|---|
| Plain conversation | Nemotron **Nano** (`R2_FAST_MODEL`) | fast and cheap for everyday calls |
| A task that uses tools | Nemotron **Super** (`R2_OPENAI_MODEL`) | reliable tool calling |
| Planning, analysis, long or many-step work | Nemotron **Ultra** (`R2_DEEP_MODEL`) | serious reasoning, used only when needed so credits stretch |
| A request containing a secret, with Ollama running | your **local** model | it never leaves the PC |

Token Factory is OpenAI-compatible, so R2 talks to it with plain HTTPS (`https://api.tokenfactory.nebius.com/v1`).
`r2 cloud` lists the models your key can really use and fills in the three tiers for you. If a hosted call
fails, a running local model finishes the job, so R2 keeps working without the network or credits.

"Private" is decided on the current turn only (your newest message and the tool results since). Older chat
history is still masked before it is sent, but it does not pin every later request to the local model. R2
checks for Ollama at request time, so starting it after R2 works without a restart. Both plain chat and tool
tasks follow these rules.

### The privacy gateway and ledger (what makes R2 different)

Before anything is sent to a hosted model, a local filter replaces emails, phone numbers, card numbers (Luhn
checked), Aadhaar and PAN numbers, OTPs, "password is ..." phrases and API keys with placeholders like
`[EMAIL_1]`. The model works on the placeholders; the real values are restored **locally** in the reply and
in tool arguments, so tools still work. Every request is appended to `data/egress.jsonl`: time, host, model,
size, what was masked, a preview of what was actually sent, and the routing decision with its reason. The
ledger never stores the original values.

```powershell
.venv\Scripts\r2 ledger
# 2026-10-11 16:02:11  route -> deep (nvidia/...ultra...): request asks for planning or analysis  [left this PC]
# 2026-10-11 16:02:12  api.tokenfactory.nebius.com  nvidia/...ultra...  4 msgs, 1830 B  [1 EMAIL]  HTTP 200
#     sent: plan my week and email [EMAIL_1] the summary
# Routing: 12 decisions, 3 stayed on this PC.
```

The phone/web page has the same ledger and a skills panel (approve or reject what R2 suggests).

### Meeting summarizer for your team

Say who is on the team once, then paste or point R2 at what was said in a meeting. R2 writes the minutes,
finds who was asked to do what and by when, and keeps it all in a project you can come back to.

```powershell
.venv\Scripts\r2 project create "Launch" --members "Aman Sharma=Aman|Ams, Riya, Karan"
.venv\Scripts\r2 meeting add Launch standup.txt --date 2026-10-12 --me Garv     # .txt .md .vtt .srt, or audio
.venv\Scripts\r2 tasks Launch                                                  # open tasks, grouped by due date
.venv\Scripts\r2 tasks Launch done 3                                           # tick one off
.venv\Scripts\r2 tasks Launch assign 5 --owner Karan                           # fix a wrong owner
.venv\Scripts\r2 meeting show 1                                                # the full minutes as markdown
```

You can also do it from the web page (Meetings panel: create a project, paste the transcript, tick tasks off,
change an owner) or just ask R2: "summarize C:\meetings\standup.txt for the Launch project".

What you get for each meeting: a summary, decisions, open questions, and **action items with an owner, a
deadline and the exact words they came from**. Everything is saved in `data/projects.db` and written as
readable markdown to `data/projects/<project>/` (one file per meeting, plus a live `TASKS.md` per person).

How it works, and why you can trust the result:

* **Two extractors.** Offline rules always run (no key, no internet): they read "Aman, please finish X by
  Friday", "Riya will handle Y", "I'll do Z tomorrow", "assign this to Karan", and Hinglish such as "Rahul
  tum backend kal tak kar do" or "main deck bhej dunga parso tak". Deadlines understand `Friday`, `tomorrow`,
  `kal`, `parso`, `shukravar`, `next week`, `agle hafte`, `in 3 days`, `20 October`, `20/10`. When a hosted
  model is configured (Nemotron on Token Factory) it writes the summary and catches what the rules miss.
  Planning words in the request route it to the biggest tier.
* **Checked, not trusted.** Model owners must be people on your team, and every item must quote words that
  really appear in the transcript. Anything that does not is saved as `Unassigned` or marked **unverified**,
  never silently accepted.
* **Private.** The transcript goes through the same privacy gateway as everything else (emails, phone and
  card numbers are masked before a hosted model sees them), and the ledger records it.
* **Fixable.** Wrong owner? Reassign it. Missed one? Add it by hand later. Done? Tick it, and `TASKS.md` updates.

Audio recordings are transcribed on this PC with faster-whisper (`pip install -e ".[voice]"`); nothing is
uploaded. For Google Meet, turn on captions or "Transcripts" and save the text, or paste it.

### Try it

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\r2 cloud          # choose "Nebius Token Factory", paste your key (hidden)
.venv\Scripts\r2 ask "Plan my week around three deadlines: Mon report, Wed demo, Fri review"
.venv\Scripts\r2 ledger         # see what was sent, what was masked, which model answered
.venv\Scripts\r2 skills         # the bundled skills
.venv\Scripts\r2 tools          # switch tools on or off
.venv\Scripts\r2 memory export  # open data\memory\MEMORY.md in Notepad
```

Optional: set `TAVILY_API_KEY` in `.env` and `web_search` uses Tavily (the query is masked first and
logged in the ledger); without it, or if Tavily fails, it falls back to free DuckDuckGo search.

### Public demo build (locked down)

`Dockerfile` builds the **demo profile** (`R2_PROFILE=demo`): only memory, skills and web search exist.
There are no file, shell, app, screen or browser tools in that build, so there is nothing on the host to
reach. It uses a fixed access code (`R2_DEMO_CODE`) instead of one-time pairing, and it is rate limited
(`R2_DEMO_TASKS_PER_HOUR`, default 30). Secrets are platform environment variables, never in the image.
The demo also blocks the screen-capture and "revoke all devices" endpoints, caps stored visitor tokens, and
shows a banner: **memory, projects and tasks in the demo are shared by every visitor and reset on restart**, so don't enter
personal information there.

```bash
docker build -t r2-demo .
docker run -p 8080:8080 -e R2_OPENAI_API_KEY=... -e R2_OPENAI_MODEL=nvidia/... \
  -e R2_FAST_MODEL=... -e R2_DEEP_MODEL=... -e R2_DEMO_CODE=choose-a-code r2-demo
```

Open `http://localhost:8080`, enter the access code, and chat. The same image deploys to Nebius Serverless
Endpoints. `sandbox/openshell-policy.yaml` is a default-deny [OpenShell](https://github.com/NVIDIA/OpenShell)
policy that allows only Token Factory and Tavily.

### What was built during the submission period

R2 was started on 2026-10-10, inside the submission period (Aug 26 - Oct 30, 2026), so it is new work. The
base assistant (agent loop, safety gate, tools, memory, phone page, tray daemon) came first. The
hackathon-specific work added on top, each with automated tests, is: the Nebius Token Factory provider and
Nemotron tiers (`r2/cloud_setup.py`, `r2/tiers.py`), the privacy gateway and egress ledger
(`r2/privacy.py`), skills (`r2/skills.py`, `skills/`), readable memory (`r2/memory_files.py`), the tool
on/off policy (`r2/policy.py`), the meeting summarizer with projects and per-person tasks (`r2/meetings.py`), the Tavily backend, the ledger and skills panels, the locked-down demo
profile, and the container and OpenShell files. The suite has 727 tests; the Token Factory path is tested
end to end against a local stand-in for the API (`tests/test_e2e_token_factory_mock.py`).

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
# no .env needed to start: every setting has a default. `r2 cloud` writes one for you, or create .env yourself
# (README sections below list every variable, e.g. R2_BRAIN, R2_OPENAI_API_KEY, R2_FAST_MODEL, TAVILY_API_KEY)

# a local model (once)
#   install Ollama from https://ollama.com/download, then:
ollama pull qwen2.5:3b            # does the work (tools)
ollama pull qwen2.5:1.5b          # answers chat instantly
setx OLLAMA_MAX_LOADED_MODELS 2   # keep both models in memory (then restart Ollama)

.venv\Scripts\r2 doctor           # checks the setup and says what is slow
.venv\Scripts\r2 daemon           # tray icon + the desktop chat window (Ctrl+Alt+R shows/hides it)
.venv\Scripts\r2 pair             # link + QR code to sign a phone or browser in
.venv\Scripts\r2 ask "List the files in my Downloads folder"   # one-off from the terminal
```

Run at every login (no admin needed), and undo it any time:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1
```

## The desktop window and speed

`r2 daemon` shows a small always-on-top chat window. **Ctrl+Alt+R** shows or hides it from anywhere,
**Esc** hides it, and the tray icon reopens it. Replies appear word by word, **Stop** cuts the model off
immediately, and risky actions show an Approve/Deny card right in the window.

**The floating shield.** R2 floats on your desktop as a small graphite-and-silver shield with a glowing blue
"face" (from the design sheet in `image.png`). Click it to open or close the chat, drag it anywhere. Its
face shows what R2 is doing: **idle**, **focused** (chat open), **curious** (you are typing), **thinking**
(reading your request), **working** (answering), **alert** (waiting for your approval), **sleeping** (paused
or R2 not running), **happy** (an action just got done), **supportive** (a friendly chat reply) and
**confused** (something failed).

**"Open <app>" is instant.** A plain "open whatsapp" / "launch chrome" skips the AI entirely and looks the
app up in your Start menu (so Microsoft Store apps like WhatsApp work too), taking about 0.1 s. If there is
no such app R2 says so instead of pretending, then lets the model try something else (for example a website).

How it stays quick on an ordinary laptop (no GPU needed):

* **Two models.** Plain conversation goes to a small fast model (`qwen2.5:1.5b`) with a tiny prompt and no
  tools. Real tasks go to the bigger `qwen2.5:3b` with only the tools that matter for that request. If the
  small model decides a message needs the PC it says so and the big one takes over.
* **Short prompts.** On a CPU most of the wait is the model *reading* the prompt, so the history and tool
  descriptions sent are kept short.
* **Warm start.** The daemon loads both models when it starts, so the first message is not slow.
* **CPU-only models (`R2_GPU_LAYERS=0`, the default).** On a small graphics card (this was built on a 2 GB
  one) Ollama puts a few layers on the GPU, and loading the second model then makes it *evict* the first to
  rebalance video memory, so every switch between chat and a task cost ~10 s. Pinning both models to the CPU
  stops that for almost no speed loss. With a big GPU set `R2_GPU_LAYERS=auto`.
* **Tools count for the model.** Folder listings start with exact item counts, because small models miscount.
* Measure it on your PC with `r2 bench`; `r2 doctor` points out what is slowing it down.

Typical numbers on the 16 GB / no-GPU-to-speak-of laptop it was built on: chat first word in about
0.5-1 s and a full reply in 1-5 s; a task that needs tools takes roughly 20-40 s. Smaller or faster
hardware will change this. In a small test on this PC the 3B model was both faster (21 s vs 66 s) and
more accurate than 7B at counting files, but 7B can be better at harder multi-step tasks: try
`R2_MODEL=qwen2.5:7b`. Use `R2_CHAT_MODEL=qwen2.5:3b` for smarter (but slower) chat.

## Connect a cloud model (optional: faster and more accurate)

On a laptop without a strong graphics card the local models are the bottleneck: a task takes about 30 s and
the small model sometimes gets things wrong. A hosted model answers in well under a second and is much more
accurate. R2 can use any OpenAI-compatible service, including free tiers from
[Groq](https://console.groq.com/keys), [Google Gemini](https://aistudio.google.com/apikey) and
[OpenRouter](https://openrouter.ai/keys).

```powershell
.venv\Scripts\r2 cloud      # guided: pick a provider, paste your key (hidden), pick a model
.venv\Scripts\r2 doctor     # confirms the key works
.venv\Scripts\r2 local      # go back to fully local any time
```

`r2 cloud` asks the provider which models your key can really use (so there are no stale model names),
saves the settings to `.env`, and never prints the key. You can choose the cloud for **everything**
(fastest) or **tasks only**, keeping plain chat on this PC.

Be clear about the trade-off: with a cloud model, whatever R2 sends to the model goes to that provider: your
messages, and any text it reads from files, web pages or the screen during a task. The chat window shows a
small **cloud** or **local** badge so you can always see which is active. Free tiers have rate limits (check
your provider's account page); R2 says so plainly when you hit one. The safety rules (approvals, blocked
actions) are enforced by R2's own code and do not depend on which model is used.

## Learning your habits

Opt-in and local. R2 always remembers how you phrased past requests and what it did for them, and uses
the closest ones as hints for new requests (this is retrieval, not retraining the model). If you also set
`R2_LEARN=1` it notes which program is in front once a minute, only the program name such as `chrome.exe`,
never window titles, keystrokes or screen contents, and learns which apps you usually use at which hour.
`r2 forget` erases history, the action log and learned habits (saved facts are kept; delete those from the
phone page).

## Using it from your phone

R2 only listens on `localhost`. It refuses to bind anything else, because it controls your PC.
To reach it from your phone, use [Tailscale](https://tailscale.com) (a private network between your own devices):

1. Install Tailscale on the PC and the phone and sign in to the same account.
2. On the PC: `tailscale serve --bg 8765`. This gives you an `https://<pc>.<tailnet>.ts.net` address.
3. Put that address in `.env` as `R2_PUBLIC_URL=...`, then run `r2 pair` and scan the QR code with the phone.

The phone page has chat, approve/deny buttons, a Stop button, a live view of the screen, and the list of
things R2 remembers (with a Forget button). "Revoke all devices" signs every phone out at once.

## What R2 can do

| Area | Tools |
|---|---|
| Files | list, read, find, write, delete (single files) |
| Apps and shell | open an app by name, run a Windows command |
| Screen | screenshot, list windows, list the buttons and fields of the active window |
| Keyboard and mouse | type, scroll, press keys, click at coordinates |
| Browser | open a page, read it, fill fields by label, click buttons by text (your installed Chrome or Edge, own profile) |
| Web | search (Tavily if you add a key, else DuckDuckGo), fetch a public page |
| Memory and skills | remembers facts across sessions, reads and proposes skills; every action is logged |

## How the safety model works

Every action has a risk level decided **in code, never by the model**:

| Level | Examples | What happens |
|---|---|---|
| Safe | read a file, type text, scroll, search, open a normal app, fill a field | runs immediately |
| Needs approval | click a "Submit/Post/Send/Pay/Delete" button, press Enter, run a shell command, write or delete a file, click at coordinates, read a secret-looking file, very long URLs | pauses until you approve on the phone, the overlay or the page. Unanswered requests are denied after 2 minutes |
| Never | formatting disks, deleting system folders, editing the boot config, switching off Defender or the firewall | refused |

You can pre-approve one kind of action for one task by saying so: "fill the form and **submit**" lets the
submit button through for that task only. "Don't submit" cancels it. Words that appear in web pages or
files can never grant anything. "Post" and "send" only count in verb form ("...and post it"), and delete,
payments and password fields are never pre-approved.

Voice can start and stop tasks, but it can **never approve** an action: a spoken "yes" could come from a
video or someone nearby.

## Optional extras

* **Cloud brain:** set `R2_BRAIN=cloud` and `ANTHROPIC_API_KEY` in `.env` for harder tasks. Only the
  conversation and tool results go to the cloud; memory stays on the PC.
* **Desktop window:** on by default (`R2_OVERLAY=0` turns it off).
* **Voice:** `pip install -e ".[voice]"`, then `R2_VOICE=1`. Use tray "Listen now" to talk. For a
  hands-free wake word you must supply an openWakeWord model file (`R2_WAKE_MODEL=...`), such as one you
  train for "Hey R2". Pre-built models cover other phrases only.

## Honest limits

Read this before trusting it with anything important.

* **Hackathon additions, status:**
  * *Tested (727 tests):* masking and restoring, the ledger, tier routing and fallback, skills (including
    propose/approve), memory import/export, the tool policy, the meeting summarizer (transcript formats, English and Hinglish owners and deadlines, model-output validation, projects, tasks, CLI, web panel in a real Chrome), the Tavily path (against a mock), the demo
    profile (fixed code, rate limit, localhost guard), and the whole Token Factory path end to end against a
    local stand-in for the API.
  * *Not yet run against the live service:* Nebius Token Factory and Tavily were exercised only against
    mocks in the tests. The exact Nemotron model ids come from the provider's model list at `r2 cloud` time
    (third-party catalogs spell them inconsistently), and `r2 cloud` picks tiers by name hints (`nano`,
    `ultra`), so check the "Model tiers" line it prints.
  * *Prototype:* `sandbox/openshell-policy.yaml` follows OpenShell's documented policy schema but has not
    been applied on a live OpenShell gateway, and `scripts/run-in-openshell.sh` is an untested sketch (the
    default OpenShell image has no Python or R2). OpenShell needs Linux, macOS or Windows WSL 2
    (experimental), so it does not sandbox the Windows desktop tools; those rely on R2's approval gates.
  * *Masking is pattern based.* It catches common formats (see `r2/privacy.py`), not every secret: a name,
    an address, or a password written in prose is not masked. It is a safety net, not a guarantee, and it
    cannot un-send text you paste into a request on purpose.
  * *Routing is keyword based.* "Plan", "analyze", "compare" and similar words, a long request, or six
    tool steps escalate to the biggest model. A hard request phrased without those words stays on the main
    model. The ledger shows every decision so you can see when it guessed wrong.
  * *Meeting summarizer.* The offline rules cover common English and Hinglish phrasings, not every way people
    talk: indirect assignments ("it would be great if someone looked at the login bug"), pronouns without a
    name ("you take it"), and speakers without labels are the usual misses, and they show up as `Unassigned`
    or are missed. Dates like "next Friday" use a simple rule. It does not tell speakers apart by voice: names
    come from the transcript labels (or `--me`). Model-written minutes have been tested against a mock, not
    yet against live Nemotron. Always skim the task list after a meeting.
  * *Skills are instructions, not code.* They cannot widen what tools are allowed, but a badly written
    skill can still steer R2 badly; read what you approve.
* **Tested by automated tests (400+ before the hackathon work):** the safety gate, risk rules, memory, agent loop, server and auth,
  the phone page in a real Chrome (pairing, approving, stop, no script injection, fits a phone screen),
  the browser tools on a local test page, shell/app/key rules, cloud request building (against a mock),
  voice logic and overlay logic (with fakes), and the PowerShell scripts parse.
* **Run once for real:** the tray daemon (server, overlay launch, token, graceful voice fallback).
* **Never run here:** Ollama (not installed on the machine used to build it), a live Anthropic call,
  Tailscale, the microphone / wake word / Whisper / text-to-speech, the Task Scheduler install, and
  controlling arbitrary desktop apps with the mouse. Expect rough edges there.
* **The small local model is the weak link.** With 2 GB of video memory only ~3B-parameter models fit.
  They can pick the wrong tool or loop (R2 stops after 2 errors in a row or 25 steps). Use the cloud
  brain for anything that matters.
* **It cannot see pictures.** "Seeing the screen" means reading window and page structure plus
  screenshots for *you*; a text model cannot read a screenshot.
* **Prompt injection is reduced, not solved.** A hostile web page can still try to steer the agent. The
  guards: web text is labelled as data, approvals can't be granted by page text, secret files and very
  long URLs need approval. The remember tool is auto-approved, so a page could try to plant a false
  "fact"; check the Memory list on the phone page now and then.
* **Your browser profile for R2 is separate** (`data/browser-profile`), but anything you sign in to
  there stays signed in, and R2 can act as you on those sites within the rules above.
* A paired phone can see your whole screen and send tasks. Treat pairing like handing over the keys;
  use "Revoke all devices" if a phone is lost.

## Project layout

```
r2/agent.py      the loop: decide -> check -> act -> repeat (step limit, Stop, 2-errors rule)
r2/gate.py       risk -> allow / ask / deny, and "submit"-style grants from your own words
r2/memory.py     SQLite: chat, facts, action log (secrets are redacted/refused)
r2/memory_files.py  MEMORY.md export / import
r2/brain.py      Ollama + cloud model behind one interface
r2/tiers.py      Nano / Super / Ultra / local routing and fallback
r2/privacy.py    masking gateway and the egress ledger
r2/meetings.py   meeting summarizer: transcript parsing, owners, deadlines, projects, tasks
r2/skills.py     SKILL.md loader, proposals and approval (skills/ holds the bundled ones)
r2/policy.py     per-tool on/off and the demo profile
r2/tools/        files, shell, apps, screen, inputs, web, browser (+ registry)
r2/runtime.py    approval broker, one-task-at-a-time manager
r2/auth.py       one-time pairing codes -> device tokens (stored as hashes)
r2/server.py     FastAPI + phone page (r2/web)
r2/daemon.py     tray icon, starts Ollama and the server
r2/voice.py      wake word -> speech-to-text -> task -> spoken answer
r2/overlay.py    floating window
docs/            hackathon submission pack and video script
```

Run the tests with `.venv\Scripts\python -m pytest`.

## Configuration reference (`.env`)

Create a file called `.env` next to `pyproject.toml` (it is git-ignored; never commit it). Every value is
optional. Real environment variables win over the file.

| Variable | Default | Meaning |
|---|---|---|
| `R2_BRAIN` | `ollama` | `ollama` (local), `openai` (any OpenAI-compatible host, including Nebius Token Factory), or `cloud` (Anthropic) |
| `R2_MODEL` / `R2_CHAT_MODEL` | `qwen2.5:3b` / `qwen2.5:1.5b` | local tool model / local chat model (also the private and offline fallback when hosted) |
| `R2_OPENAI_BASE_URL` | | e.g. `https://api.tokenfactory.nebius.com/v1` |
| `R2_OPENAI_API_KEY` | | your key; stays in `.env`, never printed or logged |
| `R2_OPENAI_MODEL` | | main hosted model for tasks (Nemotron Super class) |
| `R2_FAST_MODEL` | | hosted model for plain chat (Nemotron Nano class); empty = off |
| `R2_DEEP_MODEL` | | hosted model for planning, analysis, long tasks (Nemotron Ultra class); empty = off |
| `R2_PRIVATE_LOCAL` | `1` | requests containing secrets stay on this PC while Ollama is running |
| `TAVILY_API_KEY` | | use Tavily for web search (falls back to DuckDuckGo) |
| `R2_PROFILE` | | `demo` = locked-down hosted demo (the Dockerfile sets this; do not set it on your own PC) |
| `R2_DEMO_CODE` / `R2_DEMO_TASKS_PER_HOUR` | | demo access code / rate limit (default 30) |
| `R2_HOST` / `R2_PORT` | `127.0.0.1` / `8765` | R2 only listens on localhost unless the demo profile is set |
| `R2_DATA_DIR` | `data` | memory, ledger, skills, tokens, logs |
| `R2_OLLAMA_URL` | `http://localhost:11434` | local model server |
| `R2_MAX_STEPS` | `25` | step limit per task |
| `R2_LEARN` | `0` | learn which apps you use at which hour (program names only) |

`r2 cloud` writes the hosted settings for you and picks the three Nemotron tiers from the provider's model list.

## Licence and credits

R2 is released under the [MIT License](LICENSE).

* Skills follow the open [agentskills.io](https://agentskills.io) `SKILL.md` layout; the loader here is our own code.
* The sandbox policy is written against NVIDIA OpenShell's documented schema (OpenShell is Apache-2.0).
* Hosted models (NVIDIA Nemotron on Nebius Token Factory) and Tavily are used under their own terms; check each
  model card and service agreement before redistributing outputs commercially.
* Python libraries: httpx, FastAPI, uvicorn, Playwright and others, each under its own permissive licence.
