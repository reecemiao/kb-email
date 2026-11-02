---
name: "kb-select"
description: "Decide which labeled mail threads contain durable knowledge for the knowledge base, and raise questions about unknown terms. Use when there are open KB selection batches in work/kb_select/."
---
Process every open batch in `work/kb_select/` (`batch_NNN.md` without `batch_NNN.result.jsonl`). If there
are none, run `kbmail kb prepare`.

For each thread in a batch, decide `select` or `skip`. Select when the thread contains durable knowledge:
decisions and their reasons, specifications, designs, configurations, procedures, explanations, status
and plans of projects, ownership and responsibilities, problems with root cause and fix, important
numbers/dates, commitments. Skip pure scheduling, acknowledgements, FYI forwards without content,
newsletters, and threads whose content is fully contained in another selected thread.

For selected threads:
- `title`: specific and searchable ("Payment API cut-over plan and rollback criteria", not "Re: update")
- `messages`: `all`, or the list of ids to keep (drop "thanks"/"+1" messages)
- `attachments`: `all`, `none`, or the attachment file names worth converting (specs, slides, sheets)
- `topics`: 2-5 keywords
- `questions`: anything you could not understand and the user could clarify: unknown acronyms, systems,
  projects, people's roles, references to documents/links that should be provided. These go to
  `review/questions.yaml` automatically.

Write one JSON object per line to the result file named in the batch, e.g.
`{"thread": "t…", "decision": "select", "title": "…", "reason": "…", "topics": ["…"], "messages": "all", "attachments": "all", "questions": []}`

Then run `kbmail kb import` and `kbmail kb convert`, and tell the user how many threads were selected,
how many images need descriptions, and how many questions are open.
