---
name: "kbmail-cli"
description: "Reference for the kbmail command-line tool used in this workspace (export, labeling, review, rules, KB selection and conversion, images, questions, wiki bookkeeping, people, triage, drafts, Teams, settings). Use when you need the exact command or options for a kbmail step."
user-invocable: false
---
# kbmail CLI reference

Run commands from the workspace folder (or set `KBMAIL_WORKSPACE`). Every option without a value
falls back to a setting (`kbmail config show` lists them with their `KBMAIL_*` names). Add `-h` to
any command for details.

## State and settings

| command | what it does |
|---|---|
| `kbmail status [--json]` | counts, last runs, and the ordered "Next" list |
| `kbmail config show [prefix]` | effective settings, their source (default / kbmail.yaml / .env / environment) and variable name |
| `kbmail config env` | print a commented `.env` template |
| `kbmail sync [--no-export] [--teams]` | delta run of every deterministic step, then status |
| `kbmail hook session-start` | JSON status context for the session-start hook (used by `.github/hooks/kbmail.json`) |
| `kbmail init [path] [--upgrade]` | create a workspace, or refresh skills/agents/schema in it |
| `kbmail git setup` / `kbmail git check [--staged]` | git init + pre-commit check / list problems (raw exports, tokens, secrets) before a commit |

## Getting mail in

| command | what it does |
|---|---|
| `kbmail export [--days N] [--include-subfolders] [--all-folders] [--folder F]... [--no-calendar] [--calendar-only] [--force]` | Outlook mail + calendar (Windows); delta by default |
| `kbmail import-eml <file-or-folder>...` | import saved .eml files |
| `kbmail teams export [--via powershell\|graph\|outlook] [--days N]` | Teams chats as chat-day records (powershell = scripts/teams-export.ps1, no app registration) |
| `kbmail teams import [json files/folders]` | import saved Graph JSON (default folder `data/teams-graph`) |
| `kbmail teams import-text <file.txt>... [--title T]` | import chats copied from the Teams window |
| `kbmail teams probe-outlook` | check whether Outlook exposes the Teams chat folder |

## Reading mail

| command | what it does |
|---|---|
| `kbmail show <message-or-thread-id> [--thread] [--full]` | print a message or whole thread (quoted history stripped unless `--full`) |
| `kbmail mail <query> [-n N] [--area A]` | BM25 search over exported mail |

## Labeling and rules

| command | what it does |
|---|---|
| `kbmail label prepare [--batch-size N] [--limit N]` | write `work/labeling/batch_NNN.md` for unlabeled mail |
| `kbmail label import` | validate and import `*.result.jsonl` |
| `kbmail label apply-rules` / `kbmail label counts` | apply rules to unlabeled mail / label counts by status |
| `kbmail review [--source proposed\|auto\|all] [--area A] [--order confidence\|date] [--accept-above X]` | interactive label confirmation (the user runs this) |
| `kbmail review export-csv` / `import-csv` | review in Excel via `review/labels_review.csv` |
| `kbmail rules prepare` | write `work/rules/RULES_CONTEXT.md` |
| `kbmail rules evaluate` | precision/recall of `config/rules.yaml` against confirmed labels |
| `kbmail rules commit [--note "..."]` | validate, snapshot to `state/rules_history/`, record the run, re-apply |
| `kbmail rules apply` / `kbmail rules status` | re-apply rules / is a refresh due? |

## Knowledge base

| command | what it does |
|---|---|
| `kbmail kb prepare [--batch-threads N]` | write `work/kb_select/batch_NNN.md` for threads needing assessment |
| `kbmail kb import` | import selection results (questions go to `review/questions.yaml`) |
| `kbmail kb convert [--force]` | markitdown conversion + images + redaction into `work/convert/` |
| `kbmail kb queue` / `kbmail kb status` | refresh `work/convert/QUEUE.md` / counts |
| `kbmail images status\|review\|describe\|import` | image descriptions workflow (`review/images/`) |
| `kbmail images svg [--force]` | auto-convert pending SVGs (simple diagrams → `.mmd`, icons skipped, complex → `<id>.svg.md` digest) |
| `kbmail svg2mermaid <file.svg> [-o out] [--digest]` | convert one SVG/.svgz; exit 0 = Mermaid, 3 = too complex (prints the digest) |
| `kbmail questions list [--all]` | open questions |
| `kbmail questions add "<question>" --context "<where>" --step <skill>` | record something you could not understand |
| `kbmail questions answer` / `kbmail questions ingest [--no-fetch]` | user answers / turn answers into `raw/notes/` (+ fetch links into `raw/web/`) |
| `kbmail raw import` | convert files dropped in `raw/inbox/` into `raw/docs/*.md` |

## Wiki

| command | what it does |
|---|---|
| `kbmail wiki queue` | new/changed (with diff)/deleted raw files → `work/wiki/QUEUE.md` |
| `kbmail wiki mark <raw path>... \| --all` | record raw files as ingested |
| `kbmail wiki lint` | structural issues + stale pages → `work/wiki/LINT.md` |
| `kbmail wiki search <query> [-n N] [--raw]` | hybrid BM25 + graph search |
| `kbmail wiki graph [--page people/x] [--depth N]` | build the graph / Mermaid neighbourhood of a page |
| `kbmail wiki status` | page and queue counts |

## People, triage, drafts

| command | what it does |
|---|---|
| `kbmail people build` | relationship statistics → `work/people/PEOPLE_DIGEST.md` |
| `kbmail triage [--days N]` | needs-reply / follow-up / meetings → `work/triage/TRIAGE.md` |
| `kbmail style [-n N]` | my sent mail samples → `work/style/SAMPLES.md` |
| `kbmail draft-outlook drafts/<file>.md` | save a draft into Outlook Drafts (never sends) |
