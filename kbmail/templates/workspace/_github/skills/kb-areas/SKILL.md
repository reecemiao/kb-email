---
name: "kb-areas"
description: "Propose the areas (topics) to sort mail into from a digest of the mailbox, then let the user pick which to keep. Use when the user has no areas yet, wants Copilot to suggest them, or asks what topics their mail covers or what the current areas miss."
---
1. Run `kbmail areas discover` (unless `work/areas/DISCOVER.md` was just written). It needs exported mail.
2. Read the whole digest and follow it: write `work/areas/proposals.result.jsonl`, run `kbmail areas import`,
   and fix any line it reports (rewrite the file, import again).
3. Show the user the numbered list the import printed (name, threads, description, keywords) and ask which to
   keep. They may rename, merge, split or reword proposals: change the result file and import again.
   Never pick for them.
4. Run `kbmail areas pick <numbers or names>` with their choice. It adds to the areas already in `kbmail.yaml`;
   add `--replace` only when they want those dropped.
5. Next step: `kbmail label prepare`, then `/kb-label`.
