# Knowledge Sources & Traceability Matrix

All 10 knowledge documents in `egain/knowledge/` are original, synthetic, and explicitly labeled as simulated for the hackathon.

| Document | Topic Covered | Sample Grounded Question | Verified Fact |
|---|---|---|---|
| `01-service-overview.md` | Service purpose, support hours, simulated notice | What are your support hours? | Monday to Friday, 8 AM to 6 PM Pacific time |
| `02-supported-features.md` | Reminders, missed-dose logging, daily limits | How many reminders can I get each day? | Up to 6 reminders per day per medication, in 15-minute steps |
| `03-patient-onboarding.md` | Account setup time, required info, privacy | How long does onboarding setup take? | About 10 minutes |
| `05-reminder-troubleshooting.md` | 4-step troubleshooting order, 5-min delay limit | What should I check if my reminders are not arriving? | Notifications allowed, Do Not Disturb off, time zone matches, mobile number confirmed |
| `06-missed-dose-logging.md` | Logging steps, 24-hour edit window, no dose advice | Can I edit a missed dose log later? | Yes, for 24 hours after logging it |
| `08-privacy-and-data-handling.md` | Minimal data collection, PII scrubbing, synthetic data | Is the patient data in this demo real? | No, synthetic data only |
| `09-escalation-and-contact-options.md` | Support ticket, callback (`555-0100`), email (`help@smartpoli.example`) | How can I contact a human? | Support request, callback, or email `help@smartpoli.example` |
| `10-known-limitations.md` | No diagnosis, no prescription orders, no dose changes | Can SmartPoli order my prescription refill? | Does not place prescription orders |
| `11-emergency-disclaimer.md` | Emergency symptoms, 911, Poison Control (`1-800-222-1222`) | What is the poison control number? | 1-800-222-1222 |
| `12-faq.md` | Consolidated FAQ pairs for rapid intent matching | How late can a reminder arrive? | Up to 5 minutes late |
