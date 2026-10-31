"""kbmail command line. Run `kbmail -h` or `kbmail <command> -h`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kbmail.lock import LockBusy, workspace_lock
from kbmail.workspace import Workspace, WorkspaceError, init_workspace


def _ws(args) -> Workspace:
    ws = Workspace.find(args.workspace)
    ws.config  # noqa: B018 - load now so .env problems surface before any work starts
    for w in ws.config_warnings:
        print(f"warning: {w}", file=sys.stderr)
    return ws


# ---------------------------------------------------------------- handlers


def cmd_init(args):
    written = init_workspace(Path(args.path), upgrade=args.upgrade)
    print(
        f"{'Upgraded' if args.upgrade else 'Initialized'} workspace {Path(args.path).resolve()} "
        f"({len(written)} file(s) written)."
    )
    if not args.upgrade:
        print(
            "Next: edit kbmail.yaml (me.emails, areas), open the folder in VS Code, select the KB Librarian "
            "agent and type /kb-start."
        )


def cmd_status(args):
    from kbmail.status import collect, print_status

    ws = _ws(args)
    if args.json:
        print(json.dumps(collect(ws), ensure_ascii=False, indent=1, default=str))
    else:
        print_status(ws)


def cmd_export(args):
    from kbmail import outlook

    ws = _ws(args)
    if not args.calendar_only:
        outlook.export_mail(
            ws,
            days=args.days,
            include_subfolders=args.include_subfolders,
            all_folders=ws.opt(args.all_folders, "outlook.all_folders"),
            folders=args.folder or None,
            force=args.force,
        )
    if args.calendar_only or ws.opt(args.calendar, "outlook.calendar"):
        outlook.export_calendar(ws, days=args.days)


def cmd_import_eml(args):
    from kbmail.importers import import_eml

    import_eml(_ws(args), args.paths, force=args.force)


def cmd_sync(args):
    """Delta run of every deterministic step; prints what Copilot should do next."""
    from kbmail import assist, kb, people, rules, wiki
    from kbmail.labels import prepare_batches
    from kbmail.status import print_status

    ws = _ws(args)
    # Steps that talk to Outlook, Teams or the directory may fail (Outlook closed, token expired, VPN
    # down). The rest still runs on what is already exported, but the failure is recorded in
    # runs.json, shown by `kbmail status` / the Copilot session hook, and makes the exit code 1, so a
    # scheduled task doesn't keep "succeeding" for weeks without new mail.
    problems: list[dict] = []

    def attempt(step: str, fn) -> None:
        try:
            fn()
        except Exception as e:
            from kbmail.outlook import OutlookUnavailable

            if isinstance(e, OutlookUnavailable):  # not Windows / no pywin32: import-eml workspaces
                print(f"({step} skipped: {e})")
                return
            print(f"{step} FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            problems.append({"step": step, "error": f"{type(e).__name__}: {e}"[:300]})

    if ws.opt(args.export, "sync.export"):

        def export():
            from kbmail import outlook

            outlook.export_mail(ws)
            if ws.cfg("outlook.calendar"):
                outlook.export_calendar(ws)

        attempt("export", export)
    if ws.opt(args.teams, "sync.teams"):

        def teams_export():
            from kbmail import teams

            teams.export(ws, ws.cfg("teams.via"))

        attempt("teams export", teams_export)
    rules.apply_rules(ws)
    prepare_batches(ws)
    kb.prepare_select(ws)
    kb.convert(ws)
    wiki.raw_import(ws)
    wiki.write_queue(ws)
    if ws.cfg("directory.enabled"):

        def directory_sync():  # never prompts here: needs directory.auth kerberos or KBMAIL_DIRECTORY_PASSWORD
            from kbmail import directory

            people.build(ws, log=lambda *a: None)  # fresh contact list to look up
            directory.sync(ws, interactive=False)

        attempt("directory lookup", directory_sync)
    people.build(ws)
    assist.triage(ws)
    ws.record_run("sync", problems=problems)
    print()
    print_status(ws)
    return 1 if problems else 0


def cmd_show(args):
    from kbmail.assist import show

    print(show(_ws(args), args.id, thread=args.thread, full=args.full))


def cmd_history(args):
    from kbmail import history

    ws = _ws(args)
    if args.action == "show":
        if not args.id:
            print("usage: kbmail history show <message-id>", file=sys.stderr)
            return 1
        try:
            print(history.explain(ws, args.id))
        except KeyError:
            print(f"unknown message id: {args.id}", file=sys.stderr)
            return 1
    else:
        st = history.status(ws)
        print(
            f"{st['with_history']} of {st['messages']} message(s) quote older mail: {st['quoted']} quoted message(s), "
            f"{st['already_exported']} already exported (shown as a pointer), {st['kept']} not exported (kept).\n"
            f"{st['messages_quoted_elsewhere']} exported message(s) also appear quoted inside later mail."
        )
    return 0


def cmd_mail(args):
    from kbmail.assist import mail_search

    ws = _ws(args)
    for r in mail_search(ws, " ".join(args.query), ws.opt(args.limit, "mail.search_limit"), args.area):
        print(f"{r['id']}  {r['date']}  {r['score']:>6}  {r['from'][:30]:30}  {r['subject']}  (thread {r['thread']})")


def cmd_label(args):
    from kbmail import labels, rules

    ws = _ws(args)
    if args.action == "prepare":
        labels.prepare_batches(ws, args.batch_size, ws.opt(args.limit, "labeling.limit"))
    elif args.action == "import":
        labels.import_results(ws)
    elif args.action == "apply-rules":
        rules.apply_rules(ws)
    elif args.action == "counts":
        print(labels.label_counts(ws))


def cmd_review(args):
    from kbmail import labels

    ws = _ws(args)
    source = ws.opt(args.source, "review.source")
    if args.action == "export-csv":
        labels.export_csv(ws, source)
    elif args.action == "import-csv":
        labels.import_csv(ws)
    else:
        labels.interactive_review(
            ws, source, args.area, ws.opt(args.order, "review.order"), ws.opt(args.accept_above, "review.accept_above")
        )


def cmd_rules(args):
    from kbmail import rules

    ws = _ws(args)
    if args.action == "prepare":
        rules.prepare_context(ws)
    elif args.action == "evaluate":
        rules.print_eval(ws)
    elif args.action == "commit":
        if not rules.commit(ws, note=args.note or ""):
            return 1
        rules.apply_rules(ws)
    elif args.action == "apply":
        rules.apply_rules(ws)
    elif args.action == "status":
        print(rules.due(ws)[1])


def cmd_kb(args):
    from kbmail import kb

    ws = _ws(args)
    {
        "prepare": lambda: kb.prepare_select(ws, ws.opt(args.batch_threads, "kb.select_batch_threads")),
        "import": lambda: kb.import_select(ws),
        "convert": lambda: kb.convert(ws, force=args.force),
        "queue": lambda: kb.write_queue(ws),
        "status": lambda: print(kb.status(ws)),
    }[args.action]()


def cmd_images(args):
    from kbmail import images

    ws = _ws(args)
    if args.action == "import":
        images.import_descriptions(ws)
        from kbmail.kb import write_queue

        write_queue(ws)
    elif args.action == "describe":
        images.interactive_describe(ws)
        from kbmail.kb import write_queue

        write_queue(ws)
    elif args.action == "review":
        print(images.write_review_files(ws, images.load(ws)))
    elif args.action == "svg":
        reg = images.load(ws)
        images.process_svgs(ws, reg, force=args.force)
        images.save(ws, reg)
        images.write_review_files(ws, reg)
        from kbmail.kb import write_queue

        write_queue(ws)
    else:
        print(images.counts(ws))


def cmd_svg2mermaid(args):
    """Exit code 0 = converted, 3 = too complex (digest written instead)."""
    from kbmail.svg2mermaid import convert

    path = Path(args.file)
    res = convert(path.read_bytes(), path.name)
    text = res.digest if (args.digest or not res.is_simple) else res.mermaid + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    print(f"{res.kind}: {res.summary}", file=sys.stderr)
    return 0 if res.is_simple else 3


def cmd_questions(args):
    from kbmail import questions

    ws = _ws(args)
    if args.action == "add":
        print(questions.add_question(ws, " ".join(args.text), context=args.context or "", step=args.step))
    elif args.action == "answer":
        questions.interactive_answer(ws)
    elif args.action == "ingest":
        questions.ingest(ws, fetch=ws.opt(args.fetch, "questions.fetch_links"))
    else:
        for q in questions.load(ws):
            if args.all or q.get("status") == "open":
                print(f"{q['id']} [{q.get('status')}] {q['question']}  ({q.get('context', '')})")


def cmd_raw(args):
    from kbmail import wiki

    wiki.raw_import(_ws(args))


def cmd_wiki(args):
    from kbmail import wiki

    ws = _ws(args)
    if args.action == "queue":
        wiki.write_queue(ws)
    elif args.action == "mark":
        wiki.mark(ws, args.paths, all_queued=args.all)
    elif args.action == "lint":
        wiki.lint(ws)
    elif args.action == "search":
        limit, raw = ws.opt(args.limit, "wiki.search_limit"), ws.opt(args.raw, "wiki.search_raw")
        for r in wiki.search(ws, " ".join(args.query), limit, include_raw=raw):
            conf = f" conf={r['confidence']}" if r.get("confidence") is not None else ""
            print(f"{r['score']:.4f}  {r['path']}  [{r['kind']}{conf}]  {r['title']}")
    elif args.action == "graph":
        g = wiki.build_graph(ws)
        if args.page:
            print(wiki.graph_mermaid(g, args.page, ws.opt(args.depth, "wiki.graph_depth")))
        else:
            print(f"{len(g['nodes'])} node(s), {len(g['edges'])} edge(s) -> state/wiki_graph.json")
    elif args.action == "status":
        print(wiki.status(ws))


def cmd_directory(args):
    from kbmail import directory, people

    try:
        ws = Workspace.find(args.workspace)
    except WorkspaceError:
        ws = None  # lookup/test/init also work standalone with --config or ~/.kbmail/directory.yaml
    try:
        if args.action == "init":
            return _directory_init(ws, args.config)
        if args.action == "sync":
            if ws is None:
                raise directory.DirectoryError("sync needs a kbmail workspace (it feeds the people digest)")
            people.build(ws, log=lambda *a: None)
            directory.sync(ws, limit=args.limit, refresh=args.refresh)
            people.build(ws)
            return 0
        cfg = directory.load_config(args.config, ws)
        conn = directory.connect(cfg)
        try:
            d = directory.Directory(cfg, conn)
            if args.action == "test":
                print(f"Bound to {cfg['server']} as {conn.extend.standard.who_am_i() or '(unknown)'}")
                emails = sorted(ws.me_emails)[:1] if ws else []
            else:
                emails = args.email
                if not emails:
                    print("give an e-mail address", file=sys.stderr)
                    return 2
            for email in emails:
                print(directory.as_json(d.card(email, args.view)))
        finally:
            conn.unbind()
    except directory.DirectoryError as e:
        print(f"directory: {e}", file=sys.stderr)
        return 1
    return 0


def _directory_init(ws, target: str | None) -> int:
    import shutil

    from kbmail.workspace import DIRECTORY_FILE, TEMPLATE_DIR

    example = TEMPLATE_DIR / "config" / "directory.example.yaml"
    if target:
        dst = Path(target)
    elif ws is not None:
        dst = ws.path(DIRECTORY_FILE)
    else:
        from kbmail.directory import user_config_path

        dst = user_config_path()
    if dst.exists():
        print(f"{dst} already exists; edit it (template: {example})")
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, dst)
    print(f"Created {dst}. Fill in server, search_base, ca_certs_file and your schema, then `kbmail directory test`.")
    return 0


def cmd_people(args):
    from kbmail import people

    people.build(_ws(args))


def cmd_triage(args):
    from kbmail import assist

    assist.triage(_ws(args), args.days)


def cmd_style(args):
    from kbmail import assist

    ws = _ws(args)
    assist.style_samples(ws, ws.opt(args.n, "style.samples"))


def cmd_draft_outlook(args):
    from kbmail import outlook

    outlook.create_outlook_draft(_ws(args), args.path)


def cmd_teams(args):
    from kbmail import teams

    ws = _ws(args)
    if args.action == "probe-outlook":
        teams.probe_outlook(ws)
    elif args.action == "import":
        teams.import_graph_json(ws, args.paths or None)
    elif args.action == "import-text":
        if not args.paths:
            print("give the text file(s) you pasted the chat into", file=sys.stderr)
            return 2
        teams.import_text(ws, args.paths, title=args.title)
    else:
        try:
            teams.export(ws, ws.opt(args.via, "teams.via"), args.days)
        except RuntimeError as e:
            print(f"teams: {e}", file=sys.stderr)
            return 1


def cmd_hook(args):
    """Agent hooks (.github/hooks/kbmail.json). Never fails: hooks must not break a session."""
    from kbmail.status import hook_context

    payload = {}
    try:
        if not sys.stdin.isatty():
            payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        payload = {}
    out = {}
    try:
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        ws = Workspace.find(args.workspace or cwd or None)
        out = {"additionalContext": hook_context(ws)}
    except Exception as e:  # not a kbmail workspace, broken config, ...: add a hint, never an error
        out = {"additionalContext": f"kbmail: status unavailable ({e}). Run `kbmail status` to check."}
    print(json.dumps(out, ensure_ascii=False))


def cmd_git(args):
    from kbmail import gitops

    ws = _ws(args)
    try:
        if args.action == "setup":
            gitops.setup(ws)
            return 0
        return gitops.check(ws, staged=args.staged)
    except gitops.GitError as e:
        print(f"git: {e}", file=sys.stderr)
        return 2


def cmd_config(args):
    from kbmail.workspace import env_example, env_name

    if args.action == "env":
        print(env_example(), end="")
        return
    ws = _ws(args)
    cfg = ws.config
    for key, src in ws.config_sources.items():
        cur = cfg
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if args.key and not key.startswith(args.key):
            continue
        print(f"{key} = {json.dumps(cur, ensure_ascii=False)}    [{src}; {env_name(key)}]")


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    toggle = argparse.BooleanOptionalAction  # --x / --no-x; None when absent -> configured default
    p = argparse.ArgumentParser(prog="kbmail", description=__doc__)
    p.add_argument(
        "-w", "--workspace", help="workspace folder (default: $KBMAIL_WORKSPACE, else search upward for kbmail.yaml)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create (or --upgrade) a workspace folder")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--upgrade", action="store_true", help="refresh prompt files/schema, keep your config and data")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("status", help="what is done, what is due next")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("export", help="export Outlook mail + calendar (Windows, pywin32); delta by default")
    s.add_argument("--days", type=int, help="look back N days (default: since last export, or outlook.days)")
    g = s.add_mutually_exclusive_group()
    g.add_argument(
        "--include-subfolders",
        dest="include_subfolders",
        action="store_true",
        default=None,
        help="also export subfolders (default: skip them)",
    )
    g.add_argument("--skip-subfolders", dest="include_subfolders", action="store_false")
    s.add_argument(
        "--all-folders",
        action=toggle,
        help="all mail folders of the default store (default: outlook.all_folders = false, Inbox + Sent Items)",
    )
    s.add_argument(
        "--folder",
        action="append",
        help="folder to export instead of the configured ones "
        "(inbox, sent, or a path like 'Inbox/Projects'); repeatable",
    )
    s.add_argument("--calendar", action=toggle, help="export the calendar too (default: outlook.calendar)")
    s.add_argument("--calendar-only", action="store_true")
    s.add_argument("--force", action="store_true", help="re-export messages that are already in the index")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("import-eml", help="import .eml files or folders (any OS)")
    s.add_argument("paths", nargs="+")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_import_eml)

    s = sub.add_parser("sync", help="delta run: export, apply rules, prepare all Copilot queues, status")
    s.add_argument("--export", action=toggle, help="export from Outlook first (default: sync.export)")
    s.add_argument("--skip-export", dest="export", action="store_false", help="same as --no-export")
    s.add_argument("--teams", action=toggle, help="also export Teams chats (default: sync.teams)")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("show", help="print a message (or its whole thread)")
    s.add_argument("id")
    s.add_argument("--thread", action="store_true")
    s.add_argument("--full", action="store_true", help="the raw body, with all quoted history")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("history", help="quoted history in replies/forwards: what is already exported, what is kept")
    s.add_argument("action", choices=["status", "show"], nargs="?", default="status")
    s.add_argument("id", nargs="?", help="message id (for show)")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("mail", help="search exported mail (BM25)")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", "--limit", type=int, help="number of results (default: mail.search_limit)")
    s.add_argument("--area")
    s.set_defaults(func=cmd_mail)

    s = sub.add_parser("label", help="labeling batches for Copilot")
    s.add_argument("action", choices=["prepare", "import", "apply-rules", "counts"])
    s.add_argument("--batch-size", type=int, help="messages per batch (default: labeling.batch_size)")
    s.add_argument("--limit", type=int, help="max messages to queue (default: labeling.limit)")
    s.set_defaults(func=cmd_label)

    s = sub.add_parser("review", help="confirm labels interactively (default) or via CSV")
    s.add_argument("action", nargs="?", default="interactive", choices=["interactive", "export-csv", "import-csv"])
    s.add_argument(
        "--source",
        choices=["proposed", "auto", "all"],
        help="proposed = Copilot suggestions, auto = rule labels (spot-check) (default: review.source)",
    )
    s.add_argument("--area")
    s.add_argument("--order", choices=["confidence", "date"], help="(default: review.order)")
    s.add_argument(
        "--accept-above",
        type=float,
        help="bulk-confirm proposals with confidence >= X first (default: review.accept_above)",
    )
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("rules", help="rule refresh cycle")
    s.add_argument("action", choices=["prepare", "evaluate", "commit", "apply", "status"])
    s.add_argument("--note")
    s.set_defaults(func=cmd_rules)

    s = sub.add_parser("kb", help="knowledge-base selection and conversion")
    s.add_argument("action", choices=["prepare", "import", "convert", "queue", "status"])
    s.add_argument("--batch-threads", type=int, help="threads per batch (default: kb.select_batch_threads)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_kb)

    s = sub.add_parser("images", help="image descriptions (vision-off workflow)")
    s.add_argument("action", choices=["status", "review", "describe", "import", "svg"])
    s.add_argument("--force", action="store_true", help="(svg) redo automatic SVG results too")
    s.set_defaults(func=cmd_images)

    s = sub.add_parser("svg2mermaid", help="convert one SVG (or .svgz) to Mermaid, or print a digest for Copilot")
    s.add_argument("file")
    s.add_argument("-o", "--output", help="write the Mermaid (or, if complex, the digest) here instead of stdout")
    s.add_argument("--digest", action="store_true", help="print the text digest even when conversion succeeds")
    s.set_defaults(func=cmd_svg2mermaid)

    s = sub.add_parser("questions", help="questions for the user (unknown terms, missing links)")
    s.add_argument("action", choices=["list", "add", "answer", "ingest"])
    s.add_argument("text", nargs="*")
    s.add_argument("--context")
    s.add_argument("--step", default="manual")
    s.add_argument("--all", action="store_true")
    s.add_argument(
        "--fetch",
        action=toggle,
        help="fetch URLs found in answers into raw/web (default: questions.fetch_links)",
    )
    s.set_defaults(func=cmd_questions)

    s = sub.add_parser("raw", help="convert files dropped in raw/inbox into raw/docs/*.md")
    s.add_argument("action", choices=["import"])
    s.set_defaults(func=cmd_raw)

    s = sub.add_parser("wiki", help="wiki bookkeeping: delta queue, mark, lint, search, graph")
    s.add_argument("action", choices=["queue", "mark", "lint", "search", "graph", "status"])
    s.add_argument("paths", nargs="*", help="(mark) raw files that were ingested")
    s.add_argument("--all", action="store_true", help="(mark) everything in the queue")
    s.add_argument("--query", "-q", nargs="+", dest="query", help="(search) query text")
    s.add_argument("-n", "--limit", type=int, help="(search) number of results (default: wiki.search_limit)")
    s.add_argument("--raw", action=toggle, help="(search) include raw/ sources (default: wiki.search_raw)")
    s.add_argument("--page", help="(graph) page key, e.g. people/sarah-tanaka")
    s.add_argument("--depth", type=int, help="(graph) neighbourhood depth (default: wiki.graph_depth)")
    s.set_defaults(func=cmd_wiki)

    s = sub.add_parser("people", help="relationship statistics -> work/people/PEOPLE_DIGEST.md")
    s.add_argument("action", choices=["build"], nargs="?", default="build")
    s.set_defaults(func=cmd_people)

    s = sub.add_parser("directory", help="corporate directory (LDAP/AD): init | test | lookup <email> | sync people")
    s.add_argument("action", choices=["init", "test", "lookup", "sync"])
    s.add_argument("email", nargs="*", help="(lookup) e-mail address(es)")
    s.add_argument(
        "--config",
        help="directory settings file (default: workspace config/directory.yaml, "
        "$KBMAIL_DIRECTORY_CONFIG or ~/.kbmail/directory.yaml)",
    )
    s.add_argument("--view", help="(lookup/test) output view from `views` (record = raw fields/relations)")
    s.add_argument("--limit", type=int, help="(sync) max lookups this run (default: directory.max_lookups)")
    s.add_argument("--refresh", action="store_true", help="(sync) ignore the cache")
    s.set_defaults(func=cmd_directory)

    s = sub.add_parser("triage", help="what needs a reply / follow-up -> work/triage/TRIAGE.md")
    s.add_argument("--days", type=int, help="look-back window (default: triage.days)")
    s.set_defaults(func=cmd_triage)

    s = sub.add_parser("style", help="collect my sent mail as writing samples -> work/style/SAMPLES.md")
    s.add_argument("-n", type=int, help="number of samples (default: style.samples)")
    s.set_defaults(func=cmd_style)

    s = sub.add_parser("draft-outlook", help="save a drafts/*.md file as an Outlook draft (never sends)")
    s.add_argument("path")
    s.set_defaults(func=cmd_draft_outlook)

    s = sub.add_parser("teams", help="Teams chats: export (powershell/graph/outlook), import saved JSON or pasted text")
    s.add_argument("action", choices=["export", "import", "import-text", "probe-outlook"])
    s.add_argument(
        "paths", nargs="*", help="(import) JSON files/folders, default teams.import_dir; (import-text) text files"
    )
    s.add_argument("--via", choices=["powershell", "graph", "outlook"], help="(export) route (default: teams.via)")
    s.add_argument("--title", help="(import-text) chat title (default: the file name)")
    s.add_argument("--days", type=int, help="look-back window (default: teams.days, or since last export)")
    s.set_defaults(func=cmd_teams)

    s = sub.add_parser("git", help="version the workspace safely: setup (init + pre-commit check) / check")
    s.add_argument("action", choices=["setup", "check"])
    s.add_argument("--staged", action="store_true", help="(check) only what is staged for commit")
    s.set_defaults(func=cmd_git)

    s = sub.add_parser("hook", help="agent hook entry points (used by .github/hooks/kbmail.json)")
    s.add_argument("event", choices=["session-start"])
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("config", help="show effective settings and where they come from; print a .env template")
    s.add_argument("action", choices=["show", "env"], nargs="?", default="show")
    s.add_argument("key", nargs="?", help="(show) only keys starting with this, e.g. outlook")
    s.set_defaults(func=cmd_config)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows consoles + Japanese text
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    # argparse consumes a trailing `nargs="*"` positional (as empty) before options such as --config,
    # so words after the options arrive here. Hand them to the command's list positional.
    target = next((n for n in ("email", "paths", "text") if isinstance(getattr(args, n, None), list)), None)
    if extra and target and not any(x.startswith("-") for x in extra):
        getattr(args, target).extend(extra)
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if args.cmd == "wiki" and args.action == "search" and not args.query:
        args.query = args.paths
    try:
        if _needs_lock(args):
            ws = Workspace.find(args.workspace)
            with workspace_lock(ws, " ".join(_command_words(args))):
                rc = args.func(args)
        else:
            rc = args.func(args)
    except WorkspaceError as e:
        print(e, file=sys.stderr)
        return 2
    except LockBusy as e:
        print(e, file=sys.stderr)
        return 3
    return int(rc or 0)


# Commands that change no state (a rebuilt cache is fine to lose), so they never wait for the lock.
# `review` (interactive) is here too: a review session can last an hour, so instead of holding the
# lock throughout it saves each answer under the lock, merged into the current labels.
_NO_LOCK = {"init", "status", "show", "history", "mail", "svg2mermaid", "hook", "config", "git"}
_NO_LOCK_ACTIONS = {
    ("review", "interactive"),
    ("label", "counts"),
    ("rules", "evaluate"),
    ("rules", "status"),
    ("kb", "status"),
    ("images", "status"),
    ("questions", "list"),
    ("wiki", "search"),
    ("wiki", "graph"),
    ("wiki", "status"),
}


def _command_words(args) -> list[str]:
    action = getattr(args, "action", None)
    return [args.cmd, action] if isinstance(action, str) else [args.cmd]


def _needs_lock(args) -> bool:
    words = _command_words(args)
    return words[0] not in _NO_LOCK and tuple(words) not in _NO_LOCK_ACTIONS


if __name__ == "__main__":
    raise SystemExit(main())
