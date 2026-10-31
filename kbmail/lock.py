"""One kbmail command at a time per workspace.

State files (labels.json, kb_selection.json, runs.json, ...) are read, changed in memory and written
back. Two commands doing that at the same time - the scheduled `kbmail sync` and a `kbmail label
import` run by Copilot, say - would each write their own copy and silently drop the other's changes.

The lock is an OS file lock on `state/.kbmail.lock` (flock on POSIX, msvcrt on Windows). The OS
releases it when the process ends, so a crashed command never leaves a stale lock behind.
`state/.kbmail.lock.json` says who holds it, for the "waiting for ..." message.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager

from kbmail.textutil import now_iso
from kbmail.workspace import Workspace

LOCK = "state/.kbmail.lock"
HOLDER = "state/.kbmail.lock.json"
WAIT_SECONDS = 600.0  # an Outlook export can take minutes; a scheduled sync should wait for it


class LockBusy(RuntimeError):
    pass


_held = threading.local()  # workspace roots this thread holds: nested use must not wait for itself


def _try_lock(f) -> bool:
    if os.name == "nt":  # pragma: no cover - Windows only
        import msvcrt

        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f) -> None:
    if os.name == "nt":  # pragma: no cover - Windows only
        import msvcrt

        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def holder(ws: Workspace) -> str:
    try:
        h = json.loads(ws.path(HOLDER).read_text(encoding="utf-8"))
        return f"`kbmail {h.get('command', '?')}` (pid {h.get('pid', '?')}, since {h.get('since', '?')[:19]})"
    except (OSError, ValueError):
        return "another kbmail command"


@contextmanager
def workspace_lock(ws: Workspace, command: str, wait: float | None = None, poll: float = 0.5, log=None):
    """Hold the workspace lock for the duration of the block; wait up to `wait` seconds for it."""
    roots = _held.__dict__.setdefault("roots", set())
    key = str(ws.root.resolve())
    if key in roots:
        yield
        return
    wait = WAIT_SECONDS if wait is None else wait
    log = log or (lambda msg: print(msg, file=sys.stderr))
    p = ws.path(LOCK)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = p.open("a+b")
    try:
        deadline = time.monotonic() + wait
        told = False
        while not _try_lock(f):
            if time.monotonic() >= deadline:
                raise LockBusy(
                    f"{holder(ws)} is still running in {ws.root}; gave up waiting after {wait:.0f}s. "
                    "Run this command again when it has finished."
                )
            if not told:
                log(f"Waiting for {holder(ws)} to finish ...")
                told = True
            time.sleep(poll)
        try:
            ws.path(HOLDER).write_text(
                json.dumps({"command": command, "pid": os.getpid(), "since": now_iso()}), encoding="utf-8"
            )
        except OSError:
            pass
        roots.add(key)
        try:
            yield
        finally:
            roots.discard(key)
            try:
                ws.path(HOLDER).unlink(missing_ok=True)
            except OSError:
                pass
            _unlock(f)
    finally:
        f.close()
