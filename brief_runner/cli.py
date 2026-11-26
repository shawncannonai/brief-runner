"""brief-runner command line: run, status, list."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import locks
from .config import load_config
from .ledger import Ledger
from .runner import OK, REFUSED_DONE, REFUSED_RUNNING, run_brief

EXIT = {OK: 0, REFUSED_RUNNING: 3, REFUSED_DONE: 4}


def _ago(ts: float) -> str:
    s = int(time.time() - ts)
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def cmd_run(a, cfg) -> int:
    if a.timeout:
        cfg.timeout = a.timeout
    say = (lambda m: print(f"brief-runner: {m}", file=sys.stderr)) if not a.quiet else None
    r = run_brief(a.brief, cfg, job=a.job, force=a.force, start_engine=a.engine, echo=say)
    if r.status in (REFUSED_RUNNING, REFUSED_DONE):
        print(f"{r.status}: {r.detail}")
    else:
        print(f"{r.status} run={r.run_id} attempts={len(r.attempts)}"
              + (f" report={r.report}" if r.report else f" ({r.detail})"))
    return EXIT.get(r.status, 1)


def cmd_status(a, cfg) -> int:
    led = Ledger(Path(cfg.state_dir) / "ledger.jsonl")
    if a.run_id:
        runs = [r for r in led.runs() if r["run_id"].startswith(a.run_id)]
        if not runs:
            print(f"no finished run matching {a.run_id}")
        for r in runs:
            print(f"run {r['run_id']}  {r['status']}  brief={r['brief']}  hash={r['hash']}"
                  + (f"  job={r['job']}" if r.get("job") else ""))
            for t in led.attempts(r["run_id"]):
                print(f"  #{t['attempt']} {t['engine']:<10} {t['status']:<10} rc={t['rc']} {t['secs']}s"
                      + (f"  {t['detail']}" if t.get("detail") else ""))
            if r.get("report"):
                print(f"  report: {r['report']}")
        return 0 if runs else 1
    live = locks.live_locks(Path(cfg.state_dir) / "locks")
    print(f"running: {sum(not l['stale'] for l in live)}")
    for l in live:
        print(f"  {l['lock']:<28} pid={l.get('pid')} run={l.get('run_id')} brief={l.get('brief')}"
              + ("  STALE (reclaimed on next run)" if l["stale"] else f"  up {_ago(l.get('started', time.time()))}"))
    runs = led.runs()
    if runs:
        r = runs[-1]
        print(f"last: {r['run_id']} {r['status']} {_ago(r['ts'])} ago  {r['brief']}")
    return 0


def cmd_list(a, cfg) -> int:
    led = Ledger(Path(cfg.state_dir) / "ledger.jsonl")
    rows = [r for r in led.rows() if r.get("event") in ("run", "refused")][-a.n:]
    if not rows:
        print("no runs yet")
    for r in rows:
        eng = ">".join(r.get("engines") or []) or "-"
        print(f"{r['run_id']}  {r['status']:<15} {eng:<20} {_ago(r['ts']):>4} ago  {r['brief']}"
              + (f"  [{r['job']}]" if r.get("job") else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brief-runner", description="One fresh agent per brief, no duplicate work.")
    p.add_argument("--config", help="TOML/JSON config (default: $BRIEF_RUNNER_CONFIG, ./brief-runner.toml|.json)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a brief on a fresh agent")
    r.add_argument("brief")
    r.add_argument("--job", help="job name: also refuse while another brief with this name runs")
    r.add_argument("--force", action="store_true", help="ignore the recent-done window")
    r.add_argument("--engine", help="start the chain at this engine")
    r.add_argument("--timeout", type=float, help="wall-clock seconds per attempt")
    r.add_argument("-q", "--quiet", action="store_true")
    s = sub.add_parser("status", help="running locks and the last run, or one run's attempts")
    s.add_argument("run_id", nargs="?")
    li = sub.add_parser("list", help="recent runs and refusals")
    li.add_argument("-n", type=int, default=20)
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    return {"run": cmd_run, "status": cmd_status, "list": cmd_list}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
