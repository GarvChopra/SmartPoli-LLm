---
name: find-and-summarize
description: Find a document on this PC by name or topic, read it, and give a short summary with the key points.
---
1. Use find_files with the words the user gave (try the name, then likely extensions: pdf, docx, txt).
2. If several match, list at most 5 with their folders and ask which one, unless one is clearly the best.
3. Read it with read_file. For long files read the start, then the headings or the end.
4. Reply with: a one-sentence summary, up to 5 key points, and any dates, amounts or deadlines mentioned.
5. State only what the file says. If you could not read part of it, say so. Do not guess.
