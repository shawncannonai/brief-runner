"""Exclusive, self-healing lock files.

A lock is a JSON file created with O_EXCL. It records the owner pid, host and a deadline. It is stale (and reclaimed)
when the owner pid is dead on this host, or when the deadline has passed (protects against pid reuse and against
locks written on another machine sharing the state directory).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LockHeld(Exception):
    def __init__(self, path: Path, owner: dict):
        self.path, self.owner = path, owner
        super().__init__(f"{path.name} held by pid {owner.get('pid')} (run {owner.get('run_id')})")


@dataclass
class Lock:
    path: Path
    reclaimed: dict | None = None    # the stale owner record we replaced, if any

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def read_owner(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}                     # exists but unreadable / half-written


def is_stale(owner: dict | None, path: Path, now: float | None = None, unreadable_grace: float = 30.0) -> bool:
    now = time.time() if now is None else now
    if owner is None:
        return True
    if not owner:                     # half-written: give the writer a moment, then reclaim
        try:
            return now - path.stat().st_mtime > unreadable_grace
        except OSError:
            return True
    if owner.get("deadline") and now > float(owner["deadline"]):
        return True
    if owner.get("host") == socket.gethostname():
        return not pid_alive(int(owner.get("pid", 0)))
    return False


def acquire(path: Path, info: dict, deadline: float) -> Lock:
    """Take the lock or raise LockHeld. Reclaims a stale lock once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(info, pid=os.getpid(), host=socket.gethostname(), started=time.time(), deadline=deadline)
    reclaimed = None
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            owner = read_owner(path)
            if not is_stale(owner, path):
                raise LockHeld(path, owner or {})
            reclaimed = owner or {}
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
        return Lock(path, reclaimed)
    raise LockHeld(path, read_owner(path) or {})


def live_locks(lock_dir: Path) -> list[dict]:
    rows = []
    for p in sorted(lock_dir.glob("*.lock")) if lock_dir.is_dir() else []:
        owner = read_owner(p)
        rows.append(dict(owner or {}, lock=p.name, stale=is_stale(owner, p)))
    return rows
