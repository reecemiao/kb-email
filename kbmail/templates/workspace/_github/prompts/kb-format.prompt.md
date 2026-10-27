---
description: "Turn converted threads into clean knowledge-source Markdown in raw/"
agent: agent
---
Run `kbmail kb queue` and open `work/convert/QUEUE.md`. For every thread under **Ready**:

1. Read the `source` file (`work/convert/<thread>/thread.md`: messages, converted attachments, image
   descriptions, Mermaid diagrams).
2. Write the `target` file under `raw/` with this structure:

   ```markdown
   ---
   title: <specific title>
   thread: <thread id>
   source_version: <source_version from the queue - copy exactly>
   areas: [<areas>]
   topics: [<topics>]
   period: <first date> .. <last date>
   participants: [<Name <email>>, ...]
   message_ids: [<ids>]
   ---

   # <title>

   ## Summary
   3-6 sentences: what this is about, outcome/current state.

   ## Key points
   - decisions, facts, numbers, owners, deadlines (each as a bullet; keep exact figures and names)

   ## Action items
   - [ ] who - what - by when (only if present)

   ## Conversation
   ### 2026-10-01 Sarah Tanaka → team
   Cleaned message text (verbatim content; remove greetings, signatures, disclaimers, quoted history,
   join-meeting boilerplate). Keep tables as Markdown tables.

   ## Attachments
   ### <file name>
   Cleaned converted content (headings, tables, lists). Keep image descriptions and Mermaid blocks.

   ## Open questions
   - things that remain unclear
   ```
3. If the target already exists (thread got new messages), update it: keep previous content, add the new
   messages, update the summary/key points, and set the new `source_version`.
4. Never invent content. Facts must come from the source. Unknown terms → `kbmail questions add ...`.

After all ready threads, run `kbmail kb queue` again; it should report them as done. Mention threads
still blocked on image descriptions. Then run `kbmail wiki queue`.
