"""Corporate directory (LDAP / Active Directory) lookups by e-mail: titles, departments and managers.

Everything company-specific is configuration (`directory.*` in kbmail.yaml / .env): server, TLS
truststore, search base and scope, filters, and which attributes mean what. Two mappings:

* ``fields``     kbmail field -> list of LDAP attributes (first non-empty wins), e.g.
                 ``title: [title]`` or ``title: [extensionAttribute5, title]``
* ``relations``  references to other people, resolved by a second lookup:
                 ``dn``: attribute holding a DN (standard AD ``manager``/``assistant``),
                 ``id``: attribute holding an id, looked up with ``id_filter``,
                 ``name``: attribute holding just a display name (used when nothing resolves)

`kbmail directory sync` looks up the people from the people digest (you first, then by
interaction), follows your manager chain, and caches results in state/directory.json
(git-ignored: it holds personal data). `kbmail people build` then derives org relationships
(manager, skip-manager, direct report, peer, same department) for /kb-people.
"""

from __future__ import annotations

import getpass
import json
import os
from datetime import datetime, timedelta

from kbmail.textutil import now_iso, parse_dt
from kbmail.workspace import Workspace

STATE = "state/directory.json"
PASSWORD_ENV = "KBMAIL_DIRECTORY_PASSWORD"


class DirectoryError(RuntimeError):
    pass


def _first(attrs: dict, names: list[str]) -> str:
    for n in names or []:
        v = attrs.get(n.casefold())
        if isinstance(v, (list, tuple)):
            v = next((x for x in v if x not in (None, "")), None)
        if v not in (None, ""):
            return str(v)
    return ""


def connect(cfg: dict, password: str | None = None, interactive: bool = True):
    """Open a read-only, bound ldap3 connection from `directory` settings (see `load_config`)."""
    try:
        import ssl

        import ldap3
    except ImportError as e:
        raise DirectoryError("Install the directory extra: uv tool install -e '.[directory]'") from e
    c = cfg
    if not c["server"] or not c["search_base"]:
        raise DirectoryError("Set server and search_base in config/directory.yaml (see `kbmail directory init`)")
    tls = None
    if c["use_ssl"]:
        tls = ldap3.Tls(
            validate=ssl.CERT_REQUIRED if c["validate_cert"] else ssl.CERT_NONE,
            ca_certs_file=c["ca_certs_file"] or None,
        )
    server = ldap3.Server(
        c["server"],
        port=int(c["port"]),
        use_ssl=bool(c["use_ssl"]),
        tls=tls,
        connect_timeout=int(c["connect_timeout"]),
        get_info=ldap3.NONE,
    )
    kw = {"auto_bind": True, "raise_exceptions": True, "read_only": True, "receive_timeout": int(c["receive_timeout"])}
    auth = str(c["auth"]).lower()
    try:
        if auth == "kerberos":
            return ldap3.Connection(server, authentication=ldap3.SASL, sasl_mechanism=ldap3.KERBEROS, **kw)
        if auth == "anonymous":
            return ldap3.Connection(server, **kw)
        user = c["user"] or default_user()
        pwd = password or os.environ.get(PASSWORD_ENV)
        if not pwd:
            if not interactive:
                raise DirectoryError(f"no password: set {PASSWORD_ENV} or use directory.auth: kerberos")
            pwd = getpass.getpass(f"Directory password for {user}: ")
        method = ldap3.NTLM if auth == "ntlm" else ldap3.SIMPLE
        return ldap3.Connection(server, user=user, password=pwd, authentication=method, **kw)
    except ldap3.core.exceptions.LDAPException as e:
        hint = ""
        if "MD4" in str(e) or "md4" in str(e):
            hint = " (NTLM needs pycryptodome: install the '.[directory]' extra)"
        raise DirectoryError(f"{type(e).__name__}: {e}{hint}") from e


CONFIG_ENV = "KBMAIL_DIRECTORY_CONFIG"


def user_config_path():
    from pathlib import Path

    return Path.home() / ".kbmail" / "directory.yaml"


def load_config(path: str | None = None, ws: Workspace | None = None) -> dict:
    """Directory settings: an explicit file, else the workspace, else $KBMAIL_DIRECTORY_CONFIG or
    ~/.kbmail/directory.yaml (so `kbmail directory lookup` also works outside a workspace)."""
    from pathlib import Path

    import yaml

    from kbmail.workspace import DEFAULT_CONFIG, deep_merge

    base = ws.config["directory"] if ws is not None else DEFAULT_CONFIG["directory"]
    if path is None and ws is not None:
        return base
    candidates = [Path(path)] if path else [Path(p) for p in [os.environ.get(CONFIG_ENV)] if p] + [user_config_path()]
    for f in candidates:
        if f.is_file():
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            data = data.get("directory", data) if isinstance(data, dict) else {}
            return deep_merge(base, data)
    if path:
        raise DirectoryError(f"{path} not found")
    raise DirectoryError(
        f"No directory settings: run inside a kbmail workspace (config/directory.yaml), pass --config FILE, "
        f"set {CONFIG_ENV}, or create {user_config_path()} (`kbmail directory init --config <file>`)"
    )


def default_user() -> str:
    dom, user = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME") or getpass.getuser()
    return f"{dom}\\{user}" if dom else user


class Directory:
    """Lookups over an ldap3 connection (real, or MOCK_SYNC in tests)."""

    def __init__(self, cfg: dict, connection):
        from kbmail import directory_views

        self.conn = connection
        self.cfg = cfg
        self.fields: dict[str, list[str]] = {k: list(v or []) for k, v in (self.cfg.get("fields") or {}).items()}
        self.relations: dict[str, dict] = self.cfg.get("relations") or {}
        attrs = {a for v in self.fields.values() for a in v}
        for spec in self.relations.values():
            attrs |= {a for key in ("dn", "id", "name") for a in (spec or {}).get(key) or []}
        attrs |= directory_views.attributes_used(cfg.get("views") or {}, cfg.get("computed") or {})
        attrs |= set(cfg.get("attributes") or [])
        self.attributes = sorted(attrs, key=str.casefold)
        self._shallow: dict[str, dict | None] = {}

    # ------------------------------------------------------------ raw searches
    def _scope(self, name: str | None = None):
        import ldap3

        return {"subtree": ldap3.SUBTREE, "level": ldap3.LEVEL, "base": ldap3.BASE}[name or self.cfg["scope"]]

    def _search(self, base: str, flt: str, scope) -> dict | None:
        self.conn.search(search_base=base, search_filter=flt, search_scope=scope, attributes=self.attributes)
        if not self.conn.entries:
            return None
        e = self.conn.entries[0]
        return {"dn": e.entry_dn, **{k.casefold(): v for k, v in e.entry_attributes_as_dict.items()}}

    def raw_by_email(self, email: str) -> dict | None:
        from ldap3.utils.conv import escape_filter_chars

        flt = self.cfg["email_filter"].replace("{email}", escape_filter_chars(email.strip()))
        return self._search(self.cfg["search_base"], flt, self._scope())

    def raw_by_id(self, ident: str) -> dict | None:
        from ldap3.utils.conv import escape_filter_chars

        flt = self.cfg["id_filter"].replace("{id}", escape_filter_chars(ident.strip()))
        return self._search(self.cfg["search_base"], flt, self._scope())

    def raw_by_dn(self, dn: str) -> dict | None:
        return self._search(dn, "(objectClass=*)", self._scope("base"))

    # ------------------------------------------------------------ records
    def _fields(self, raw: dict) -> dict[str, str]:
        return {k: v for k, attrs in self.fields.items() if (v := _first(raw, attrs))}

    def _shallow_person(self, key: str, fetch) -> dict | None:
        if key not in self._shallow:
            raw = fetch()
            self._shallow[key] = None if raw is None else {"dn": raw["dn"], **self._fields(raw)}
        return self._shallow[key]

    def record(self, raw: dict) -> dict:
        rels = {}
        for rname, spec in self.relations.items():
            spec = spec or {}
            target, ident = None, ""
            dn = _first(raw, spec.get("dn") or [])
            if dn:
                target = self._shallow_person("dn:" + dn, lambda dn=dn: self.raw_by_dn(dn))
            if target is None:
                ident = _first(raw, spec.get("id") or [])
                if ident:
                    target = self._shallow_person("id:" + ident, lambda i=ident: self.raw_by_id(i))
            name = _first(raw, spec.get("name") or [])
            if target or ident or name:
                rels[rname] = {
                    "name": (target or {}).get("name") or name,
                    "email": ((target or {}).get("email") or "").lower(),
                    "id": ident or (target or {}).get("employee_id", ""),
                    "title": (target or {}).get("title", ""),
                }
        return {"found": True, "dn": raw["dn"], "fields": self._fields(raw), "relations": rels}

    def lookup(self, email: str) -> dict:
        raw = self.raw_by_email(email)
        return self.record(raw) if raw else {"found": False}

    def card(self, email: str, view: str | None = None) -> dict:
        """Look a person up and render a view from `views` (the plain record when there is none)."""
        from kbmail import directory_views

        raw = self.raw_by_email(email)
        if raw is None:
            return {"found": False, "email": email}
        rec = self.record(raw)
        name = view if view is not None else self.cfg.get("view") or ""
        views = self.cfg.get("views") or {}
        if not name or name == "record":
            return {"email": email, **rec}
        if name not in views:
            raise DirectoryError(f"unknown view {name!r}; defined: {', '.join(views) or 'none'}")
        return directory_views.render(views[name], raw, rec["relations"], self.cfg.get("computed") or {})


# ---------------------------------------------------------------- sync / cache


def load(ws: Workspace) -> dict:
    return ws.load_json(STATE, {"people": {}, "chain": []})


def _fresh(entry: dict, days: int) -> bool:
    when = parse_dt(entry.get("fetched_at"))
    return bool(when) and datetime.now().astimezone() - when < timedelta(days=days)


def sync(
    ws: Workspace, connection=None, limit: int | None = None, refresh: bool = False, interactive: bool = True, log=print
) -> dict:
    """Look up me + the people digest (most active first), follow my manager chain, cache it all."""
    cfg = ws.config["directory"]
    state = load(ws)
    people = state["people"]
    conn = connection or connect(cfg, interactive=interactive)
    d = Directory(cfg, conn)
    limit = limit or int(cfg["max_lookups"])
    days = int(cfg["cache_days"])
    done = {"looked_up": 0, "cached": 0, "not_found": 0}

    def get(email: str) -> dict:
        email = email.lower()
        cur = people.get(email)
        if cur and _fresh(cur, days) and not refresh:
            done["cached"] += 1
            return cur
        if done["looked_up"] >= limit:
            return cur or {"found": False}
        rec = d.lookup(email)
        rec["fetched_at"] = now_iso()
        people[email] = rec
        done["looked_up"] += 1
        done["not_found"] += not rec["found"]
        return rec

    me = sorted(ws.me_emails)
    pstate = ws.load_json("state/people.json", {})
    me = me or pstate.get("me", [])
    if not me:
        log("Set me.emails in kbmail.yaml so the directory knows who you are.")
    chain: list[str] = []
    for email in me:
        rec = get(email)
        if rec.get("found"):
            cur = rec
            for _ in range(int(cfg["chain_depth"])):
                mgr = (cur.get("relations", {}).get("manager") or {}).get("email")
                if not mgr or mgr in chain or mgr in me:
                    break
                chain.append(mgr)
                cur = get(mgr)
                if not cur.get("found"):
                    break
            break
    state["chain"] = chain
    contacts = sorted((pstate.get("people") or {}).values(), key=lambda p: -p.get("score", 0))
    for p in contacts:
        if done["looked_up"] >= limit:
            break
        get(p["email"])
    state["synced_at"] = now_iso()
    ws.save_json(STATE, state)
    ws.record_run("directory_sync", **done)
    log(
        f"Directory: {done['looked_up']} looked up ({done['not_found']} not found), {done['cached']} from cache; "
        f"manager chain: {' -> '.join(chain) or '-'}"
    )
    if connection is None:
        conn.unbind()
    return done


# ---------------------------------------------------------------- org relationships


def org_relation(email: str, state: dict, me: set[str]) -> str:
    """Relationship of `email` to the mailbox owner according to the directory ('' if unknown)."""
    people = state.get("people") or {}
    rec = people.get(email.lower()) or {}
    if not rec.get("found"):
        return ""
    chain = state.get("chain") or []
    if chain and email == chain[0]:
        return "manager"
    if email in chain:
        return f"skip-manager (+{chain.index(email)})"
    mgr = ((rec.get("relations") or {}).get("manager") or {}).get("email", "")
    if mgr and mgr in me:
        return "direct-report"
    if mgr and (mrec := people.get(mgr)) and ((mrec.get("relations") or {}).get("manager") or {}).get("email") in me:
        return "indirect-report"
    if chain and mgr == chain[0]:
        return "peer"
    my = next((people[m] for m in me if (people.get(m) or {}).get("found")), None)
    if my and rec["fields"].get("department") and rec["fields"].get("department") == my["fields"].get("department"):
        return "same-department"
    return "colleague"


def summary(ws: Workspace, email: str, state: dict | None = None, me: set[str] | None = None) -> dict:
    """Directory facts for the people digest: org relation, selected fields, resolved relations."""
    state = state or load(ws)
    rec = (state.get("people") or {}).get(email.lower()) or {}
    if not rec.get("found"):
        return {}
    keep = ws.cfg("directory.digest_fields", [])
    return {
        "org_relation": org_relation(email.lower(), state, me if me is not None else ws.me_emails),
        **{k: v for k, v in rec["fields"].items() if k in keep},
        "relations": {k: v.get("name") or v.get("email") for k, v in (rec.get("relations") or {}).items()},
    }


def as_json(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, indent=2)
