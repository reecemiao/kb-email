"""Workspace layout, configuration and small persistent-state helpers.

A *workspace* is the folder you open in VS Code. It holds your private data
(exported mail, state, knowledge base) and the Copilot prompt files. The tool
code itself lives in the kbmail package, so the workspace can be its own
private git repository.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from datetime import date
from pathlib import Path

import yaml

from kbmail.textutil import now_iso

CONFIG_NAME = "kbmail.yaml"
OTHERS = "others"

DEFAULT_CONFIG: dict = {
    "me": {"name": "", "emails": []},
    "areas": [],
    "outlook": {
        "days": 30,
        "folders": ["inbox", "sent"],
        "include_subfolders": False,
        "extra_folders": [],
        "calendar": True,
        "calendar_future_days": 14,
        "save_html": True,
        "save_attachments": True,
        "attachment_max_mb": 25,
        "overlap_hours": 24,
        "max_body_chars": 60000,
    },
    "labeling": {"batch_size": 40, "preview_chars": 700, "rule_threshold": 2.0},
    "rules": {"refresh_every_days": 14, "refresh_after_new_confirmed": 50},
    "kb": {"include_auto_labels": True, "message_chars": 6000},
    "copilot": {"vision": False},
    "images": {"min_bytes": 6000},
    "privacy": {"redact": True, "extra_patterns": []},
    "people": {"top_n": 80},
    "triage": {"days": 14, "follow_up_after_days": 3},
    "wiki": {"half_life_days": {"fast": 30, "normal": 180, "slow": 730}, "stale_below": 0.35},
    "teams": {
        "tenant": "organizations",
        "client_id": "",
        "days": 30,
        "token_cache": "state/msal_cache.bin",
    },
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


class Workspace:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self._config: dict | None = None

    # ------------------------------------------------------------ discovery
    @classmethod
    def find(cls, start: str | Path | None = None) -> Workspace:
        env = os.environ.get("KBMAIL_WORKSPACE")
        if start is None and env:
            start = env
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
            user = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
            self._config = deep_merge(DEFAULT_CONFIG, user or {})
        return self._config

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


def init_workspace(target: Path, upgrade: bool = False) -> list[str]:
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    written = []
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
    ws = Workspace(target)
    ws.ensure_dirs()
    return written
