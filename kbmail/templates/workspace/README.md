# kbmail workspace

A private knowledge base compiled from your Outlook mail, calendar and Teams chats by
[kbmail](https://github.com/reecemiao/kb-email) and GitHub Copilot. Open this folder in VS Code,
pick the **KB Librarian** agent and type `/kb-sync` (first time: `/kb-start`).
`kbmail status` always says what is due next.

This file is refreshed by `kbmail init --upgrade`.

## Folders

| folder | what | in git? |
|---|---|---|
| `wiki/` | the knowledge base (pages, `index.md`, `log.md`, `SCHEMA.md`) | yes |
| `raw/` | formatted sources the wiki is compiled from (`email/`, `teams/`, `notes/`, `web/`, `docs/`) | yes (except `raw/inbox/`) |
| `config/` | labeling rules, rules journal, people overrides | yes |
| `kbmail.yaml`, `.env.example` | settings (`.env` holds your local overrides) | yes (`.env`: no) |
| `AGENTS.md`, `.github/`, `.vscode/`, `.githooks/` | Copilot instructions, skills, agent, hook, editor settings, git hook | yes |
| `review/` | your to-do area: questions, image descriptions, Mermaid conversions | answers, descriptions and `.mmd` only |
| `state/` | kbmail bookkeeping | only what moves with you (see below) |
| `data/` | raw export of mail, attachments, calendar, Teams | **no** |
| `work/` | generated queues for Copilot | no |
| `drafts/` | email drafts | no (remove `drafts/` from `.gitignore` to version them) |

## Version control with GitHub

Git gives the wiki a full history: every ingest, supersession and lint fix is a reviewable diff.
It also gives you a backup, and a way to move to another machine.

**Use a private repository**, ideally in your company's GitHub organization, and check your
company's policy on storing mail-derived content first. `raw/` and `wiki/` contain business
content even after redaction. kbmail's redaction catches common secret patterns, not everything.

### What is pushed

`.gitignore` (written by `kbmail init`) tracks what you and Copilot authored, plus the bookkeeping
that lets delta runs continue on another machine:

- the knowledge: `wiki/`, `raw/` (except `raw/inbox/`), `review/questions.yaml`,
  `review/images/descriptions.yaml`, `review/images/*.mmd`
- the setup: `kbmail.yaml`, `.env.example`, `config/`, `AGENTS.md`, `.github/`, `.vscode/`,
  `.githooks/`, `.gitignore`, `.gitattributes`
- the decisions and delta state:
  - `state/labels.json`: your confirmed labels, the ground truth for rules
  - `state/kb_selection.json`, `state/kb_convert.json`: which threads feed the KB and where
  - `state/images.json`: image descriptions
  - `state/wiki_manifest.json`, `state/wiki_snapshots/`, `state/wiki_audit.jsonl`: which raw files
    the wiki already contains, and the base for diffs
  - `state/rules_history/`, `state/runs.json`: rule versions and the last run of each step

Ids are derived from the mail itself (Internet Message-ID, conversation, content hash), so these
files line up again after a fresh export on another machine.

Never pushed: `data/` (the raw mail export), `work/`, `.env`, `state/msal_cache.bin` (Teams sign-in
token), `state/index.jsonl` and the other export-bound state, `raw/inbox/` originals, and image
files extracted from mail.

The `.gitignore` and `.gitattributes` content between the `kbmail managed` markers is refreshed
on upgrade. Put your own rules outside the markers. To keep the formatted source threads out of
the repo as well (wiki-only mode), uncomment `raw/email/` and `raw/teams/` in `.gitignore`.

### First push

```powershell
kbmail git setup                    # git init + enable the pre-commit check (.githooks)
gh repo create <owner>/<name> --private --source . --remote origin   # or create it on github.com
kbmail git check                    # lists anything that must not be pushed
git add -A
git commit -m "Knowledge base snapshot"
git push -u origin main
```

The pre-commit hook runs `kbmail git check --staged`. It blocks commits that contain raw exports,
tokens, `.env`, extracted images, files over 5 MB, or text that still looks like a password, API
key or token. If it is a false positive, edit the text, or bypass once with
`git commit --no-verify`.

### Routine

Commit after each working session, typically after `/kb-wiki-ingest` or `/kb-wiki-lint`:

```powershell
git add -A
git commit -m "Ingest 2026-10 mail: payments cut-over, infra patching"
git push
```

Review `git diff wiki/` before committing to see exactly what Copilot changed in the knowledge base.

### Another machine, or restoring from GitHub

```powershell
git clone https://github.com/<owner>/<name>.git C:\kb
cd C:\kb
git config core.hooksPath .githooks
kbmail init . --upgrade             # recreates local folders and .env from the template
kbmail export --days 30             # re-export mail; labels, KB choices and image descriptions re-attach
kbmail kb convert                   # rebuilds work files for threads that were not formatted yet
kbmail status
```

Work from one machine at a time, and pull before you start. The state files under `state/` are
JSON written by kbmail, so changes made in parallel on two machines conflict.
