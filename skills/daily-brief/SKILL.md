---
name: daily-brief
description: Give a short morning brief from the saved facts about the user, plus weather or news if they ask for it.
---
1. Start from the user's saved facts (they are in your prompt): routines, deadlines, people, preferences.
2. If the user asked for news or weather, use web_search once for each and keep one line per item.
3. Reply in at most 6 short lines: today's date, what matters today from the facts, then the extras.
4. If you know nothing yet, say so and suggest two things worth telling you to remember.
5. Never invent a meeting, deadline or appointment that is not in the facts.
