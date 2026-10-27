---
name: "kb-start"
description: "Guided first run of a kbmail workspace: configure areas, export mail, label, review, rules, KB selection, images, questions, wiki, style. Use when the user sets up kbmail for the first time or asks how to get started."
disable-model-invocation: true
---
Guide the user through the first full run of this workspace, one stage at a time. Before each stage run
`kbmail status`, explain in two sentences what will happen, do it, and show the result. Pause for the
user whenever a stage needs them (marked USER).

1. Check `kbmail.yaml`: `me.emails` and `areas` (name + description + keywords) must be filled. If not,
   ask the user for them and edit the file. Ask whether Copilot vision is allowed and set `copilot.vision`.
2. Export: ask how many days, then run `kbmail export --days <n>` (default Inbox + Sent Items, no
   subfolders; mention `--include-subfolders`, `--all-folders`, `--folder`). On non-Windows machines the
   user can use `kbmail import-eml <folder>` instead.
3. Labeling: `kbmail label prepare`, then follow `/kb-label` for every batch, then `kbmail label import`.
4. USER: ask the user to run `kbmail review` in a terminal (interactive; or `kbmail review export-csv`
   for Excel). Wait until they say they are done.
5. Rules: follow `/kb-rules`.
6. KB selection: `kbmail kb prepare`, follow `/kb-select`, `kbmail kb import`, `kbmail kb convert`.
7. Images and diagrams: if `kbmail status` lists pending images, follow `/kb-svg` for SVGs; for bitmaps
   follow `/kb-images` when vision is enabled, otherwise USER: fill `review/images/descriptions.yaml`
   (preview in `review/images/REVIEW.md`) and run `kbmail images import`.
8. USER: open questions in `review/questions.yaml` (answers, links to documentation). Then
   `kbmail questions ingest`. The user may also drop documents into `raw/inbox/` and run `kbmail raw import`.
9. Format: follow `/kb-format` until `work/convert/QUEUE.md` has no ready threads.
10. People: `kbmail people build`, then follow `/kb-people`.
11. Wiki: `kbmail wiki queue`, follow `/kb-wiki-ingest`, then `kbmail wiki lint` and `/kb-wiki-lint`.
12. Style: `kbmail style`, follow `/kb-style`.
13. Finish: run `kbmail triage` and follow `/kb-suggest`. Explain the delta routine (`/kb-sync`).
