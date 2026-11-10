---
name: tidy-downloads
description: Sort a messy folder (usually Downloads) into subfolders by file type, without deleting anything.
---
Goal: make a cluttered folder tidy, never lose a file.

1. Call list_dir on the folder the user named (default: their Downloads folder).
2. Group the files by extension: Documents (pdf, docx, txt, xlsx, pptx), Images (png, jpg, jpeg, gif, webp),
   Installers (exe, msi, zip, rar), Media (mp3, mp4, mkv), Other.
3. Tell the user the plan in one short list (how many files go to each folder) BEFORE moving anything.
4. Only move files after the user agrees. Use run_shell with move commands; never delete files.
5. Do not touch files modified in the last 24 hours, or folders that already exist inside the target.
6. Finish with a count per folder and mention anything you skipped.
