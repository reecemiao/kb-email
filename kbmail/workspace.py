"""Workspace layout, configuration and small persistent-state helpers.

A *workspace* is the folder you open in VS Code. It holds your private data
(exported mail, state, knowledge base) and the Copilot prompt files. The tool
code itself lives in the kbmail package, so the workspace can be its own
private git repository.

Configuration precedence (highest first):
  1. command-line flags
  2. environment variables          KBMAIL_<SECTION>__<KEY>=value
  3. .env in the current directory  (same variables)
  4. .env in the workspace root     (same variables)
  5. kbmail.yaml
  6. DEFAULT_CONFIG below
Every CLI option that is not a one-off action (ids, paths, --force) has a
config key, so every default can be set in kbmail.yaml or .env.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from datetime import date
from pathlib import Path

import yaml
from dotenv import dotenv_values

from kbmail.textutil import now_iso

CONFIG_NAME = "kbmail.yaml"
OTHERS = "others"
ENV_PREFIX = "KBMAIL_"
ENV_WORKSPACE = "KBMAIL_WORKSPACE"
ENV_FILE = ".env"

DEFAULT_CONFIG: dict = {
    "me": {"name": "", "emails": []},
    "areas": [],
    "outlook": {
        "days": 30,
        "folders": ["inbox", "sent"],
        "include_subfolders": False,
        "extra_folders": [],
        "all_folders": False,
        "calendar": True,
        "calendar_future_days": 14,
        "save_html": True,
        "save_attachments": True,
        "attachment_max_mb": 25,
        "overlap_hours": 24,
        "max_body_chars": 60000,
    },
    "labeling": {"batch_size": 40, "limit": None, "preview_chars": 700, "rule_threshold": 2.0},
    "review": {"source": "proposed", "order": "confidence", "accept_above": None},
    "rules": {"refresh_every_days": 14, "refresh_after_new_confirmed": 50},
    "kb": {"include_auto_labels": True, "message_chars": 6000, "select_batch_threads": 8},
    "copilot": {"vision": False},
    "images": {"min_bytes": 6000},
    "privacy": {"redact": True, "extra_patterns": []},
    "questions": {"fetch_links": True},
    "people": {"top_n": 80},
    "triage": {"days": 14, "follow_up_after_days": 3},
    "mail": {"search_limit": 15},
    "style": {"samples": 40},
    "sync": {"export": True, "teams": False},
    "wiki": {
        "half_life_days": {"fast": 30, "normal": 180, "slow": 730},
        "stale_below": 0.35,
        "search_limit": 10,
        "search_raw": False,
        "graph_depth": 1,
    },
    "teams": {
        "via": "graph",
        "tenant": "organizations",
        "client_id": "",
        "days": 30,
        "token_cache": "state/msal_cache.bin",
    },
}

# One-line help per key, used for the generated .env.example and `kbmail config show`.
CONFIG_HELP: dict[str, str] = {
    "me.name": "your display name (used in drafts)",
    "me.emails": "all your SMTP addresses, comma-separated",
    "areas": "area names (comma-separated) or a YAML list of {name, description, keywords}",
    "outlook.days": "first export window in days; later runs are deltas (export --days)",
    "outlook.folders": "folders to export: inbox, sent, or paths like Inbox/Projects (export --folder)",
    "outlook.include_subfolders": "also export subfolders (export --include-subfolders)",
    "outlook.extra_folders": "additional folders, e.g. archives",
    "outlook.all_folders": "every mail folder of the default store (export --all-folders)",
    "outlook.calendar": "export the calendar too (export --[no-]calendar)",
    "outlook.calendar_future_days": "calendar look-ahead in days",
    "outlook.save_html": "keep HTML bodies (tables, inline images) for KB conversion",
    "outlook.save_attachments": "save attachments",
    "outlook.attachment_max_mb": "skip attachments larger than this",
    "outlook.overlap_hours": "delta export re-reads this much before the last watermark",
    "outlook.max_body_chars": "truncate longer plain-text bodies",
    "labeling.batch_size": "messages per Copilot labeling batch (label prepare --batch-size)",
    "labeling.limit": "max messages to queue per `label prepare` (empty = all) (--limit)",
    "labeling.preview_chars": "body preview length in labeling batches",
    "labeling.rule_threshold": "default rule threshold if rules.yaml sets none",
    "review.source": "proposed | auto | all (review --source)",
    "review.order": "confidence | date (review --order)",
    "review.accept_above": "bulk-confirm proposals with confidence >= X first (review --accept-above)",
    "rules.refresh_every_days": "rules refresh is due after this many days...",
    "rules.refresh_after_new_confirmed": "...or after this many newly confirmed labels",
    "kb.include_auto_labels": "rule-labeled (unreviewed) mail can feed the KB",
    "kb.message_chars": "max characters per message in KB selection batches",
    "kb.select_batch_threads": "threads per KB selection batch (kb prepare --batch-threads)",
    "copilot.vision": "true if Copilot may look at images",
    "images.min_bytes": "ignore smaller images (logos, signature icons)",
    "privacy.redact": "strip passwords/tokens/keys before raw/ and wiki/",
    "privacy.extra_patterns": "extra regexes to redact (YAML list)",
    "questions.fetch_links": "fetch URLs found in answers into raw/web (questions ingest --[no-]fetch)",
    "people.top_n": "contacts listed in the people digest",
    "triage.days": "look-back window for triage (triage --days)",
    "triage.follow_up_after_days": "flag my unanswered questions after N days",
    "mail.search_limit": "results for `kbmail mail` (-n)",
    "style.samples": "sent messages collected by `kbmail style` (-n)",
    "sync.export": "export from Outlook during `kbmail sync` (--[no-]export)",
    "sync.teams": "also export Teams during `kbmail sync` (--[no-]teams)",
    "wiki.half_life_days.fast": "confidence half-life for decay: fast",
    "wiki.half_life_days.normal": "confidence half-life for decay: normal",
    "wiki.half_life_days.slow": "confidence half-life for decay: slow",
    "wiki.stale_below": "lint flags pages whose effective confidence is below this",
    "wiki.search_limit": "results for `kbmail wiki search` (-n)",
    "wiki.search_raw": "include raw/ sources in wiki search (--[no-]raw)",
    "wiki.graph_depth": "neighbourhood depth for `kbmail wiki graph` (--depth)",
    "teams.via": "graph | outlook (teams export --via)",
    "teams.tenant": "Entra tenant id or domain",
    "teams.client_id": "Entra app (public client) id with Chat.Read",
    "teams.days": "Teams look-back window (teams export --days)",
    "teams.token_cache": "MSAL token cache path (keep it out of git)",
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class WorkspaceError(RuntimeError):
    pass


# ---------------------------------------------------------------- environment / .env

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _leaf_paths(d: dict, prefix: tuple = ()) -> list[tuple[str, ...]]:
    out = []
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out += _leaf_paths(v, (*prefix, k))
        else:
            out.append((*prefix, k))
    return out


def env_name(path: tuple[str, ...] | str) -> str:
    parts = path.split(".") if isinstance(path, str) else path
    return ENV_PREFIX + "__".join(p.upper() for p in parts)


def read_env(root: Path | None = None) -> dict[str, tuple[str, str]]:
    """KBMAIL_* variables -> (value, source). Later sources win: workspace .env, cwd .env, environment."""
    out: dict[str, tuple[str, str]] = {}
    files = []
    if root is not None:
        files.append(Path(root) / ENV_FILE)
    cwd_env = Path.cwd() / ENV_FILE
    if not files or cwd_env.resolve() != files[0].resolve():
        files.append(cwd_env)
    for f in files:
        if f.is_file():
            for k, v in dotenv_values(f).items():
                if k.startswith(ENV_PREFIX) and v is not None:
                    out[k] = (v, str(f))
    for k, v in os.environ.items():
        if k.startswith(ENV_PREFIX):
            out[k] = (v, "environment")
    return out


def coerce(raw: str, default, var: str):
    """Parse an environment string using the type of the default value."""
    text = raw.strip()
    try:
        if isinstance(default, bool):
            low = text.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError("expected true/false")
        if isinstance(default, int):
            return int(text)
        if isinstance(default, float):
            return float(text)
        if isinstance(default, list):
            if text.startswith("["):
                val = yaml.safe_load(text)
                if not isinstance(val, list):
                    raise ValueError("expected a list")
                return val
            return [x.strip() for x in text.split(",") if x.strip()]
        if isinstance(default, dict):
            val = yaml.safe_load(text)
            if not isinstance(val, dict):
                raise ValueError("expected a mapping like {a: 1}")
            return val
        if isinstance(default, str):
            return raw
        return yaml.safe_load(text)  # default None: a number or bool
    except (ValueError, yaml.YAMLError) as e:
        raise WorkspaceError(f"{var}={raw!r}: {e}") from e


def env_overrides(root: Path | None = None) -> tuple[dict, dict[str, str], list[str]]:
    """Return (nested overrides, {dotted key: source}, warnings) from KBMAIL_* variables."""
    known = {env_name(p): p for p in _leaf_paths(DEFAULT_CONFIG)}
    sections = {env_name((k,)): (k,) for k, v in DEFAULT_CONFIG.items() if isinstance(v, dict)}
    overrides: dict = {}
    sources: dict[str, str] = {}
    warnings = []
    for var, (raw, src) in sorted(read_env(root).items()):
        if var == ENV_WORKSPACE or not raw.strip():
            continue  # an empty value (KEY=) means "not set"
        path = known.get(var) or sections.get(var)
        if path is None:
            warnings.append(f"ignoring unknown setting {var} (from {src}); see .env.example for valid names")
            continue
        default = DEFAULT_CONFIG
        for part in path:
            default = default[part]
        cur = overrides
        for part in path[:-1]:
            cur = cur.setdefault(part, {})
        cur[path[-1]] = coerce(raw, default, var)
        sources[".".join(path)] = src
    return overrides, sources, warnings


def env_example() -> str:
    """A commented .env template listing every setting with its default."""
    lines = [
        "# kbmail settings. Copy to .env (or set real environment variables) and uncomment what you need.",
        "# Precedence: CLI flag > environment > .env (current dir) > .env (workspace) > kbmail.yaml > default.",
        "# Names: KBMAIL_<SECTION>__<KEY> (double underscore between levels).",
        "# Lists: comma-separated (a,b) or YAML ([a, b]). Booleans: true/false.",
        "",
        "# Workspace folder used when kbmail runs outside it (same as --workspace).",
        f"# {ENV_WORKSPACE}=C:\\kb",
    ]
    section = None
    for path in _leaf_paths(DEFAULT_CONFIG):
        if path[0] != section:
            section = path[0]
            lines += ["", f"# --- {section}"]
        default = DEFAULT_CONFIG
        for part in path:
            default = default[part]
        if isinstance(default, bool):
            val = str(default).lower()
        elif isinstance(default, list):
            val = (
                ",".join(map(str, default))
                if all(isinstance(x, str) for x in default)
                else yaml.safe_dump(default, default_flow_style=True).strip()
            )
        elif default is None:
            val = ""
        else:
            val = str(default)
        help_ = CONFIG_HELP.get(".".join(path))
        if help_:
            lines.append(f"# {help_}")
        lines.append(f"# {env_name(path)}={val}")
    return "\n".join(lines) + "\n"


class Workspace:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self._config: dict | None = None
        self.config_sources: dict[str, str] = {}
        self.config_warnings: list[str] = []

    # ------------------------------------------------------------ discovery
    @classmethod
    def find(cls, start: str | Path | None = None) -> Workspace:
        if start is None:
            env = read_env().get(ENV_WORKSPACE)
            if env:
                start = env[0]
        here = Path(start or Path.cwd()).resolve()
        for p in [here, *here.parents]:
            if (p / CONFIG_NAME).exists():
                return cls(p)
        raise WorkspaceError(
            f"No {CONFIG_NAME} found in {here} or its parents. Run `kbmail init <folder>` first, "
            "or pass --workspace / set KBMAIL_WORKSPACE."
        )

    # ------------------------------------------------------------ paths
    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def rel(self, p: Path | str) -> str:
        p = Path(p)
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    def ensure_dirs(self) -> None:
        for d in (
            "config",
            "data/messages",
            "data/calendar",
            "state",
            "work",
            "review/images",
            "raw/email",
            "raw/teams",
            "raw/web",
            "raw/notes",
            "raw/docs",
            "raw/inbox",
            "wiki",
            "drafts",
        ):
            self.path(d).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ config
    @property
    def config(self) -> dict:
        if self._config is None:
            p = self.path(CONFIG_NAME)
            user = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}) or {}
            env, env_sources, self.config_warnings = env_overrides(self.root)
            self._config = deep_merge(deep_merge(DEFAULT_CONFIG, user), env)
            self.config_sources = {".".join(k): "default" for k in _leaf_paths(DEFAULT_CONFIG)}
            for k in _leaf_paths(user):
                self.config_sources[".".join(k)] = CONFIG_NAME
            self.config_sources.update(env_sources)
        return self._config

    def opt(self, value, dotted: str):
        """A CLI value if given, else the configured default (kbmail.yaml / .env / environment)."""
        return self.cfg(dotted) if value is None else value

    def cfg(self, dotted: str, default=None):
        cur = self.config
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def areas(self) -> list[dict]:
        out = []
        for a in self.config.get("areas") or []:
            if isinstance(a, str):
                a = {"name": a}
            out.append(
                {
                    "name": str(a["name"]).strip(),
                    "description": a.get("description", ""),
                    "keywords": a.get("keywords", []) or [],
                }
            )
        return out

    @property
    def area_names(self) -> list[str]:
        return [a["name"] for a in self.areas]

    @property
    def labels_allowed(self) -> list[str]:
        return [*self.area_names, OTHERS]

    def normalize_label(self, label: str) -> str | None:
        want = str(label).strip().lower()
        for name in self.labels_allowed:
            if name.lower() == want:
                return name
        return None

    @property
    def me_emails(self) -> set[str]:
        return {e.strip().lower() for e in (self.cfg("me.emails") or []) if e}

    # ------------------------------------------------------------ json state
    def load_json(self, rel: str, default=None):
        p = self.path(rel)
        if not p.exists():
            return copy.deepcopy(default) if default is not None else {}
        return json.loads(p.read_text(encoding="utf-8"))

    def save_json(self, rel: str, obj) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, p)
        return p

    def load_yaml(self, rel: str, default=None):
        p = self.path(rel)
        if not p.exists():
            return copy.deepcopy(default)
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return data if data is not None else copy.deepcopy(default)

    def save_yaml(self, rel: str, obj) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
        return p

    # ------------------------------------------------------------ run log
    def record_run(self, step: str, **info) -> None:
        runs = self.load_json("state/runs.json", {})
        entry = {"last_run": now_iso(), **info}
        runs[step] = entry
        hist = runs.setdefault("_history", [])
        hist.append({"step": step, **entry})
        runs["_history"] = hist[-500:]
        self.save_json("state/runs.json", runs)

    def last_run(self, step: str) -> dict | None:
        return self.load_json("state/runs.json", {}).get(step)


# ---------------------------------------------------------------- init

TEMPLATE_DIR = Path(__file__).parent / "templates" / "workspace"
# Hidden folders can't be shipped reliably as package data, so they are stored with a
# leading underscore and renamed on copy.
_RENAMES = {"_github": ".github", "_vscode": ".vscode", "_gitignore": ".gitignore"}
# Files the user is expected to edit: never overwritten by `init --upgrade`.
_USER_OWNED = {
    CONFIG_NAME,
    "config/rules.yaml",
    "config/people.yaml",
    "config/rules_journal.md",
    "wiki/index.md",
    "wiki/log.md",
    "wiki/overview.md",
    "wiki/glossary.md",
}


# Files shipped by earlier versions and replaced since (prompt files -> skills,
# copilot-instructions.md -> AGENTS.md). Removed on upgrade only if they are still ours.
_OBSOLETE = {
    ".github/copilot-instructions.md": "# Copilot instructions for this kbmail workspace",
    **{
        f".github/prompts/{n}.prompt.md": "---"
        for n in (
            "kb-ask",
            "kb-draft",
            "kb-format",
            "kb-images",
            "kb-label",
            "kb-people",
            "kb-rules",
            "kb-select",
            "kb-start",
            "kb-style",
            "kb-suggest",
            "kb-svg",
            "kb-sync",
            "kb-wiki-ingest",
            "kb-wiki-lint",
        )
    },
}


def _remove_obsolete(target: Path) -> list[str]:
    removed = []
    for rel, marker in _OBSOLETE.items():
        p = target / rel
        if p.is_file() and p.read_text(encoding="utf-8", errors="replace").startswith(marker):
            p.unlink()
            removed.append(rel)
    prompts = target / ".github" / "prompts"
    if prompts.is_dir() and not any(prompts.iterdir()):
        prompts.rmdir()
    return removed


def init_workspace(target: Path, upgrade: bool = False) -> list[str]:
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    written = []
    if upgrade:
        written += [f"removed {r}" for r in _remove_obsolete(target)]
    for src in sorted(TEMPLATE_DIR.rglob("*")):
        if src.is_dir() or src.name == "__pycache__" or src.suffix == ".pyc":
            continue
        rel_parts = [_RENAMES.get(p, p) for p in src.relative_to(TEMPLATE_DIR).parts]
        rel = "/".join(rel_parts)
        dst = target.joinpath(*rel_parts)
        if dst.exists() and (not upgrade or rel in _USER_OWNED):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".md":
            text = src.read_text(encoding="utf-8").replace("{{TODAY}}", date.today().isoformat())
            dst.write_text(text, encoding="utf-8")
        else:
            shutil.copyfile(src, dst)
        written.append(rel)
    # Generated from DEFAULT_CONFIG so it always lists every current setting.
    (target / ".env.example").write_text(env_example(), encoding="utf-8")
    written.append(".env.example")
    if not (target / ENV_FILE).exists():
        (target / ENV_FILE).write_text(env_example(), encoding="utf-8")
        written.append(ENV_FILE)
    ws = Workspace(target)
    ws.ensure_dirs()
    return written
