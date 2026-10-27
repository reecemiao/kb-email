# VS Code / Copilot setup

Targets the current VS Code with GitHub Copilot (customization docs as of October 2026). The
workspace uses the portable formats, so the same files work in the **Local** and **Copilot** agent
harnesses, in GitHub Copilot CLI, and largely in other skills-compatible agents.

## What `kbmail init` puts in the workspace

| file | role | notes |
|---|---|---|
| `AGENTS.md` | always-on instructions: folder contract, privacy, output formats, how to ask questions | cross-agent format, read by Local and Copilot harnesses |
| `.github/skills/kb-*/SKILL.md` | one [agent skill](https://agentskills.io) per job, usable as `/kb-*` commands | the model also loads a skill on its own when the description matches the request |
| `.github/skills/*/…` resources | `raw-template.md`, `draft-template.md`, `rules-format.md` | loaded only when the skill reaches that step |
| `.github/skills/kbmail-cli/` | CLI reference | `user-invocable: false`: hidden from `/`, loaded when the model needs a command |
| `.github/agents/kb-librarian.agent.md` | **KB Librarian** custom agent | tools `read, edit, search, execute, todo, agent, askQuestions`, **no `web`**; handoff buttons (Local) for sync, wiki update, what needs a reply |
| `.github/hooks/kbmail.json` | `sessionStart` hook → `kbmail hook session-start` | injects `kbmail status` (counts + ordered next actions) as context; fails open |
| `.github/instructions/*.instructions.md` | `applyTo: raw/**` / `wiki/**` rules | attached automatically when those files are edited |
| `.vscode/settings.json` | `chat.useHooks` (Local), terminal auto-approve for `kbmail` | remove the auto-approve entry to confirm every command |

`kbmail init <folder> --upgrade` refreshes all of these after you update the tool. It never touches
`kbmail.yaml`, `.env`, `config/*` or your wiki pages. It also removes the files from earlier versions
(`.github/prompts/kb-*.prompt.md`, `.github/copilot-instructions.md`) when they are unmodified.
Use **Chat: Open Customizations** to see everything VS Code loaded. Right-click in the Chat view →
**Diagnostics** shows load errors.

## Skills

| skill | purpose |
|---|---|
| `/kb-start` | guided first run (manual only: `disable-model-invocation`) |
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

Skills replaced the earlier prompt files because prompt files only run in the Local harness, while
skills work in every harness, appear as slash commands, and load their resources only when needed.

## Typical session

1. Open the workspace and pick **KB Librarian** in the agent picker. The session-start hook has
   already given it the workspace state.
2. Type `/kb-sync` (or just "sync"). The agent runs `kbmail sync` and works through the queue. With
   several batches it hands each one to a subagent, so the main conversation stays small.
3. When it reaches a step that needs you (label review, images without vision, questions), it stops
   and says what to do. Run `kbmail review` in a terminal, then tell it to continue (or use the
   **Continue the sync** handoff button).

## Vision on or off

Set `copilot.vision` in `kbmail.yaml` (or `KBMAIL_COPILOT__VISION`).

* **Off** (common in enterprises): images are copied to `review/images/`, de-duplicated and filtered
  by size. `review/images/REVIEW.md` shows them in the Markdown preview next to their context. You
  write descriptions in `descriptions.yaml` (or `kbmail images describe`), then run
  `kbmail images import`. Mark irrelevant ones with `skip: true`.
* **On:** `/kb-images` lets Copilot fill the same YAML.
* **SVG either way:** SVGs are XML, so Copilot reads them as text. `/kb-svg` writes Mermaid
  (`review/images/<id>.mmd`) for flows, architectures, sequences, org charts and timelines, which is
  easier for an LLM to use later than a picture. Otherwise it writes a description.

Threads wait in the format queue until all their images are described, skipped or converted, so the
knowledge base never contains silent "[image]" holes.

## Permissions and privacy

* Keep **Manual permissions** (the default) or use the `kbmail` auto-approve rule from the workspace
  settings. Avoid **Allow all** / **Autopilot** on a mailbox workspace unless you trust every input:
  mail content is untrusted text and could carry prompt-injection attempts.
* The KB Librarian has no web tools. `kbmail` itself fetches only the links *you* put into
  `review/questions.yaml`.
* If your organization enables agent sandboxing, allow the workspace folder for terminal commands
  (`kbmail` reads and writes only inside it, except for Outlook COM and the Teams Graph export).

## Model and context tips

* Use a strong model for `/kb-rules`, `/kb-wiki-ingest` and `/kb-people`. A fast model is fine for
  `/kb-label` and `/kb-select`. A custom agent or handoff can pin a model if you want it fixed.
* Batch sizes (`labeling.batch_size`, `kb.select_batch_threads`) keep each file within a comfortable
  context size. Lower them if Copilot starts skipping items. The importers report missing ids, and
  those items return to the queue.
* Experimental: `context: fork` in a skill's frontmatter runs it in its own subagent context (needs
  `github.copilot.chat.skillTool.enabled`). This can help for `kb-label` / `kb-select` on large
  backlogs.

## Automation beyond the chat window (optional)

The deterministic half is a CLI, so it can be scheduled (see the README for Task Scheduler). The LLM
half is defined by skills and file contracts, which GitHub Copilot CLI also reads from the workspace.
For example, after a scheduled `kbmail sync`, a non-interactive Copilot CLI run can execute
`/kb-label` with tool permissions limited to `kbmail` and file edits in the workspace. The GitHub
Copilot SDK can drive the same loop from Python. Check your organization's Copilot policies first,
and keep the human steps (`kbmail review`, images, questions) interactive.
