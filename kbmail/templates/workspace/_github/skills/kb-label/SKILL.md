---
name: "kb-label"
description: "Label exported messages by area into work/labeling/*.result.jsonl and import them. Use when there are open labeling batches or the user asks to label or classify new mail."
---
Label every open batch in `work/labeling/` (files `batch_NNN.md` without a matching
`batch_NNN.result.jsonl`). If there are none, run `kbmail label prepare` first.

For each batch:
1. Read the whole batch file. It lists the areas (with descriptions/keywords) and the messages.
2. For each message decide the area(s). Judge by what the message is *about*, not by who sent it alone:
   - use the subject, the text, the participants and attachments; rule scores are only hints
   - automated notifications, newsletters, HR/admin, social, spam → `others` unless clearly about an area
   - several labels only when the message substantially covers several areas
   - confidence: 0.9 obvious; 0.7 likely; 0.5 unsure (the user reviews the low ones first)
   - reason: a few words the user can verify quickly ("vendor X contract renewal", "build failure on Y")
3. Write `work/labeling/batch_NNN.result.jsonl`, one line per message, exactly:
   `{"id": "<id>", "labels": ["<area>"], "confidence": 0.8, "reason": "..."}`
   Include every message id from the batch. No code fences, no array brackets.
4. Continue with the next batch. After all batches, run `kbmail label import` and fix any reported lines
   (rewrite the result file and import again).

Finish by telling the user how many were labeled per area and to run `kbmail review`.
