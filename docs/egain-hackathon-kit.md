# eGain Hackathon Kit: "Penny", the Card & Account Help Agent

Industry: Retail banking (fictional "Maple Credit Union"). Use case: card problems and account questions, resolved end to end, with safe escalation.

## 1. Knowledge content (upload as 3 to 4 docs or web pages you own)
Write these as plain FAQ-style docs (fictional data only):
1. **Cards FAQ**: lost or stolen card, freezing a card, replacement time (5-7 business days, expedited 2 days for $15), card limits, foreign transaction fee (1%).
2. **Fees & Accounts FAQ**: monthly fees, overdraft policy, ATM limits, how to dispute a charge (60-day window), wire cutoff times.
3. **Security FAQ**: we never ask for PIN or full card number in chat, how to spot phishing, fraud reporting steps.
4. **Hours & Contact**: branch hours, phone (demo: 555-0100), email (demo@maplecu.example).

After training: review the generated intents, merge duplicates, and test 15 questions. Add 2 or 3 questions that are out of scope (for example "Should I buy Tesla stock?") and confirm the bot declines and offers a human.

## 2. Workflows
**Pre-chat** (collect before the AI Agent starts): Name, Email, "What do you need help with?" (dropdown: Lost/stolen card, Dispute a charge, Fees question, Other), "Is this urgent/fraud?" (yes/no). Urgent = yes routes straight to the lost/stolen workflow.

**Process workflow A: Lost or stolen card** (linked to the intent "Lost or stolen card")
1. Ask: Lost or stolen? (stolen leads to a fraud flag)
2. Ask: Any unrecognized transactions? Yes leads to the dispute workflow.
3. Confirm the last 4 digits (demo data only).
4. Action: Freeze card (see Actions).
5. Ask: standard (free) or expedited ($15) replacement.
6. Confirm the summary and give a reference number.

**Process workflow B: Which card fits me?** (recommendation) Ask spending habits (travel / groceries / building credit) and fee tolerance, then recommend Travel, Cashback, or Starter card.

**Escalation workflow** (at least 2 options): 
- Live chat with an agent (shown when the user says "agent", the bot fails twice, sentiment is angry, or the fraud flag is set).
- Request a callback (collect phone and time window).
- Email the support team (demo address).
Escalation passes the pre-chat data and a conversation summary so the customer does not repeat themselves.

## 3. Actions (the distinctive feature)
- `freeze_card(last4)`: instantly freezes the demo card.
- `order_replacement(last4, speed)`: returns a reference number.
- `book_callback(phone, window)`.
Good judgment rules, stated in the agent instructions: never ask for a PIN or full card number. Decline investment, legal, and tax advice. Anything over a $500 dispute, account closure, or a suspected account takeover escalates to a human.

If real actions are hard to set up, use a mock endpoint (webhook.site or a small Flask or Cloudflare Worker returning JSON). Disclose that it's a demo backend.

## 4. Checklist
- [ ] Knowledge trained, 15 questions tested, language matches the docs
- [ ] Pre-chat, both process workflows, and escalation published, each linked to an intent
- [ ] Actions enabled and added to the AI Agent
- [ ] Tested in Test AI Agent, then via the published link (wait up to 5 min)
- [ ] Demo link copied, video recorded, form submitted before **Nov 15, 11:59 PM PST** (that's Nov 16, 12:29 PM IST)

## 5. Description (about 120 words, paste into the form)
Penny is an AI support agent for Maple Credit Union (fictional) that helps members when they are most stressed: a lost or stolen card. Instead of long phone queues, Penny answers questions from the credit union's own policies, then walks members through a guided workflow. It checks for fraud, freezes the card, and orders a replacement on the member's behalf. A pre-chat form captures the issue and urgency, so urgent cases skip the queue. Penny knows its limits: it never asks for PINs, declines financial advice, and hands off to a live agent, callback, or email with the full context when a case is risky or complex. This cuts handle time, reduces fraud losses, and keeps members safe.

## 6. One-minute video script
- 0:00-0:08 Problem: "Lost your card at midnight? Calling support takes 30 minutes. Meet Penny."
- 0:08-0:20 Pre-chat form (select "Lost/stolen", urgent = yes). Ask "What's the foreign transaction fee?" and show the grounded answer.
- 0:20-0:45 **Lost-card workflow**: the card is frozen via the action, replacement ordered, reference number shown.
- 0:45-0:55 Ask for stock advice: Penny declines. Say "I want a person": show the escalation options with context handed off.
- 0:55-1:00 Value: "Resolved in two minutes, no queue, no risky guesses."
