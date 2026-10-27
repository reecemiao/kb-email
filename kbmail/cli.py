"""kbmail command line. Run `kbmail -h` or `kbmail <command> -h`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kbmail.workspace import Workspace, WorkspaceError, init_workspace


def _ws(args) -> Workspace:
    return Workspace.find(args.workspace)


# ---------------------------------------------------------------- handlers


def cmd_init(args):
    written = init_workspace(Path(args.path), upgrade=args.upgrade)
    print(
        f"{'Upgraded' if args.upgrade else 'Initialized'} workspace {Path(args.path).resolve()} "
        f"({len(written)} file(s) written)."
    )
    if not args.upgrade:
        print(
            "Next: edit kbmail.yaml (me.emails, areas), open the folder in VS Code, and run `kbmail export --days 30`."
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
            all_folders=args.all_folders,
            folders=args.folder or None,
            force=args.force,
        )
    if args.calendar_only or (ws.cfg("outlook.calendar") and not args.no_calendar):
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
    if not args.skip_export:
        try:
            from kbmail import outlook

            outlook.export_mail(ws)
            if ws.cfg("outlook.calendar"):
                outlook.export_calendar(ws)
        except Exception as e:  # keep going: the rest works on what is already exported
            print(f"(export skipped: {e})")
        if args.teams:
            from kbmail import teams

            try:
                teams.export_graph(ws)
            except Exception as e:
                print(f"(teams export skipped: {e})")
    rules.apply_rules(ws)
    prepare_batches(ws)
    kb.prepare_select(ws)
    kb.convert(ws)
    wiki.raw_import(ws)
    wiki.write_queue(ws)
    people.build(ws)
    assist.triage(ws)
    ws.record_run("sync")
    print()
    print_status(ws)


def cmd_show(args):
    from kbmail.assist import show

    print(show(_ws(args), args.id, thread=args.thread, full=args.full))


def cmd_mail(args):
    from kbmail.assist import mail_search

    for r in mail_search(_ws(args), " ".join(args.query), args.limit, args.area):
        print(f"{r['id']}  {r['date']}  {r['score']:>6}  {r['from'][:30]:30}  {r['subject']}  (thread {r['thread']})")


def cmd_label(args):
    from kbmail import labels, rules

    ws = _ws(args)
    if args.action == "prepare":
        labels.prepare_batches(ws, args.batch_size, args.limit)
    elif args.action == "import":
        labels.import_results(ws)
    elif args.action == "apply-rules":
        rules.apply_rules(ws)
    elif args.action == "counts":
        print(labels.label_counts(ws))


def cmd_review(args):
    from kbmail import labels

    ws = _ws(args)
    if args.action == "export-csv":
        labels.export_csv(ws, args.source)
    elif args.action == "import-csv":
        labels.import_csv(ws)
    else:
        labels.interactive_review(ws, args.source, args.area, args.order, args.accept_above)


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
        "prepare": lambda: kb.prepare_select(ws, args.batch_threads),
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
    else:
        print(images.counts(ws))


def cmd_questions(args):
    from kbmail import questions

    ws = _ws(args)
    if args.action == "add":
        print(questions.add_question(ws, " ".join(args.text), context=args.context or "", step=args.step))
    elif args.action == "answer":
        questions.interactive_answer(ws)
    elif args.action == "ingest":
        questions.ingest(ws, fetch=not args.no_fetch)
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
        for r in wiki.search(ws, " ".join(args.query), args.limit, include_raw=args.raw):
            conf = f" conf={r['confidence']}" if r.get("confidence") is not None else ""
            print(f"{r['score']:.4f}  {r['path']}  [{r['kind']}{conf}]  {r['title']}")
    elif args.action == "graph":
        g = wiki.build_graph(ws)
        if args.page:
            print(wiki.graph_mermaid(g, args.page, args.depth))
        else:
            print(f"{len(g['nodes'])} node(s), {len(g['edges'])} edge(s) -> state/wiki_graph.json")
    elif args.action == "status":
        print(wiki.status(ws))


def cmd_people(args):
    from kbmail import people

    people.build(_ws(args))


def cmd_triage(args):
    from kbmail import assist

    assist.triage(_ws(args), args.days)


def cmd_style(args):
    from kbmail import assist

    assist.style_samples(_ws(args), args.n)


def cmd_draft_outlook(args):
    from kbmail import outlook

    outlook.create_outlook_draft(_ws(args), args.path)


def cmd_teams(args):
    from kbmail import teams

    ws = _ws(args)
    if args.action == "probe-outlook":
        teams.probe_outlook(ws)
    elif args.via == "outlook":
        teams.export_outlook(ws, args.days)
    else:
        teams.export_graph(ws, args.days)


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kbmail", description=__doc__)
    p.add_argument("-w", "--workspace", help="workspace folder (default: search upward for kbmail.yaml)")
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
        action="store_true",
        help="all mail folders of the default store (default: Inbox + Sent Items only)",
    )
    s.add_argument(
        "--folder",
        action="append",
        help="folder to export instead of the configured ones "
        "(inbox, sent, or a path like 'Inbox/Projects'); repeatable",
    )
    s.add_argument("--no-calendar", action="store_true")
    s.add_argument("--calendar-only", action="store_true")
    s.add_argument("--force", action="store_true", help="re-export messages that are already in the index")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("import-eml", help="import .eml files or folders (any OS)")
    s.add_argument("paths", nargs="+")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_import_eml)

    s = sub.add_parser("sync", help="delta run: export, apply rules, prepare all Copilot queues, status")
    s.add_argument("--skip-export", action="store_true")
    s.add_argument("--teams", action="store_true", help="also export Teams chats via Graph")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("show", help="print a message (or its whole thread)")
    s.add_argument("id")
    s.add_argument("--thread", action="store_true")
    s.add_argument("--full", action="store_true", help="keep quoted history")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("mail", help="search exported mail (BM25)")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", "--limit", type=int, default=15)
    s.add_argument("--area")
    s.set_defaults(func=cmd_mail)

    s = sub.add_parser("label", help="labeling batches for Copilot")
    s.add_argument("action", choices=["prepare", "import", "apply-rules", "counts"])
    s.add_argument("--batch-size", type=int)
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_label)

    s = sub.add_parser("review", help="confirm labels interactively (default) or via CSV")
    s.add_argument("action", nargs="?", default="interactive", choices=["interactive", "export-csv", "import-csv"])
    s.add_argument(
        "--source",
        default="proposed",
        choices=["proposed", "auto", "all"],
        help="proposed = Copilot suggestions, auto = rule labels (spot-check)",
    )
    s.add_argument("--area")
    s.add_argument("--order", default="confidence", choices=["confidence", "date"])
    s.add_argument("--accept-above", type=float, help="bulk-confirm proposals with confidence >= X first")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("rules", help="rule refresh cycle")
    s.add_argument("action", choices=["prepare", "evaluate", "commit", "apply", "status"])
    s.add_argument("--note")
    s.set_defaults(func=cmd_rules)

    s = sub.add_parser("kb", help="knowledge-base selection and conversion")
    s.add_argument("action", choices=["prepare", "import", "convert", "queue", "status"])
    s.add_argument("--batch-threads", type=int, default=8)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_kb)

    s = sub.add_parser("images", help="image descriptions (vision-off workflow)")
    s.add_argument("action", choices=["status", "review", "describe", "import"])
    s.set_defaults(func=cmd_images)

    s = sub.add_parser("questions", help="questions for the user (unknown terms, missing links)")
    s.add_argument("action", choices=["list", "add", "answer", "ingest"])
    s.add_argument("text", nargs="*")
    s.add_argument("--context")
    s.add_argument("--step", default="manual")
    s.add_argument("--all", action="store_true")
    s.add_argument("--no-fetch", action="store_true", help="don't fetch URLs found in answers")
    s.set_defaults(func=cmd_questions)

    s = sub.add_parser("raw", help="convert files dropped in raw/inbox into raw/docs/*.md")
    s.add_argument("action", choices=["import"])
    s.set_defaults(func=cmd_raw)

    s = sub.add_parser("wiki", help="wiki bookkeeping: delta queue, mark, lint, search, graph")
    s.add_argument("action", choices=["queue", "mark", "lint", "search", "graph", "status"])
    s.add_argument("paths", nargs="*", help="(mark) raw files that were ingested")
    s.add_argument("--all", action="store_true", help="(mark) everything in the queue")
    s.add_argument("--query", "-q", nargs="+", dest="query", help="(search) query text")
    s.add_argument("-n", "--limit", type=int, default=10)
    s.add_argument("--raw", action="store_true", help="(search) include raw/ sources")
    s.add_argument("--page", help="(graph) page key, e.g. people/sarah-tanaka")
    s.add_argument("--depth", type=int, default=1)
    s.set_defaults(func=cmd_wiki)

    s = sub.add_parser("people", help="relationship statistics -> work/people/PEOPLE_DIGEST.md")
    s.add_argument("action", choices=["build"], nargs="?", default="build")
    s.set_defaults(func=cmd_people)

    s = sub.add_parser("triage", help="what needs a reply / follow-up -> work/triage/TRIAGE.md")
    s.add_argument("--days", type=int)
    s.set_defaults(func=cmd_triage)

    s = sub.add_parser("style", help="collect my sent mail as writing samples -> work/style/SAMPLES.md")
    s.add_argument("-n", type=int, default=40)
    s.set_defaults(func=cmd_style)

    s = sub.add_parser("draft-outlook", help="save a drafts/*.md file as an Outlook draft (never sends)")
    s.add_argument("path")
    s.set_defaults(func=cmd_draft_outlook)

    s = sub.add_parser("teams", help="Teams chats (Graph or Outlook route)")
    s.add_argument("action", choices=["export", "probe-outlook"])
    s.add_argument("--via", choices=["graph", "outlook"], default="graph")
    s.add_argument("--days", type=int)
    s.set_defaults(func=cmd_teams)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows consoles + Japanese text
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.cmd == "wiki" and args.action == "search" and not args.query:
        args.query = args.paths
    try:
        rc = args.func(args)
    except WorkspaceError as e:
        print(e, file=sys.stderr)
        return 2
    return int(rc or 0)


if __name__ == "__main__":
    raise SystemExit(main())
