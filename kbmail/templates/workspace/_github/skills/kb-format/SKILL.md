---
name: "kb-format"
description: "Turn converted threads in work/convert/ into clean knowledge-source Markdown under raw/. Use when work/convert/QUEUE.md lists ready threads."
---
Run `kbmail kb queue` and open `work/convert/QUEUE.md`. For every thread under **Ready**:

1. Read the `source` file (`work/convert/<thread>/thread.md`: messages, converted attachments, image
   descriptions, Mermaid diagrams).
2. Write the `target` file under `raw/` following [the raw file template](./raw-template.md): front matter
   (copy `source_version` exactly from the queue), summary, key points, action items, the cleaned
   conversation, cleaned attachments (keep image descriptions and Mermaid blocks), open questions.
3. If the target already exists (thread got new messages), update it: keep previous content, add the new
   messages, update the summary/key points, and set the new `source_version`.
4. Never invent content. Facts must come from the source. Unknown terms → `kbmail questions add ...`.

After all ready threads, run `kbmail kb queue` again; it should report them as done. Mention threads
still blocked on image descriptions. Then run `kbmail wiki queue`.
