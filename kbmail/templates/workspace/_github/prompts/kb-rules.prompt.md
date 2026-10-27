---
description: "Create or refresh config/rules.yaml from confirmed labels; record conclusions"
agent: agent
---
Goal: deterministic rules that label new mail like the user would, so later runs need less LLM work.
High precision matters more than coverage: anything rules don't decide goes to `/kb-label` anyway.

1. Run `kbmail rules prepare` and read `work/rules/RULES_CONTEXT.md` fully: current rules and their
   precision/recall against confirmed labels, mistakes, mined signals (sender, domain, subject token,
   category with support/purity), uncovered examples, labels confirmed since the last refresh, and
   **previous conclusions** from `config/rules_journal.md`. Build on those conclusions; don't undo a
   past decision without a reason from the new data.
2. Read `config/rules.yaml` (format documented at the top of the file).
3. Edit the rules:
   - fix rules that cause mistakes (narrow them with extra `when` fields, add `unless`, lower weight, or
     disable with `disabled: true` and a description of why)
   - add rules for strong signals: support >= 3 and purity >= 0.9 → weight = threshold; weaker
     signals → lower weight so two must agree
   - prefer stable signals: sender domains, distribution lists, project codes in subjects (use `re:`),
     Outlook categories. Avoid one-off people or generic words
   - add `others` rules for newsletters/notifications (`from: [noreply, no-reply]`, etc.)
   - keep ids stable; new ids continue the sequence; every rule gets a `description`
4. Run `kbmail rules evaluate`. Iterate (step 3) until no area has precision below 0.95 where possible.
   Report the coverage/accuracy trade-off.
5. Run `kbmail rules commit --note "<one-line summary>"`. It validates, snapshots to
   `state/rules_history/`, records the run and re-applies rules to unreviewed mail.
6. Append a dated entry to `config/rules_journal.md`: what changed and why, metrics before → after,
   patterns noticed (e.g. "area2 mail often comes from finance DL"), open doubts to check next time.
