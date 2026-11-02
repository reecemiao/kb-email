---
name: "kb-wiki-ingest"
description: "Ingest new or changed raw/ sources into the LLM wiki (delta, with diffs) following wiki/SCHEMA.md. Use when work/wiki/QUEUE.md has entries or the user asks to update the wiki."
---
Read [wiki/SCHEMA.md](../../../wiki/SCHEMA.md) first; it is the contract. Then run `kbmail wiki queue` and open `work/wiki/QUEUE.md`.

For each **new** raw file (one at a time, smallest first unless the user says otherwise):
1. Read it. Check `kbmail wiki search "<main entities>"` to find existing pages before creating new ones.
2. Create the episode page `wiki/episodes/YYYY-MM-DD-slug.md` (what happened, who, when, outcome; facts
   with citations).
3. Update or create semantic pages (people, projects, systems, topics, decisions, area hubs, glossary):
   add facts with `(src: ...; conf: ...; seen: ...)`, update `last_confirmed`, `sources`, `relations`;
   apply supersession when facts change; record contradictions under `## Open questions`.
4. Promote repeated patterns (3+ episodes) into `wiki/procedures/`.
5. Update `wiki/index.md` and append to `wiki/log.md`.
6. Run `kbmail wiki mark <raw path>`.

For each **changed** file: integrate only the diff shown in the queue (new messages, corrected facts);
update the episode page and affected semantic pages; mark it.
For each **deleted** file: remove it from `sources`, lower confidence of claims that only it supported,
set `status: stale` when nothing supports a page anymore; mark it.

Facts from `raw/notes/` are the user's own answers: confidence 0.9+, and they override email claims.
Unclear points → `kbmail questions add "..." --context "<raw file>" --step wiki-ingest`.
When the queue is empty, run `kbmail wiki lint` and report the summary.
