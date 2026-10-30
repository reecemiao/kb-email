"""Output views for directory lookups, defined in YAML (no company-specific code).

A view is a nested mapping: section -> label -> value spec. Value specs:

    "template"                                plain template (see below)
    {join: ", ", parts: [tpl, tpl, ...]}      non-empty rendered parts joined with the separator
    {first: [tpl, tpl, ...]}                  the first part that renders non-empty
    {value: tpl, when: tpl}                   value only if `when` renders non-empty
    {value: tpl, omit_empty: true}            drop the label entirely when the value is empty
    (join / first specs accept `when` / `omit_empty` too)
    e.g. "Name (ID)" if both, else either: {first: ["[{name} ({id})]", "{name}", "{id}"]}

Templates:
    {attr}                LDAP attribute (case-insensitive), first value
    {attr|upper}          filters: upper, lower, title, strip, oneline (line breaks -> ", "),
                          map:K=V;K2=V2;*=fallback, default:text
    {@manager.name}       resolved relation (name, email, id, title)
    {=helper}             a named helper from `computed` (same value specs)
    [ ... ]               optional segment: dropped when any placeholder inside is empty
"""

from __future__ import annotations

import re

_PH = re.compile(r"\{([^{}]+)\}")
_SEG = re.compile(r"\[([^\[\]]*)\]")
_SPEC_KEYS = {"value", "join", "first"}


def _first(raw: dict, attr: str) -> str:
    v = raw.get(attr.strip().casefold())
    if isinstance(v, (list, tuple)):
        v = next((x for x in v if x not in (None, "")), None)
    return "" if v is None else str(v)


def _filter(value: str, f: str) -> str:
    name, _, arg = f.strip().partition(":")
    if name == "upper":
        return value.upper()
    if name == "lower":
        return value.lower()
    if name == "title":
        return value.title()
    if name == "strip":
        return value.strip()
    if name == "oneline":
        v = re.sub(r"\s*(\r\n|\r|\n)\s*", ", ", value)
        return re.sub(r"(,\s*)+", ", ", v).strip(", ")
    if name == "default":
        return value or arg
    if name == "map":
        pairs = dict(p.split("=", 1) for p in arg.split(";") if "=" in p)
        return pairs.get(value, pairs.get("*", value))
    raise ValueError(f"unknown filter {name!r}")


class _Ctx:
    def __init__(self, raw: dict, relations: dict, computed: dict):
        self.raw, self.relations, self.computed = raw, relations, computed
        self._cache: dict[str, str] = {}
        self._active: set[str] = set()

    def placeholder(self, expr: str) -> str:
        head, *filters = expr.split("|")
        head = head.strip()
        if head.startswith("="):
            value = self.helper(head[1:].strip())
        elif head.startswith("@"):
            rel, _, key = head[1:].partition(".")
            value = str((self.relations.get(rel.strip()) or {}).get(key.strip() or "name", "") or "")
        else:
            value = _first(self.raw, head)
        for f in filters:
            value = _filter(value, f)
        return value

    def helper(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]
        if name in self._active:
            raise ValueError(f"computed value {name!r} refers to itself")
        if name not in self.computed:
            raise ValueError(f"unknown computed value {name!r}")
        self._active.add(name)
        self._cache[name] = value_of(self.computed[name], self) or ""
        self._active.discard(name)
        return self._cache[name]

    def template(self, tpl: str) -> str:
        def seg(m):
            inner = m.group(1)
            exprs = _PH.findall(inner)
            if not exprs:
                return m.group(0)  # brackets without placeholders are literal text
            values = {e: self.placeholder(e) for e in exprs}
            if any(v == "" for v in values.values()):
                return ""
            return _PH.sub(lambda x: values[x.group(1)], inner)

        out = _SEG.sub(seg, str(tpl))
        return _PH.sub(lambda m: self.placeholder(m.group(1)), out).strip()


def value_of(spec, ctx: _Ctx) -> str | None:
    """Rendered value, or None when the label should be omitted."""
    if isinstance(spec, dict):
        if "join" in spec:
            value = str(spec["join"]).join(v for p in spec.get("parts") or [] if (v := ctx.template(p)))
        elif "first" in spec:
            value = next((v for p in spec["first"] or [] if (v := ctx.template(p))), "")
        else:
            value = ctx.template(spec.get("value", ""))
        if "when" in spec and not ctx.template(spec["when"]):
            value = ""
        if spec.get("omit_empty") and not value:
            return None
        return value
    return ctx.template(spec)


def render(view: dict, raw: dict, relations: dict, computed: dict | None = None) -> dict:
    ctx = _Ctx(raw, relations, computed or {})

    def walk(node: dict) -> dict:
        out = {}
        for label, spec in node.items():
            if isinstance(spec, dict) and not (_SPEC_KEYS & set(spec)):
                out[label] = walk(spec)
            else:
                v = value_of(spec, ctx)
                if v is not None:
                    out[label] = v
        return out

    return walk(view)


def attributes_used(views: dict, computed: dict) -> set[str]:
    """LDAP attribute names referenced by views/computed, so the search requests them."""
    found: set[str] = set()

    def scan(node):
        if isinstance(node, dict):
            for v in node.values():
                scan(v)
        elif isinstance(node, list):
            for v in node:
                scan(v)
        elif isinstance(node, str):
            for expr in _PH.findall(node):
                head = expr.split("|")[0].strip()
                if head and head[0] not in "=@":
                    found.add(head)

    scan(views)
    scan(computed)
    return found
