"""Deterministic labeling rules (config/rules.yaml), written by Copilot and evaluated by Python.

rules.yaml format
-----------------
threshold: 2.0            # an area is assigned when the summed weight reaches this
rules:
  - id: r001
    area: area1           # or "others"
    description: Vendor X mail is always area1
    weight: 3
    when:                 # ALL listed fields must match; each field matches if ANY pattern matches
      from_domain: [vendorx.com]
      subject: ["re:\\bPRJ-\\d+", "steering"]
    unless:               # optional: rule is ignored if ANY of these match
      subject: [newsletter]

Fields: from, from_domain, to, cc, participants, participant_domain, subject, body,
folder, direction, categories, attachment, source.
Patterns are case-insensitive substrings; prefix "re:" for a regex. Domain fields
match the domain or any subdomain.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from itertools import chain

from kbmail.records import message_index, participants, read_message
from kbmail.textutil import compact, email_domain, now_iso, parse_dt, strip_quoted, tokenize
from kbmail.workspace import OTHERS, Workspace

RULES_FILE = "config/rules.yaml"
FIELDS = {
    "from",
    "from_domain",
    "to",
    "cc",
    "participants",
    "participant_domain",
    "subject",
    "body",
    "folder",
    "direction",
    "categories",
    "attachment",
    "source",
}
DOMAIN_FIELDS = {"from_domain", "participant_domain"}


def load_rules(ws: Workspace) -> dict:
    data = ws.load_yaml(RULES_FILE, {}) or {}
    data.setdefault("threshold", ws.cfg("labeling.rule_threshold", 2.0))
    data.setdefault("rules", [])
    return data


def validate_rules(ws: Workspace, data: dict) -> list[str]:
    errors = []
    allowed = set(ws.labels_allowed)
    seen = set()
    for i, r in enumerate(data.get("rules") or []):
        rid = r.get("id") or f"#{i + 1}"
        if rid in seen:
            errors.append(f"{rid}: duplicate id")
        seen.add(rid)
        if r.get("area") not in allowed:
            errors.append(f"{rid}: unknown area {r.get('area')!r} (allowed: {sorted(allowed)})")
        if not r.get("when"):
            errors.append(f"{rid}: missing 'when'")
        for block in ("when", "unless"):
            for field, pats in (r.get(block) or {}).items():
                if field not in FIELDS:
                    errors.append(f"{rid}: unknown field {field!r} in {block}")
                for p in pats if isinstance(pats, list) else [pats]:
                    if isinstance(p, str) and p.startswith("re:"):
                        try:
                            re.compile(p[3:])
                        except re.error as e:
                            errors.append(f"{rid}: bad regex {p!r}: {e}")
        try:
            float(r.get("weight", 1))
        except (TypeError, ValueError):
            errors.append(f"{rid}: weight must be a number")
    return errors


# ---------------------------------------------------------------- features & matching


def features(rec: dict, body: str = "") -> dict[str, list[str]]:
    frm = rec.get("from") or {}
    parts = participants(rec)
    return {
        "from": [f"{frm.get('name', '')} <{frm.get('email', '')}>".lower()],
        "from_domain": [email_domain(frm.get("email", ""))],
        "to": [f"{p.get('name', '')} <{p.get('email', '')}>".lower() for p in rec.get("to") or []],
        "cc": [f"{p.get('name', '')} <{p.get('email', '')}>".lower() for p in rec.get("cc") or []],
        "participants": parts,
        "participant_domain": sorted({email_domain(e) for e in parts}),
        "subject": [rec.get("subject") or ""],
        "body": [body or ""],
        "folder": [rec.get("folder") or ""],
        "direction": [rec.get("direction") or ""],
        "categories": list(rec.get("categories") or []),
        "attachment": [a.get("name", "") for a in rec.get("attachments") or []],
        "source": [rec.get("source") or ""],
    }


def _match_one(field: str, values: list[str], pattern) -> bool:
    pattern = str(pattern)
    if pattern.startswith("re:"):
        rx = re.compile(pattern[3:], re.I)
        return any(rx.search(v or "") for v in values)
    p = pattern.lower()
    if field in DOMAIN_FIELDS:
        p = p.lstrip("@")
        return any(v == p or v.endswith("." + p) for v in values if v)
    return any(p in (v or "").lower() for v in values)


def _block_matches(field_map: dict, feats: dict, mode_all: bool) -> bool:
    results = []
    for field, pats in field_map.items():
        pats = pats if isinstance(pats, list) else [pats]
        results.append(any(_match_one(field, feats.get(field, []), p) for p in pats))
    return all(results) if mode_all else any(results)


def score(rules: dict, feats: dict) -> tuple[dict[str, float], dict[str, list[str]]]:
    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, list[str]] = defaultdict(list)
    for r in rules.get("rules") or []:
        if r.get("disabled"):
            continue
        if not _block_matches(r.get("when") or {}, feats, True):
            continue
        if r.get("unless") and _block_matches(r["unless"], feats, False):
            continue
        scores[r["area"]] += float(r.get("weight", 1))
        hits[r["area"]].append(r.get("id", "?"))
    return dict(scores), dict(hits)


def classify(rules: dict, feats: dict) -> tuple[list[str] | None, dict, dict]:
    scores, hits = score(rules, feats)
    th = float(rules.get("threshold", 2.0))
    areas = sorted([a for a, s in scores.items() if a != OTHERS and s >= th], key=lambda a: -scores[a])
    if areas:
        return areas, scores, hits
    if scores.get(OTHERS, 0) >= th:
        return [OTHERS], scores, hits
    return None, scores, hits


def _body_for_rules(ws: Workspace, rec: dict) -> str:
    try:
        _, body = read_message(ws, rec)
    except FileNotFoundError:
        return ""
    return strip_quoted(body)[:5000]


# ---------------------------------------------------------------- apply / evaluate


def apply_rules(ws: Workspace, relabel_auto: bool = True, log=print) -> dict:
    """Label unlabeled messages (and refresh rule-made labels) with config/rules.yaml."""
    from kbmail.labels import load_labels, save_labels, set_label

    rules = load_rules(ws)
    if not rules["rules"]:
        log("No rules yet (config/rules.yaml is empty) - nothing applied.")
        return {"labeled": 0}
    labels = load_labels(ws)
    n_new = n_changed = n_cleared = 0
    for rec in message_index(ws).all():
        cur = labels.get(rec["id"])
        if cur and not (relabel_auto and cur.get("status") == "auto"):
            continue
        areas, _, hits = classify(rules, features(rec, _body_for_rules(ws, rec)))
        if areas is None:
            if cur and cur.get("status") == "auto":
                del labels[rec["id"]]  # rules no longer cover it -> back to the LLM queue
                n_cleared += 1
            continue
        if cur and cur.get("labels") == areas:
            continue
        set_label(
            labels,
            rec["id"],
            areas,
            status="auto",
            source="rule",
            confidence=None,
            reason="rules: " + ", ".join(chain.from_iterable(hits.values())),
        )
        n_changed += bool(cur)
        n_new += not cur
    save_labels(ws, labels)
    log(f"Rules labeled {n_new} new message(s), updated {n_changed}, released {n_cleared} back to the queue.")
    return {"labeled": n_new, "updated": n_changed, "released": n_cleared}


def evaluate(ws: Workspace, rules: dict | None = None) -> dict:
    """Compare rule output with user-confirmed labels."""
    from kbmail.labels import load_labels

    rules = rules or load_rules(ws)
    labels = load_labels(ws)
    index = message_index(ws)
    per = {a: {"tp": 0, "fp": 0, "fn": 0} for a in ws.labels_allowed}
    covered = correct = total = 0
    mistakes = []
    for mid, lab in labels.items():
        if lab.get("status") != "confirmed" or mid not in index:
            continue
        rec = index.get(mid)
        truth = set(lab["labels"])
        pred_list, _, hits = classify(rules, features(rec, _body_for_rules(ws, rec)))
        pred = set(pred_list or [])
        total += 1
        if pred:
            covered += 1
            correct += pred == truth
        for a in per:
            if a in pred and a in truth:
                per[a]["tp"] += 1
            elif a in pred:
                per[a]["fp"] += 1
            elif a in truth:
                per[a]["fn"] += 1
        if pred and pred != truth:
            mistakes.append(
                {
                    "id": mid,
                    "truth": sorted(truth),
                    "pred": sorted(pred),
                    "rules": hits,
                    "subject": rec.get("subject", ""),
                    "from": (rec.get("from") or {}).get("email", ""),
                }
            )
    for c in per.values():
        c["precision"] = round(c["tp"] / (c["tp"] + c["fp"]), 3) if c["tp"] + c["fp"] else None
        c["recall"] = round(c["tp"] / (c["tp"] + c["fn"]), 3) if c["tp"] + c["fn"] else None
    return {
        "confirmed": total,
        "coverage": round(covered / total, 3) if total else None,
        "accuracy_when_covered": round(correct / covered, 3) if covered else None,
        "per_area": per,
        "mistakes": mistakes,
        "rules": len(rules.get("rules") or []),
    }


def mine_features(ws: Workspace, min_support: int = 3, min_purity: float = 0.8) -> dict[str, list[dict]]:
    """Find senders/domains/subject tokens that predict an area among confirmed labels."""
    from kbmail.labels import load_labels

    labels = load_labels(ws)
    index = message_index(ws)
    counts: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for mid, lab in labels.items():
        if lab.get("status") != "confirmed" or mid not in index:
            continue
        rec = index.get(mid)
        frm = (rec.get("from") or {}).get("email", "")
        feats = {("from", frm), ("from_domain", email_domain(frm))}
        feats |= {("participant_domain", email_domain(e)) for e in participants(rec)}
        feats |= {("subject", t) for t in set(tokenize(rec.get("subject", ""))) if len(t) >= 2}
        for c in rec.get("categories") or []:
            feats.add(("categories", c))
        for f in feats:
            if f[1]:
                for a in lab["labels"]:
                    counts[f][a] += 1
    out: dict[str, list[dict]] = defaultdict(list)
    for (field, value), c in counts.items():
        total = sum(c.values())
        area, n = c.most_common(1)[0]
        if total >= min_support and n / total >= min_purity:
            out[area].append({"field": field, "value": value, "support": total, "purity": round(n / total, 2)})
    for area in out:
        out[area].sort(key=lambda d: (-d["support"], -d["purity"]))
    return dict(out)


# ---------------------------------------------------------------- Copilot hand-off


def due(ws: Workspace) -> tuple[bool, str]:
    from kbmail.labels import load_labels

    last = ws.last_run("rules_commit")
    confirmed = sum(1 for v in load_labels(ws).values() if v.get("status") == "confirmed")
    if not last:
        return (confirmed > 0, f"never run; {confirmed} confirmed labels available")
    days = (datetime.now().astimezone() - parse_dt(last["last_run"])).days
    new = confirmed - int(last.get("confirmed", 0))
    every = ws.cfg("rules.refresh_every_days", 14)
    after = ws.cfg("rules.refresh_after_new_confirmed", 50)
    is_due = (days >= every and new > 0) or new >= after
    return is_due, f"last refresh {days} day(s) ago, {new} new confirmed label(s) since"


def _fmt_eval(ev: dict) -> list[str]:
    lines = [
        f"- confirmed labels: {ev['confirmed']}, rules: {ev['rules']}",
        f"- coverage (rules decide): {ev['coverage']}",
        f"- accuracy when rules decide: {ev['accuracy_when_covered']}",
        "",
        "| area | tp | fp | fn | precision | recall |",
        "|---|---|---|---|---|---|",
    ]
    for a, c in ev["per_area"].items():
        lines.append(f"| {a} | {c['tp']} | {c['fp']} | {c['fn']} | {c['precision']} | {c['recall']} |")
    return lines


def prepare_context(ws: Workspace, log=print) -> str:
    """Write work/rules/RULES_CONTEXT.md: everything Copilot needs to (re)write rules.yaml."""
    from kbmail.labels import load_labels

    rules = load_rules(ws)
    ev = evaluate(ws, rules)
    mined = mine_features(ws)
    labels = load_labels(ws)
    index = message_index(ws)
    last = ws.last_run("rules_commit")
    since = parse_dt(last["last_run"]) if last else None
    lines = [
        f"# Rules context ({now_iso()})",
        "",
        "Generated by `kbmail rules prepare`. Use with the `/kb-rules` prompt.",
        "",
        "## Areas",
        "",
    ]
    for a in ws.areas:
        lines.append(f"- **{a['name']}**: {a['description']} (keywords: {', '.join(a['keywords']) or '-'})")
    lines.append(f"- **{OTHERS}**: anything else")
    lines += [
        "",
        "## Current rules evaluated against confirmed labels",
        "",
        *_fmt_eval(ev),
        "",
        "### Mistakes (rules disagree with the user)",
        "",
    ]
    for m in ev["mistakes"][:60]:
        lines.append(
            f"- `{m['id']}` truth={m['truth']} pred={m['pred']} rules={m['rules']} | {m['from']} | {m['subject']}"
        )
    if not ev["mistakes"]:
        lines.append("- none")
    lines += ["", "## Strong signals mined from confirmed labels (support >= 3, purity >= 0.8)", ""]
    for area, feats in mined.items():
        lines.append(f"### {area}")
        for f in feats[:40]:
            lines.append(f"- {f['field']} = `{f['value']}` (support {f['support']}, purity {f['purity']})")
        lines.append("")
    lines += ["## Confirmed examples not covered by current rules (sample)", ""]
    uncovered = [
        mid
        for mid, lab in labels.items()
        if lab.get("status") == "confirmed"
        and mid in index
        and classify(rules, features(index.get(mid), _body_for_rules(ws, index.get(mid))))[0] is None
    ]
    for mid in uncovered[:80]:
        rec = index.get(mid)
        lines.append(
            f"- `{mid}` [{', '.join(labels[mid]['labels'])}] {(rec.get('from') or {}).get('email', '')} | "
            f"{rec.get('subject', '')}"
        )
    if since:
        recent = [
            mid
            for mid, lab in labels.items()
            if lab.get("status") == "confirmed" and parse_dt(lab.get("updated")) and parse_dt(lab["updated"]) > since
        ]
        lines += ["", f"## Labels confirmed since last rules refresh ({len(recent)})", ""]
        for mid in recent[:80]:
            rec = index.get(mid) or {}
            lines.append(
                f"- `{mid}` [{', '.join(labels[mid]['labels'])}] {(rec.get('from') or {}).get('email', '')} | "
                f"{compact(rec.get('subject', ''), 120)}"
            )
    journal = ws.path("config/rules_journal.md")
    if journal.exists():
        lines += [
            "",
            "## Previous conclusions (config/rules_journal.md, latest last)",
            "",
            journal.read_text(encoding="utf-8")[-6000:],
        ]
    out = ws.path("work/rules/RULES_CONTEXT.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {ws.rel(out)} (coverage {ev['coverage']}, accuracy {ev['accuracy_when_covered']}).")
    return ws.rel(out)


def commit(ws: Workspace, note: str = "", log=print) -> bool:
    """Validate rules.yaml, snapshot it to state/rules_history and record the run."""
    from kbmail.labels import load_labels

    rules = load_rules(ws)
    errors = validate_rules(ws, rules)
    if errors:
        for e in errors:
            log("ERROR " + e)
        return False
    ev = evaluate(ws, rules)
    hist = ws.path("state/rules_history")
    hist.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copyfile(ws.path(RULES_FILE), hist / f"rules_{stamp}.yaml")
    confirmed = sum(1 for v in load_labels(ws).values() if v.get("status") == "confirmed")
    ws.record_run(
        "rules_commit",
        confirmed=confirmed,
        coverage=ev["coverage"],
        accuracy=ev["accuracy_when_covered"],
        rules=ev["rules"],
        note=note,
    )
    log(
        f"Rules committed: {ev['rules']} rules, coverage {ev['coverage']}, accuracy {ev['accuracy_when_covered']}. "
        f"Snapshot state/rules_history/rules_{stamp}.yaml"
    )
    return True


def print_eval(ws: Workspace, log=print) -> dict:
    ev = evaluate(ws)
    for line in _fmt_eval(ev):
        log(line)
    if ev["mistakes"]:
        log(f"\n{len(ev['mistakes'])} mistake(s); first ones:")
        for m in ev["mistakes"][:15]:
            log(f"  {m['id']} truth={m['truth']} pred={m['pred']} via {m['rules']} | {m['subject'][:70]}")
    return ev
