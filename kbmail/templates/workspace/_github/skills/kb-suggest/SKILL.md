---
name: "kb-suggest"
description: "Suggest which emails to send or answer now (from `kbmail triage`), prioritized, and draft the chosen ones. Use when the user asks what needs attention, what to reply to, or what to follow up on."
---
1. Run `kbmail triage` and read `work/triage/TRIAGE.md` (heuristic: threads waiting for the user's reply,
   questions the user asked without an answer, meetings in the next 3 days).
2. For the top candidates read the thread (`kbmail show <id> --thread`) and check the wiki
   (`kbmail wiki search`) for what is already known and committed (action items, deadlines in raw/ and
   wiki/ pages).
3. Produce a prioritized list (max ~10): who, what they need, why now (deadline, seniority from
   `wiki/people/`, age), suggested action (reply / follow up / send agenda / no action needed) and a
   one-line gist of the reply. Drop false positives (FYIs, automated mail, already answered elsewhere,
   e.g. in Teams).
4. Ask which ones to draft, then follow `/kb-draft` for each chosen item.
