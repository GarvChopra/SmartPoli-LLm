# Devpost submission pack: R2, the personal AI you can audit

Copy the sections below into the Devpost form. Bracketed items `[like this]` are the ones only you can fill in.

## Track
**Personal AI Track.** Also eligible for the **Best Use of Tavily** bonus (a real runtime call to the Tavily API when `TAVILY_API_KEY` is set).

## Tagline
An always-on personal assistant that works on your PC with Nemotron, and shows you exactly what left your machine.

## What it does
R2 is an always-on assistant for Windows. It opens apps, finds and reads files, fills in web forms, searches the web and remembers what you tell it. You talk to it from a floating window, a chat page, your phone, or by voice. Anything risky (sending, deleting, paying, running commands) stops and asks you first.

For this hackathon it became a **personal AI you can audit**:

* **Nemotron on Nebius Token Factory, right-sized per request.** Plain chat goes to Nemotron Nano, tool tasks to Super, and planning, analysis or long tasks to Ultra. The routing is plain code, not a model, so it is predictable, and the biggest model is used only when it is worth it.
* **A privacy gateway.** Before any request leaves, a local filter masks emails, phone numbers, card numbers, Aadhaar and PAN numbers, OTPs, passwords and API keys with placeholders. The model works on the placeholders and the real values are restored locally in the reply, so tools still work.
* **An egress ledger.** Every request is logged: where it went, what was masked, a preview of exactly what was sent, which model answered and why, and whether it left the PC. `r2 ledger` and a panel on the web page show it.
* **Private stays local.** If a request contains a secret and a local model is running, it never leaves the PC. If a hosted call fails, the local model finishes the job.
* **Persistent memory you can read.** Facts live in SQLite and can be exported to a plain `MEMORY.md`, edited, and imported back.
* **Reusable skills.** `skills/<name>/SKILL.md` recipes (the agentskills.io layout). R2 reads one only when it fits a request, and can *propose* new skills after a task; nothing activates until you approve it.
* **Tools you choose.** Each tool can be switched off (`r2 tools disable run_shell`) and is then removed entirely; the model is never told it exists.
* **Tavily search** with the query masked first and logged in the ledger.
* **A meeting summarizer for your team.** Create a project with your three teammates, paste or point R2 at a transcript (Google Meet captions, `.vtt`, `.srt`, plain text, or an audio file transcribed on the PC), and get minutes, decisions, open questions and action items with an owner, a deadline and the exact quote. English and Hinglish ("Rahul tum backend kal tak kar do"). Offline rules always run; Nemotron improves the minutes when a key is set, and its answer is checked (owners must be on the team, quotes must appear in the transcript, otherwise the item is marked unverified). Tasks are tracked per person, saved to the project and exported as markdown.

## Why we built it
Every personal-agent pitch says "your data stays under your control", and none of them lets you check. We wanted the check to be a feature you can see in a demo: one screen that shows what was sent, what was hidden, and why a model was chosen. The target user is anyone who wants an assistant on their real machine, with real files and accounts, and is rightly nervous about handing that to a cloud model.

## How it works
`request -> router (code) -> gateway masks -> Nemotron on Token Factory -> restore locally -> tools (with approval gates) -> ledger`

* Python, FastAPI, SQLite; Windows desktop tools for files, apps, screen and browser.
* Risk levels are decided in code, never by the model. Voice can start tasks but never approve them.
* `Dockerfile` builds a locked-down demo profile (memory, skills, web search only) for Nebius Serverless Endpoints.
* 727 automated tests, including the whole Token Factory path against a local stand-in for the API.

## How we used NVIDIA Nemotron and Nebius
* **Nemotron Nano / Super / Ultra** through **Nebius Token Factory** (OpenAI-compatible API). Three tiers, so credits stretch: Ultra only for planning and analysis.
* **Nebius Serverless Endpoints:** the demo container. `[fill in after deploying: yes/no]`
* **NVIDIA OpenShell:** a default-deny network policy for the Linux demo build (`sandbox/openshell-policy.yaml`). Written against the documented schema; **not yet run on a live gateway**.
* **Tavily:** search backend.

## Built with
Python, FastAPI, SQLite, httpx, Nebius Token Factory, NVIDIA Nemotron, Tavily, NVIDIA OpenShell (policy), Docker, Ollama (optional local model).

## Testing instructions for judges
* **Hosted demo:** `[URL]` . Enter access code `[R2_DEMO_CODE]`. The demo has only memory, skills and web search (no PC-control tools exist in it). It is rate limited, and its memory is shared by all visitors (a banner says so), so please don't enter personal information. Try: "Remember that my standup is at 10", "Use your safe-research skill: what is NVIDIA Nemotron?", then open the **Privacy ledger** panel. In the **Meetings** panel, create a project (for example the team "Aman, Riya, Karan"), paste a transcript such as `Garv: Aman, please finish the login page by Friday. Riya will handle the API docs and Karan will do QA before 20 October.`, and press Summarize and save.
* **On your own machine:** see the README, "Nebius x NVIDIA Global AI Hackathon" section. `r2 cloud` -> choose Nebius Token Factory.

## If it existed before the Submission Period
It did not: the design spec and the first files are dated 2026-10-10, inside the submission period (Aug 26 - Oct 30, 2026). (Once the public repo exists, its own first commit date will show this too.) The README's "What was built during the submission period" section lists the pieces.

## Honest limits (also in the README)
Masking is pattern based and does not catch everything. The meeting summarizer misses indirect assignments and unnamed "you"s (they show as Unassigned or are missed), and its model path has been tested only against a mock. Routing is keyword based. The OpenShell policy and Tavily path are not yet verified live. OpenShell does not sandbox the Windows desktop tools. Skills are instructions, so read what you approve.

## Feedback for Nebius and NVIDIA (observations from building this; edit as you see fit)
* **Credits and billing:** the hackathon gave a code for Token Factory credits, but account activation asked for card details. Builders without a card (students) are blocked. A card-free path, or saying so up front in the credits instructions, would help.
* **Model ids:** third-party catalogs list the Nemotron 3 models with inconsistent capitalisation and naming (`nvidia/nemotron-3-super-120b-a12b` vs `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`). A short table of exact Token Factory ids per Nemotron tier in the hackathon resources would save time; we ended up listing models from the API at setup time instead.
* **OpenShell docs:** the page paths changed between versions (a `latest/reference/policy-schema` URL returned "Page Not Found" while `how-it-works/policies/schema.md` worked), and the policy `access` values and "run my own program in a sandbox" steps were hard to find. A worked example for running a custom Python service in a sandbox would help.
* **What worked well:** the OpenAI-compatible endpoint meant Token Factory dropped into an existing client with no code changes beyond the base URL. `[add your own live observations: latency, tool-calling quality per tier, rate limits]`

## What only you can do (checklist)
- [ ] Activate Token Factory credits; run `r2 cloud` and check the "Model tiers" line it prints.
- [ ] Create a **public** GitHub repo, push this folder (the MIT `LICENSE` is already here), and confirm GitHub shows the licence in the About box.
- [ ] Deploy the Docker image (Nebius Serverless Endpoint or any host); set the secrets; put the URL and access code above.
- [ ] Record the video (script: `docs/VIDEO_SCRIPT.md`), upload to YouTube as public, no copyrighted music.
- [ ] Fill the live-service notes in "Feedback" from what you actually see.
- [ ] Submit on Devpost before **Oct 30, 2026, 10:00 am PT (10:30 pm IST)**; pick the Personal AI track.
