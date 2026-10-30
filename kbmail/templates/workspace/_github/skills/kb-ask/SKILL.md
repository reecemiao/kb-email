---
name: "kb-ask"
description: "Answer a question from the knowledge base and mail with citations, and crystallize valuable answers into wiki pages. Use when the user asks what is known about a topic, project, person, or decision."
argument-hint: "Your question, e.g. what was decided about the payment API cut-over?"
---
The question is the text after `/kb-ask` (or the user's request that loaded this skill). If it is missing, ask for it.

1. Run `kbmail wiki search "<key terms>"` (try 2-3 phrasings, also `--raw` for sources not yet in the
   wiki). Read the top pages and follow their relations/links one hop (graph traversal).
2. If the wiki lacks the answer, search mail: `kbmail mail "<terms>"` and `kbmail show <id> --thread`.
3. Answer concisely with citations (wiki page paths / raw files / message ids). State confidence and note
   stale or superseded information.
4. If the answer is valuable and not already a page, offer to crystallize it into `wiki/topics/` or
   `wiki/procedures/` (with sources, index and log entries per SCHEMA).
