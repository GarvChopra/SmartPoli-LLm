# 3-minute demo video script

Rules to respect: under 3 minutes, public on YouTube, show the project working on the device it was built for,
no third-party trademarks or copyrighted music. Use your own voice or captions. Screen-record at 1080p.

Before recording: run `r2 cloud` (Nebius), have Ollama running, clear `data/egress.jsonl` so the ledger starts empty,
create the project once (`r2 project create "Launch" --members "Aman, Riya, Karan"`), and keep `r2 daemon` running
so the floating shield is visible. Keep this sample transcript ready to paste:

```
Garv: Theek hai team, launch ka plan. We decided to go with the dark theme.
Garv: Aman tum backend ka kaam kal tak kar do. Riya, please finish the landing page by Friday.
Garv: Karan will do the QA pass before 20 October and Riya will handle the API docs.
Garv: Main deck bhej dunga parso tak. Someone needs to update the privacy policy.
```

| Time | Show | Say (short) |
|---|---|---|
| 0:00-0:12 | The floating shield on the desktop, then the chat window | "This is R2, an always-on assistant on my real PC. Every personal AI says your data is safe. R2 lets you check." |
| 0:12-0:30 | Type: "Plan my week around three deadlines: Monday report, Wednesday demo, Friday review" | "A planning request. R2 routes it to Nemotron Ultra on Nebius Token Factory. Plain chat goes to Nano, tool tasks to Super, so the big model is used only when it's worth it." |
| 0:30-0:50 | Open the Privacy ledger panel (web page) or run `r2 ledger` | "Here is the routing decision and the reason, the host it went to, and exactly what was sent." |
| 0:50-1:20 | Type: "My phone is 98765 43210 and my card is 4111 1111 1111 1111, remind me to call the bank" | "Something private. The gateway masked the phone and card before sending. The model only saw placeholders. With Ollama running, a request like this stays on the PC entirely." Show the ledger line `[1 PHONE, 1 CARD]` and "stayed on this PC". |
| 1:20-1:40 | Skills panel: show a proposal with its instructions, press Approve | "R2 can propose a reusable skill, but it is inactive until I read it and approve it." |
| 1:40-2:25 | **Meetings panel: paste the sample transcript, press Summarize and save.** Show the summary, the decisions, then the tasks grouped by Aman, Riya, Karan. Tick one off, change one owner. Point at an "UNVERIFIED" or "Unassigned" item if there is one. | "I assigned work out loud in a meeting, in English and Hinglish. R2 turned it into minutes and a task list per teammate, with deadlines and the exact words they came from. Anything it can't verify is flagged, not guessed. It's saved to the project and exported as markdown." |
| 2:25-2:40 | `r2 tools disable run_shell`, then ask R2 to run a command | "I choose the tools. A disabled tool is removed entirely, so the model can't even try it." |
| 2:40-2:52 | `r2 memory export`, open MEMORY.md in Notepad | "Memory is a file I can open, edit and import back. My data, in my hands." |
| 2:52-3:00 | Title card: repo URL, "Nemotron on Nebius Token Factory, Tavily, OpenShell policy" | "R2: a personal AI you can audit. Open source, MIT." |

Tips: cut every wait (model thinking) with a jump cut; keep the ledger on screen long enough to read; do not show
your real key, real phone number or real card (use the dummy ones above, which are standard test values).
