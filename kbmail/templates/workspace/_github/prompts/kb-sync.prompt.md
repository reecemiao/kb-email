---
description: "Delta run: process only what is new since last time (mail, labels, rules, KB, wiki)"
agent: agent
---
Bring the workspace up to date, working only on deltas.

1. Run `kbmail sync` (add `--skip-export` if the user already exported or is not on Windows; add
   `--teams` if Teams export is configured). It exports new mail, applies the rules, prepares all
   queues and prints `kbmail status`.
2. Work through the "Next" list from the status output, in order, using the matching prompt:
   - labeling batches → `/kb-label`, then `kbmail label import`
   - proposed labels → ask the user to run `kbmail review` (only the new proposals are shown); also
     suggest spot-checking rule labels with `kbmail review --source auto` now and then
   - rules due → `/kb-rules` (read `config/rules_journal.md` first: build on previous conclusions)
   - KB selection → `/kb-select`, `kbmail kb import`, `kbmail kb convert`
   - images/SVG → `/kb-svg`, `/kb-images` or ask the user
   - questions → remind the user about `review/questions.yaml`, then `kbmail questions ingest`
   - format queue → `/kb-format`
   - wiki queue → `kbmail wiki queue`, `/kb-wiki-ingest`
3. If the last `wiki_lint` in `state/runs.json` is older than 7 days, run `/kb-wiki-lint`.
4. If `people_build` changed people (see `work/people/PEOPLE_DIGEST.md`, column `changed`), run `/kb-people`.
5. Finish with `kbmail triage` and `/kb-suggest`, and give the user a short summary of what changed.
