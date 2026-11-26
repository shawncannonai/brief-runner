"""Append-only JSONL run ledger."""
from __future__ import annotations

import json
import time
from pathlib import Path


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, event: str, **row) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "event": event, **row}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        return rec

    def rows(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue              # a torn last line from a killed writer is skipped, not fatal
        return out

    def runs(self) -> list[dict]:
        return [r for r in self.rows() if r.get("event") == "run"]

    def attempts(self, run_id: str) -> list[dict]:
        return [r for r in self.rows() if r.get("event") == "attempt" and r.get("run_id") == run_id]

    def recent_success(self, brief_hash: str, window: float, now: float | None = None) -> dict | None:
        if window <= 0:
            return None
        now = time.time() if now is None else now
        for r in reversed(self.runs()):
            if r.get("hash") == brief_hash and r.get("status") == "ok" and now - r.get("ts", 0) < window:
                return r
        return None
