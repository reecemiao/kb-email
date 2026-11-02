"""Version a workspace with git / GitHub without leaking the mailbox.

`kbmail git setup`  - git init (if needed) and enable the workspace's .githooks/pre-commit
`kbmail git check`  - refuse raw mail exports, tokens, .env, extracted images, huge files and
                      text that still contains secrets; `--staged` checks what is about to be
                      committed (used by the pre-commit hook), otherwise everything git would track.

The .gitignore written by `kbmail init` already keeps these out; the check also catches files
added with `git add -f` and secrets that slipped into wiki/ or raw/ pages.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath

from kbmail.redact import redact
from kbmail.workspace import Workspace

MAX_BYTES = 5_000_000
TEXT_EXT = {".md", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".csv", ".mmd", ".html", ".htm", ".xml", ".svg", ".env"}
DENY: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^data/"), "raw mail/calendar/Teams export"),
    (re.compile(r"^work/"), "generated queue (re-created by `kbmail sync`)"),
    (re.compile(r"(^|/)\.env$"), "local settings (may contain ids or secrets); commit .env.example instead"),
    (re.compile(r"^state/msal_cache\.bin$"), "Microsoft sign-in token cache"),
    (
        re.compile(r"^state/(index\.jsonl|calendar_index\.jsonl|export_state\.json|teams_state\.json|people\.json)$"),
        "tied to the local mail export",
    ),
    (re.compile(r"^raw/inbox/"), "original documents (their Markdown in raw/docs/ is tracked)"),
    (
        re.compile(r"^review/images/.+\.(png|jpe?g|gif|bmp|tiff?|webp|emf|wmf|svgz?)$", re.I),
        "image extracted from mail (its description and .mmd are tracked)",
    ),
]


class GitError(RuntimeError):
    pass


def _git(ws: Workspace, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        res = subprocess.run(["git", "-C", str(ws.root), *args], capture_output=True)
    except FileNotFoundError as e:
        raise GitError("git is not installed or not on PATH") from e
    if check and res.returncode != 0:
        raise GitError(res.stderr.decode("utf-8", "replace").strip() or f"git {' '.join(args)} failed")
    return res


def is_repo(ws: Workspace) -> bool:
    try:
        return _git(ws, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == b"true"
    except GitError:
        return False


def setup(ws: Workspace, log=print) -> None:
    if not is_repo(ws):
        _git(ws, "init", "-b", "main")
        log(f"Initialized a git repository in {ws.root}")
    _git(ws, "config", "core.hooksPath", ".githooks")
    log("Enabled the kbmail pre-commit check (core.hooksPath = .githooks).")
    log(
        "\nNext steps:\n"
        "  1. Create a PRIVATE repository (ideally in your company's GitHub organization), e.g.\n"
        "       gh repo create <owner>/<name> --private      or on github.com: New repository -> Private\n"
        "  2. git remote add origin https://github.com/<owner>/<name>.git\n"
        "  3. kbmail git check                 # see what would be pushed, and any problems\n"
        '  4. git add -A && git commit -m "Knowledge base snapshot"\n'
        "  5. git push -u origin main\n"
        "Check your company's policy on storing mail-derived content on GitHub first."
    )


def _paths(ws: Workspace, staged: bool) -> list[str]:
    if staged:
        out = _git(ws, "diff", "--cached", "--name-only", "--relative", "--diff-filter=ACMR", "-z").stdout
    else:
        out = _git(ws, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def _content(ws: Workspace, path: str, staged: bool) -> bytes | None:
    if staged:
        res = _git(ws, "show", f":./{path}", check=False)
        return res.stdout if res.returncode == 0 else None
    p = ws.path(path)
    return p.read_bytes() if p.is_file() else None


def _secret_lines(text: str) -> list[int]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if redact(line)[1]:
            out.append(n)
    return out


def check(ws: Workspace, staged: bool = False, log=print) -> int:
    """Return 0 when everything is fine, 1 when something must not be committed."""
    if not is_repo(ws):
        log(f"{ws.root} is not a git repository. Run `kbmail git setup` first.")
        return 2
    paths = _paths(ws, staged)
    problems: list[str] = []
    for path in paths:
        reason = next((why for rx, why in DENY if rx.search(path)), None)
        if reason:
            problems.append(f"{path}: {reason}")
            continue
        data = _content(ws, path, staged)
        if data is None:
            continue
        if len(data) > MAX_BYTES:
            problems.append(
                f"{path}: {len(data) // 1_000_000} MB (over {MAX_BYTES // 1_000_000} MB; keep big files out)"
            )
            continue
        if PurePosixPath(path).suffix.lower() in TEXT_EXT or path.endswith(".env.example"):
            lines = _secret_lines(data.decode("utf-8", "replace"))
            if lines:
                where = ", ".join(map(str, lines[:5])) + (" ..." if len(lines) > 5 else "")
                problems.append(f"{path}: looks like a password/token/key on line(s) {where}")
    scope = "staged file(s)" if staged else "file(s) git would track"
    if problems:
        log(f"kbmail git check: {len(problems)} problem(s) in {len(paths)} {scope}:")
        for p in problems:
            log(f"  - {p}")
        log(
            "Fix: unstage/remove the file (`git rm --cached <file>`), or edit the text "
            "(replace the secret). Bypass once only if you are sure: git commit --no-verify"
        )
        return 1
    log(f"kbmail git check: OK ({len(paths)} {scope}).")
    return 0
