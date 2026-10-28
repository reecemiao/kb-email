# Architecture

## Principles

1. **Split by capability.** Python does what must be exact and repeatable: COM export, IDs, dedup,
   watermarks, diffs, validation, scoring, redaction. Copilot does what needs language understanding:
   labeling, rule authoring, selection, formatting, wiki compilation, drafting.
2. **File contracts between them.** Every Copilot step reads a generated Markdown work file and writes
   a result in a fixed format (JSON Lines, YAML, or Markdown with required front matter). kbmail
   validates and imports it. Bad lines are reported and the items go back to the queue. Nothing
   depends on a particular LLM API, so the same contracts work with Copilot Chat, Copilot CLI, or any
   other agent.
3. **Deltas everywhere.** Each stage keeps its own state, so a rerun only touches new or changed items.
4. **Human in the loop where it matters.** Label confirmation, image descriptions without vision,
   answers to open questions, relationship overrides.
5. **Private data stays in the workspace.** The tool repo holds no data. The workspace holds data,
   state and the agent customizations (skills, agent, hook).

## Settings

`DEFAULT_CONFIG` in `kbmail/workspace.py` is the single source of truth for setting names and types.
It is overlaid by `kbmail.yaml`, then the workspace `.env`, then the current directory's `.env`, then
real `KBMAIL_*` environment variables, and finally CLI flags (`Workspace.opt`). Variable names map
nested keys with a double underscore (`outlook.include_subfolders` → `KBMAIL_OUTLOOK__INCLUDE_SUBFOLDERS`).
Values are coerced to the default's type. `.env.example` is generated from `DEFAULT_CONFIG` and
`CONFIG_HELP`, so it cannot drift. A test checks that uncommenting it reproduces the defaults.
`KBMAIL_WORKSPACE` selects the workspace like `--workspace`.

## Workspace layout

```
kbmail.yaml                 configuration (areas, me, export defaults, vision, privacy, teams)
.env, .env.example          KBMAIL_* overrides (see Settings); .env is git-ignored
config/
  rules.yaml                labeling rules (Copilot-written, kbmail-validated)
  rules_journal.md          conclusions of every rules refresh (read at the next refresh)
  people.yaml               relationship overrides / aliases / ignore list
data/                       kbmail-owned exports (read-only for Copilot)
  messages/outlook/YYYY-MM/<YYYYMMDD-HHMM>_<id>.md     one message, YAML front matter + body
  messages/outlook/YYYY-MM/<id>_files/                 attachments + body.html
  messages/teams/YYYY-MM/...                           one record per chat per day
  messages/teams/_raw/<chat>/<day>.json                merged raw chat messages
  calendar/YYYY-MM/<...>.md                            one event (attendees, organizer, agenda)
state/                      kbmail-owned state (JSON)
  index.jsonl               message ledger (id, thread, from/to, folder, attachments, path)
  calendar_index.jsonl
  export_state.json         per-folder watermarks (delta export)
  labels.json               id -> labels, status (proposed|auto|confirmed), source, confidence, history
  label_batches.json        open labeling batches
  rules_history/            snapshot of every committed rules.yaml
  kb_selection.json         thread -> select/skip, title, topics, assessed message ids
  kb_convert.json           thread -> signature, raw target, images, formatted?
  images.json               image registry (dedup by hash, status, description)
  people.json               relationship statistics
  directory.json            corporate directory cache (titles, managers; personal data, git-ignored)
  wiki_manifest.json        raw file -> sha256 at ingest
  wiki_snapshots/           ingested copy of each raw file (for diffs)
  wiki_graph.json           typed graph (front-matter relations + wikilinks)
  wiki_audit.jsonl          ingest/update/forget log
  runs.json                 last run + history of every step (drives "due" logic)
work/                       generated queues for Copilot
  labeling/batch_NNN.md (+ .result.jsonl)
  rules/RULES_CONTEXT.md
  kb_select/batch_NNN.md (+ .result.jsonl)
  convert/<thread>/thread.src.md, thread.md, QUEUE.md
  wiki/QUEUE.md, LINT.md
  people/PEOPLE_DIGEST.md, triage/TRIAGE.md, style/SAMPLES.md
review/                     things for the human
  labels_review.csv         optional Excel review
  images/                   pending images, descriptions.yaml, REVIEW.md, <id>.mmd
  questions.yaml            open questions + answers
raw/                        knowledge sources for the wiki
  email/<area>/, teams/<area>/   formatted threads (/kb-format)
  notes/                    the user's answers (authoritative)
  web/                      pages fetched from links in answers
  docs/                     user documents converted from raw/inbox/
  inbox/                    drop zone (pdf, docx, pptx, xlsx, html, ...) -> `kbmail raw import`
wiki/                       the knowledge base (SCHEMA.md, index.md, log.md, people/, projects/, ...)
drafts/                     email drafts (Markdown + front matter); `kbmail draft-outlook` -> Outlook Drafts
AGENTS.md                   always-on agent instructions (folder contract, privacy, output formats)
.github/skills/<name>/      SKILL.md per job (/kb-* commands) + bundled templates; kbmail-cli reference
.github/agents/             kb-librarian.agent.md (tools without web, handoffs)
.github/hooks/kbmail.json   sessionStart -> `kbmail hook session-start` (status as context)
.github/instructions/       raw.instructions.md, wiki.instructions.md (applyTo globs)
.vscode/settings.json       Local-harness hooks on, auto-approves `kbmail` terminal commands
README.md                   workspace guide, incl. version control (what is tracked and why)
.gitignore, .gitattributes  kbmail-managed block (refreshed on upgrade) + your own rules
.githooks/pre-commit        `kbmail git check --staged` (enable: `kbmail git setup`)
```

## Identifiers

* message id `m` + sha1(Internet Message-ID or EntryID)[:11]. The same mail found in two folders is
  exported once.
* thread id `t` + sha1(ConversationID)[:11] (Outlook), chat id (Teams), or normalized subject as
  fallback.
* image id `img_` + sha256(content)[:10]. The same logo in 200 mails is one image, and images below
  `images.min_bytes` are ignored.

## Outlook export details (pywin32)

* Items are sorted by `ReceivedTime` (descending) and iteration stops at the cut-off. No
  `Items.Restrict` date strings, which are locale-dependent and a common source of bugs.
* pywin32 returns Outlook's local wall-clock times tagged as UTC. kbmail re-reads them as local time.
* Exchange senders/recipients (`EX` addresses) are resolved to SMTP via `PR_SMTP_ADDRESS` /
  `PR_SENDER_SMTP_ADDRESS` / `GetExchangeUser()`.
* HTML bodies are saved (`body.html`), so tables and inline images (`cid:`) survive conversion.
  Inline images are matched to attachments by Content-ID.
* Calendar uses `IncludeRecurrences` + `Restrict` (required for recurring series), with a DASL fallback
  and a Python range check. Teams join boilerplate is removed from agendas.
* Defaults: Inbox + Sent Items, no subfolders. `--include-subfolders`, `--all-folders` (every mail
  folder in the default store except Deleted/Junk/Outbox/Drafts/Sync Issues), `--folder` (repeatable;
  `inbox`, `sent`, `Inbox/Sub/Folder`, or `\\Store Name\Folder`), `outlook.extra_folders` for archives.

## Labeling and rules

Labels have a status: `proposed` (Copilot), `auto` (rules), `confirmed` (you). Confirmed labels are
never overwritten. Rules (`config/rules.yaml`) are weighted conditions on sender, domains,
participants, subject/body (substring or `re:` regex), folder, direction, categories and attachment
names. An area is assigned when the summed weight reaches the threshold, and several areas can match.
When rules change, `auto` labels are re-evaluated, and mail that no rule covers any more goes back to
the Copilot queue.

`kbmail rules evaluate` measures the rules against confirmed labels (per-area precision/recall,
coverage, accuracy, list of mistakes). `kbmail rules prepare` adds mined signals: features with
support ≥ 3 and purity ≥ 0.8 among confirmed labels.

## Quoted history (replies and forwards)

`kbmail/history.py` splits every body into its own text and the quoted messages below it: Outlook
`From/Sent/To/Cc/Subject` blocks (English, Japanese, Chinese; also on one line, as HTML mail renders
them), `-----Original Message-----`, and `On ... wrote:` with `>` quoting. For each quoted message
it checks whether that message is exported on its own. A match needs three things:
- **sender:** the same e-mail address, or the same name in any order ("MORGAN, Alex" = "Alex MORGAN");
- **time:** the quoted time ("2026年7月30日 15:48", "Thursday, July 30, 2026 3:48 PM") is the exported
  message's time in some time zone (whole or half hours, a few minutes' tolerance). A copy from
  another day never matches;
- **text:** the exported message's own text is found in the quote (letters and digits only, so line
  wrapping, markdown and links don't matter).

`state/history.json` keeps a small fingerprint per message (rebuilt for new or changed messages only).
Wherever mail text is shown (labeling batches, `kbmail review`, rules, `kbmail mail`, `kbmail show`,
KB selection and conversion), a message is its own text, plus quoted messages that are not exported,
as `--- quoted: sender | date | subject ---` blocks, plus a one-line pointer for those that are. A
forward that says only "FYI" therefore carries what it forwards. Within a thread, the same quoted
text is shown once. KB conversion counts a quote as "exported" only when the original is converted
in the same thread, so nothing is lost when Copilot drops messages from a selection. If the time or
the text doesn't fit, the quote is kept: duplicated text is cheaper than lost text. Replies also
record who was added compared with the message they answer (`[added to the conversation here: ...]`).
`kbmail history` reports counts and `kbmail history show <id>` explains one message.

## KB conversion

For each selected thread: HTML body → Markdown (markitdown); quoted history handled as below;
attachments converted with markitdown (docx, pptx,
xlsx, pdf, msg, html, ...); images inside docx/pptx/xlsx are extracted from the zip media folder;
images and SVGs are registered and replaced by markers; secrets are redacted (`privacy.*`). Pending
SVGs then go through `svg2mermaid`, which tries three things in order: the draw.io model when present
(exact), then geometry for simple diagrams, and otherwise a digest for Copilot. `.svgz` is
decompressed. Embedded `data:` images are extracted into the image registry, and an SVG counts as
resolved only when those are too.
`thread.md` is re-rendered whenever image descriptions change. A thread is "formatted" when its raw
file's front matter carries the current `source_version`.

## Wiki (LLM Wiki v2)

`wiki/SCHEMA.md` is the contract: page types, memory tiers (episodic → semantic → procedural), front
matter with `confidence`, `decay`, `last_confirmed`, `sources`, typed `relations`, claim-level
citations, supersession instead of overwrite, and the ingest/query/lint operations. kbmail provides:

* **delta ingest queue** with diffs (manifest + snapshots)
* **lint**: required front matter, broken links/relations, missing sources, orphans, pages missing
  from the index, supersession without `superseded_by`, and **stale pages**.
  Effective confidence = `confidence × 0.5^(age / half-life)` with half-lives per `decay` class
  (`wiki.half_life_days`)
* **hybrid search**: BM25 (CJK bigram tokenization, so Japanese works without a morphological
  analyzer) plus one-hop graph expansion, combined with reciprocal rank fusion and down-weighted by
  effective confidence and supersession
* **graph** from relations and wikilinks, with Mermaid neighbourhood rendering (`kbmail wiki graph --page ...`)
* **audit trail** (`state/wiki_audit.jsonl`) next to the human-readable `wiki/log.md`

Vector search is intentionally left out to avoid an embedding service. The search function is the
place to add one (e.g. a local sentence-transformers index) if the wiki grows past a few hundred pages.

## Privacy

* Redaction before raw/: passwords, API keys/tokens (AWS, GitHub, Slack, OpenAI-style, Bearer),
  private keys, Luhn-valid card numbers, plus your own `privacy.extra_patterns`. Previews in labeling
  and selection batches are redacted too.
* Copilot instructions forbid sending content to external services and storing non-work personal data.
* The MSAL token cache (Teams) is git-ignored in the workspace.
* `kbmail` never sends mail. `draft-outlook` only saves to Drafts.
