# Wiki schema

This file is the contract that turns Copilot into a disciplined knowledge worker for this
wiki. It follows the *LLM Wiki v2* pattern (rohitg00, extending Karpathy's LLM Wiki):
**compile knowledge once, keep it alive**: lifecycle, typed graph, delta automation,
quality control. Evolve this file together with the user; it is meant to change.

## 1. Layers

| layer | folder | owner | rule |
|---|---|---|---|
| sources | `raw/` | kbmail + `/kb-format` + the user | Read-only for wiki work. Never edit a raw file during ingest. |
| wiki | `wiki/` | Copilot | Everything here is compiled from sources and must cite them. |
| schema | `wiki/SCHEMA.md` | user + Copilot | Change only when the user agrees; log the change in `wiki/log.md`. |

`raw/` sub-folders: `email/<area>/` and `teams/<area>/` (formatted threads), `notes/` (the
user's answers, authoritative), `web/` (fetched documentation pages), `docs/` (the user's own documents).

## 2. Memory tiers (consolidation)

| tier | where | what | decays |
|---|---|---|---|
| working | `raw/` + `work/wiki/QUEUE.md` | unprocessed observations | n/a |
| episodic | `wiki/episodes/` | one page per source thread/document: what happened, when, who | fast |
| semantic | `wiki/people/`, `projects/`, `systems/`, `topics/`, `decisions/`, `glossary.md` | consolidated facts across episodes | normal |
| procedural | `wiki/procedures/` | how things are done here: workflows, checklists, recurring patterns | slow |

Promote upward as evidence accumulates: a fact seen in one episode stays in that episode page (and maybe
a semantic page with low confidence); confirmed by 2+ sources it belongs on a semantic page with higher
confidence; a pattern repeated 3+ times becomes a procedure.

## 3. Page types and folders

| type | folder | file name |
|---|---|---|
| `person` | `wiki/people/` | `firstname-lastname.md` (ASCII slug; put the native-script name in `aliases`) |
| `project` | `wiki/projects/` | slug of the project name |
| `system` | `wiki/systems/` | products, tools, services, environments |
| `topic` | `wiki/topics/` | recurring subjects that are not projects |
| `decision` | `wiki/decisions/` | `YYYY-MM-DD-short-slug.md` |
| `procedure` | `wiki/procedures/` | `how-to-...md` |
| `episode` | `wiki/episodes/` | `YYYY-MM-DD-short-slug.md` (one per raw source) |
| `area` | `wiki/areas/` | one hub page per configured area |
| `glossary` | `wiki/glossary.md` | single page; acronyms + internal jargon |
| `style` | `wiki/style/` | `writing-style.md` (the user's voice for drafts) |

Create a new page when an entity is mentioned in 2+ sources or is central to one source.
Otherwise mention it on the closest existing page. Prefer updating over creating.

## 4. Front matter (required on every page except index.md, log.md, SCHEMA.md)

```yaml
---
title: Payment API migration
type: project                # see section 3
aliases: [PAYMIG, 決済API移行]
areas: [area1]
status: active               # active | stale | superseded | archived
confidence: 0.8              # 0-1, see section 5
decay: normal                # fast | normal | slow  (episodes fast, decisions/procedures slow)
last_confirmed: 2026-10-20   # date of the newest source that supports the page
created: 2026-10-01
updated: 2026-10-27
sources:                     # raw files this page is compiled from (paths from workspace root)
  - raw/email/area1/2026-10-01_payment-api-cutover_t1a2b3c4d5e6.md
relations:                   # typed edges of the knowledge graph
  - {type: owned_by, target: people/sarah-tanaka, confidence: 0.9}
  - {type: depends_on, target: systems/billing-db, confidence: 0.7}
superseded_by:               # only when status: superseded
---
```

Relationship types (extend in this file when needed): `owned_by`, `member_of`, `reports_to`, `manages`,
`works_with`, `customer_of`, `vendor_of`, `uses`, `depends_on`, `part_of`, `decided_in`, `caused`, `fixed`,
`blocks`, `supersedes`, `contradicts`, `related_to`.

## 5. Claims, confidence, supersession

Write key facts as bullets under `## Facts`, each with its evidence:

```markdown
## Facts
- Cut-over is planned for 2026-11-15. (src: raw/email/area1/2026-10-20_cutover-plan_t9f8e7d6c5b4.md; conf: 0.8; seen: 2026-10-20)
- ~~Cut-over planned for 2026-11-01.~~ superseded 2026-10-20 by the line above (src: …2026-10-01…)
```

Confidence guide: 0.9+ stated by the owner/decision-maker or confirmed by 3+ sources or by the user
(`raw/notes/`); 0.7 one clear authoritative source; 0.5 one indirect source; 0.3 inferred / hearsay.
Page `confidence` = your judgement of the page as a whole.

When new information contradicts a claim: **do not silently overwrite**. Strike the old claim, link the
new one, and add a line to `## History`. If unsure which is right, keep both, add a `## Open questions`
item and ask the user with `kbmail questions add "..." --context "<page>"`. Prefer newer sources, the
owner's statements, and decisions over discussions.

Python computes *effective confidence* = confidence x 0.5^(age / half-life) from `last_confirmed` and
`decay` (`kbmail wiki lint`). Re-confirming a fact from a new source resets `last_confirmed`.

## 6. Page body layout

```markdown
# <title>
One-paragraph summary (what it is, why it matters, current state).

## Facts            (claims with evidence, section 5)
## Timeline         (dated events, newest first; for projects/decisions/people)
## Relationships    (prose about the typed relations, with [[links]])
## Open questions
## History          (supersessions, merges)
```

Links: `[[people/sarah-tanaka]]` or `[[people/sarah-tanaka|Sarah]]`, path relative to `wiki/` without
`.md`. Every page must be linked from `wiki/index.md` and at least one other page.

## 7. People pages (relationships)

Extra front matter for `type: person`: `emails`, `org`, `role`, `relationship` (to the mailbox owner:
`manager`, `skip-manager`, `direct-report`, `peer`, `stakeholder`, `customer`, `vendor`, `partner`,
`external`, `unknown`), `relationship_confidence`, `communication` (language, tone, formality,
preferred channel). `config/people.yaml` overrides always win. Never store personal data that is not
needed for work (no health, family, salary, private contact details).

## 8. Operations

* **Ingest (delta)**, `/kb-wiki-ingest`: process `work/wiki/QUEUE.md`. For each new raw file: create
  its episode page, update or create semantic pages, update relations, `index.md`, `log.md`, then
  `kbmail wiki mark <raw path>`. For changed files integrate only the diff; for deleted files remove the
  source from `sources`, lower confidence of claims that depended only on it, and mark pages `stale` when
  nothing supports them anymore.
* **Query**, `/kb-ask`: `kbmail wiki search "<question>"`, read the hits and their graph neighbours,
  answer with citations. If the answer is valuable and new, *crystallize* it: save it as a page
  (usually `topics/` or `procedures/`) citing the pages it came from.
* **Lint**, `/kb-wiki-lint` (weekly): `kbmail wiki lint`, then fix broken links, orphans, missing
  index entries, missing front matter; review stale pages (re-confirm, lower confidence, or archive);
  resolve contradictions (section 5); merge duplicate entities (keep one, add the other as alias, leave
  a redirect line in the removed page's place in index/log).

## 9. Quality bar (self-check before saving a page)

1. Every fact cites a raw source. No source, no fact.
2. Nothing secret: no passwords, tokens, personal data beyond work context (ingest is pre-redacted; stay alert).
3. Consistent with linked pages; contradictions are surfaced, not hidden.
4. Summary paragraph is useful on its own; the page is in `index.md`.
5. Score the page 1-5 in your head; below 4, improve it or flag it in `log.md`.

## 10. log.md (audit trail)

Append one line per operation, newest last:
`- 2026-10-27 ingest raw/email/... -> created episodes/..., updated projects/..., people/... (3 facts, 1 supersession)`
