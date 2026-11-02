---
name: "kb-people"
description: "Infer relationships between the mailbox owner and contacts from the people digest and update wiki/people pages. Use after `kbmail people build` reports changes or when the user asks who someone is."
---
Run `kbmail people build`, then read `work/people/PEOPLE_DIGEST.md`, `config/people.yaml` (user overrides
always win) and [wiki/SCHEMA.md](../../../wiki/SCHEMA.md) section 7.

If the directory is configured (`kbmail directory sync`; the `org` column is filled), the reporting
lines in `org` are facts from the corporate directory: `manager`, `skip-manager (+n)`, `direct-report`,
`indirect-report`, `peer` (same manager), `same-department`, `colleague`. Use them as the
`relationship` with `relationship_confidence: 0.95` and add `reports_to` / `manages` relations. Rows
marked with no recent mail are still important (your manager chain, your reports). Mail signals then
add *how* you work together (stakeholder, approver, escalation point, frequent collaborator). For
anyone without an `org` entry, infer it from mail as below. To check a single person, run
`kbmail directory lookup <email>` (it prints JSON; keep phone numbers and other personal contact
details out of the wiki).

For people marked `changed` (all of them on the first run), within the top of the table:
1. Infer the relationship to the mailbox owner from the signals: they CC the owner on their mail
   (manager/stakeholder patterns), the owner CCs them (keeping a manager informed), fast owner replies
   (seniority/priority), external domains (customer/vendor/partner, decide from the content), meeting
   frequency, shared areas, who they appear with. Read a few threads (`kbmail mail "<name>"`,
   `kbmail show <id> --thread`) to confirm role and title from signatures and content.
2. Create/update `wiki/people/<slug>.md` with front matter (`emails`, `org`, `role`, `relationship`,
   `relationship_confidence`, `areas`, `relations` such as `reports_to`, `works_with`, `member_of`,
   `customer_of`) and sections: summary, responsibilities, what they usually ask the owner for,
   communication preferences (language, tone, formality), timeline of notable interactions with citations.
3. Keep `wiki/people/_relationships.md`: a Mermaid graph (owner at the centre, edges labelled with the
   relationship) and a table of the top contacts.
4. Low-confidence guesses (< 0.6) → add a question: `kbmail questions add "Is <name> your manager or a peer?" --step people`.
Update `wiki/index.md` and `wiki/log.md`.
