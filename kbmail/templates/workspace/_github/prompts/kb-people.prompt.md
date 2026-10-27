---
description: "Infer relationships and update wiki/people pages from the people digest"
agent: agent
---
Run `kbmail people build`, then read `work/people/PEOPLE_DIGEST.md`, `config/people.yaml` (user overrides
always win) and `wiki/SCHEMA.md` section 7.

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
