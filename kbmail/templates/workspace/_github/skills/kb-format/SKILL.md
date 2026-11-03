---
name: "kb-format"
description: "Write the summary of converted threads in work/convert/ (summary.md), then let kbmail assemble the raw/ knowledge-source files. Use when work/convert/QUEUE.md lists ready threads."
---
Run `kbmail kb queue` and open `work/convert/QUEUE.md`. For every thread under **Ready**:

1. Read `work/convert/<thread>/thread.md` (messages, converted attachments, image descriptions,
   Mermaid diagrams). If the queue names **only the new messages**, read just those
   `## Message <id>` sections.
2. Write `work/convert/<thread>/summary.md` following [the summary template](./summary-template.md):
   summary, key points, action items, open questions. Only these parts: **do not copy the
   conversation or attachments**. `kbmail kb assemble` adds them from the mail itself, together with
   the front matter (thread, areas, period, participants, message ids, source version).
3. Update (queue says `update:`): kbmail has copied the current summary into `summary.md`. Keep what
   is still true, add what the new messages change (decisions, dates, owners), and mark replaced
   facts as changed. kbmail refuses a file left unchanged.
4. Quoted mail: a `--- quoted: sender | date | subject ---` block is an older message that exists only
   as a quote (for example the mail an "FYI" forward forwards). Its content counts like any message.
5. Never invent content. Facts must come from the source. Unknown terms → `kbmail questions add ...`.

Then run `kbmail kb assemble`. It writes the raw files and lists any `summary.md` to fix (missing
`## Summary` / `## Key points`, conversation pasted in); fix those and run it again. Mention threads
still blocked on image descriptions. Then run `kbmail wiki queue`.
