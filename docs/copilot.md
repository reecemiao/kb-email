# VS Code / Copilot setup

## What the workspace configures

* `.github/copilot-instructions.md`: always-on rules (folder ownership, CLI usage, output formats,
  privacy, how to ask the user questions).
* `.github/instructions/wiki.instructions.md` (`applyTo: wiki/**`) and `raw.instructions.md`
  (`applyTo: raw/**`): file-scoped rules applied when Copilot edits those folders.
* `.github/prompts/kb-*.prompt.md`: the jobs, invoked as `/kb-...` in Copilot Chat. They declare
  `agent: agent` so they run in agent mode (with file edits and terminal commands).
* `.vscode/settings.json`: enables prompt files and instruction files, and auto-approves `kbmail`
  terminal commands (`chat.tools.terminal.autoApprove`). Remove that entry if you want to approve each
  command yourself.

`kbmail init <folder> --upgrade` refreshes prompts, instructions and the schema after you update the
tool. It never overwrites `kbmail.yaml`, `config/*`, or your wiki pages.

| prompt | purpose |
|---|---|
| `/kb-start` | guided first run |
| `/kb-sync` | delta run: `kbmail sync`, then every due queue |
| `/kb-label` | label batches → `*.result.jsonl` |
| `/kb-rules` | write/refresh `config/rules.yaml`, evaluate, commit, journal |
| `/kb-select` | choose KB threads, raise questions |
| `/kb-format` | converted thread → clean `raw/` source |
| `/kb-images` | describe images (vision on) |
| `/kb-svg` | SVG → Mermaid or description (no vision needed) |
| `/kb-wiki-ingest` | delta ingest into the wiki per `SCHEMA.md` |
| `/kb-wiki-lint` | self-healing: links, stale pages, contradictions, duplicates |
| `/kb-ask` | answer from the wiki with citations; crystallize good answers |
| `/kb-people` | relationship inference → `wiki/people/` |
| `/kb-style` | learn your writing style from Sent Items |
| `/kb-draft` | draft a new mail or a reply |
| `/kb-suggest` | what to send/answer now (from `kbmail triage`) |

## Vision on or off

Set `copilot.vision` in `kbmail.yaml`.

* **Off** (common in enterprises): images are copied to `review/images/`, de-duplicated and filtered
  by size. `review/images/REVIEW.md` shows them in VS Code's Markdown preview next to their context.
  You write descriptions in `descriptions.yaml` (or `kbmail images describe`, which opens each image
  and asks in the terminal), then run `kbmail images import`. Mark irrelevant ones with `skip: true`.
* **On:** `/kb-images` lets Copilot fill the same YAML.
* **SVG either way:** SVGs are XML, so Copilot reads them as text. `/kb-svg` writes Mermaid
  (`review/images/<id>.mmd`) for flows, architectures, sequences, org charts and timelines (Mermaid
  is much easier for an LLM to use later than an SVG or a picture). Otherwise it writes a description.

Threads wait in the format queue until all their images are described, skipped or converted, so the
knowledge base never contains silent "[image]" holes.

## Model and context tips

* Use a strong model for `/kb-rules`, `/kb-wiki-ingest` and `/kb-people`. A fast model is fine for
  `/kb-label` and `/kb-select`.
* Batch sizes (`labeling.batch_size`, `kbmail kb prepare --batch-threads`) keep each file within a
  comfortable context size. Lower them if Copilot starts skipping items. The importers report
  missing ids, and those items return to the queue.
* Start a new chat for each prompt run to keep context clean.

## Automation beyond the chat window (optional)

The deterministic half is a CLI, so it can be scheduled (see README, Task Scheduler). The LLM half is
defined by file contracts and prompt files, so it can be driven headlessly when your organization
allows it:

* **GitHub Copilot CLI** in non-interactive mode can run a prompt with tool permissions restricted to
  `kbmail` commands and file edits inside the workspace, e.g. run `/kb-label` content nightly after
  `kbmail sync`.
* **GitHub Copilot SDK** (programmatic access to the same agent runtime) can wrap the loop
  "`kbmail label prepare` → agent writes results → `kbmail label import`" in a Python script.

Check your organization's Copilot policies before automating, and keep human review (`kbmail review`,
images, questions) interactive. kbmail does not depend on either option.

## Skills

If your VS Code has Agent Skills enabled, you can also expose the prompts as skills (one folder per
skill under `.github/skills/<name>/SKILL.md` with the prompt text as the body). The prompt files are
the canonical form because they work in every recent VS Code Copilot release.
