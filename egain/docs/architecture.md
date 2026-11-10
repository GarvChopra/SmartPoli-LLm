# Architecture: SmartPoli Care Navigator (Isolated inside R2)

## Overview
To keep the hackathon implementation simple, reliable, and isolated from R2's desktop-assistant features, all eGain hackathon assets live under `egain/`, `r2/care_actions.py`, and `tests/care/`.

```
eGain Platform (Cloud)
 ├── AI Agent & Knowledge Hub (10 docs from egain/knowledge/)
 ├── Pre-chat Workflow (first_name, category)
 ├── Process Workflow: Reminder Troubleshooting (4 checks -> create_ticket action)
 └── Escalation Workflow (Callback action + Support Email)
        │
        │ HTTPS (via ngrok tunnel, authenticated with X-API-Key)
        ▼
R2 Care Actions Sandbox (`python -m r2.cli care` -> r2/care_actions.py)
 ├── POST /actions/create_ticket    (idempotent, PII-scrubbed, emergency-checked)
 ├── POST /actions/request_callback (demo phone 555-01xx validation)
 ├── POST /safety/check             (emergency & prompt-injection detection)
 └── GET  /health                   (liveness check)
```

## Isolation & Reuse
- **Existing R2 preserved:** No existing R2 modules, tools, or tests were deleted or modified except adding the `care` subcommand in `r2/cli.py` and documenting it in `README.md`.
- **Out-of-scope complexity isolated:** Earlier multi-workflow files (caregiver authorization, medication refill databases) are parked in `egain/out-of-scope/`.
- **Minimal dependencies:** Reuses R2's existing `fastapi`, `pydantic`, and `uvicorn` environment.
