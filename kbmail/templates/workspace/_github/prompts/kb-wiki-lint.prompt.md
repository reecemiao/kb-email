---
description: "Self-heal the wiki: lint, stale pages, contradictions, duplicates"
agent: agent
---
Run `kbmail wiki lint` and read `work/wiki/LINT.md`, then fix, following `wiki/SCHEMA.md`:
1. frontmatter / broken_links / missing_sources / not_in_index / orphans: repair directly (link orphans
   from the right hub page or index; fix or remove broken links).
2. stale: for each page, search for newer evidence (`kbmail wiki search`, `kbmail mail "<terms>"`). If
   re-confirmed, update `last_confirmed` and cite the source; if outdated, set `status: stale` or
   `archived`; if replaced, `status: superseded` + `superseded_by`.
3. supersession issues: add the missing `superseded_by`.
4. Look for contradictions between pages on the same entity and for duplicate entities (same person with
   two pages, a project under two names). Resolve per SCHEMA section 5/8; ask the user when unsure.
5. Append a summary line to `wiki/log.md` and run `kbmail wiki lint` again to confirm.
