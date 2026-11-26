"""Run one brief: dedupe, launch a fresh agent, enforce the time limit and the report contract, reroute on failure."""
from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import locks
from .config import Config, Engine
from .ledger import Ledger

OK, NO_REPORT, BAD_REPORT, TIMEOUT, QUOTA, ERROR = "ok", "no_report", "bad_report", "timeout", "quota", "error"
REFUSED_RUNNING, REFUSED_DONE = "refused_running", "refused_done"

CONTRACT = """

---
REPORT CONTRACT (added by brief-runner)
When you finish, write a plain-text report to this file:
    {report}
(also in the environment as BRIEF_REPORT). It must contain these lines, each at the start of a line:
{lines}
A run that does not leave this file counts as FAILED, whatever you print to stdout.
"""


def brief_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


@dataclass
class Attempt:
    n: int
    engine: str
    status: str
    rc: int | None
    secs: float
    report: str
    log: str
    detail: str = ""


@dataclass
class RunResult:
    run_id: str
    status: str
    brief: str
    hash: str
    job: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def report(self) -> str | None:
        return self.attempts[-1].report if self.attempts and self.ok else None


def check_report(path: Path, required: list[str]) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return NO_REPORT, "report file not written"
    if not text.strip():
        return NO_REPORT, "report file is empty"
    lines = [l.strip() for l in text.splitlines()]
    missing = [p for p in required if not any(l.startswith(p) and l[len(p):].strip() for l in lines)]
    if missing:
        return BAD_REPORT, "report missing required line(s): " + ", ".join(missing)
    return OK, ""


def kill_tree(proc: subprocess.Popen, grace: float) -> None:
    """Terminate the agent and everything it spawned (its whole process group / tree)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        proc.wait()
        return
    pgid = proc.pid                   # start_new_session=True makes the agent its own group leader

    def alive() -> bool:
        proc.poll()                   # reap the leader so a zombie does not keep the group "alive"
        try:
            os.killpg(pgid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    if alive():
        os.killpg(pgid, signal.SIGTERM)
        end = time.monotonic() + grace
        while time.monotonic() < end and alive():
            time.sleep(0.05)
        if alive():
            os.killpg(pgid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _argv(engine: Engine, prompt: str, prompt_file: Path, report: Path) -> list[str]:
    argv = [a.replace("{report}", str(report)) for a in engine.cmd]
    if engine.prompt == "arg":
        argv.append(prompt)
    elif engine.prompt == "file":
        argv.append(str(prompt_file))
    return argv


def run_attempt(cfg: Config, engine: Engine, prompt: str, run_dir: Path, n: int, env_extra: dict) -> Attempt:
    report = run_dir / f"report-{n}.md"
    log = run_dir / f"attempt-{n}-{engine.name}.log"
    prompt_file = run_dir / f"prompt-{n}.md"
    report.unlink(missing_ok=True)
    full = prompt + CONTRACT.format(report=report, lines="\n".join(f"    {l} ..." for l in cfg.required_lines))
    prompt_file.write_text(full, encoding="utf-8")
    env = dict(os.environ, BRIEF_REPORT=str(report), BRIEF_ATTEMPT=str(n), BRIEF_ENGINE=engine.name, **env_extra)
    limit = engine.timeout or cfg.timeout
    kw: dict = {"start_new_session": True} if sys.platform != "win32" else \
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    t0 = time.monotonic()
    timed_out = False
    with log.open("wb") as out:
        try:
            proc = subprocess.Popen(_argv(engine, full, prompt_file, report), cwd=cfg.cwd, env=env,
                                    stdin=subprocess.PIPE if engine.prompt == "stdin" else subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, **kw)
        except OSError as exc:
            return Attempt(n, engine.name, ERROR, None, 0.0, str(report), str(log), f"launch failed: {exc}")
        try:
            if engine.prompt == "stdin":
                try:
                    proc.stdin.write(full.encode("utf-8"))
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            kill_tree(proc, cfg.kill_grace)   # also sweeps stragglers after a clean exit
    secs = round(time.monotonic() - t0, 2)
    rc = proc.returncode
    tail = log.read_bytes()[-20000:].decode("utf-8", errors="replace")
    rstatus, rdetail = check_report(report, cfg.required_lines)
    if timed_out:
        status, detail = TIMEOUT, f"killed after {limit:g}s wall clock"
    elif rstatus == OK and rc == 0:
        status, detail = OK, ""
    elif any(re.search(p, tail) for p in cfg.quota_patterns):
        status, detail = QUOTA, "output matched a quota / rate-limit pattern"
    elif rc != 0:
        status, detail = ERROR, f"exit code {rc}"
    else:
        status, detail = rstatus, rdetail
    return Attempt(n, engine.name, status, rc, secs, str(report), str(log), detail)


def run_brief(brief: str | Path, cfg: Config, job: str | None = None, force: bool = False,
              start_engine: str | None = None, echo=None) -> RunResult:
    """Run a brief file end to end. Never raises for agent failures; inspect RunResult.status."""
    say = echo or (lambda *_: None)
    path = Path(brief)
    text = path.read_text(encoding="utf-8")
    h = brief_hash(text)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    state = Path(cfg.state_dir)
    ledger = Ledger(state / "ledger.jsonl")
    base = {"run_id": run_id, "brief": str(path), "hash": h, "job": job}

    if not force:
        done = ledger.recent_success(h, cfg.done_window)
        if done:
            why = f"same brief succeeded {int(time.time() - done['ts'])}s ago (run {done['run_id']}); --force to rerun"
            ledger.write("refused", **base, status=REFUSED_DONE, detail=why)
            return RunResult(run_id, REFUSED_DONE, str(path), h, job, detail=why)

    engines = cfg.engines
    if start_engine:
        idx = [e.name for e in engines].index(cfg.engine(start_engine).name)
        engines = engines[idx:]
    engines = engines[: max(1, cfg.max_engines)]
    worst = sum((e.timeout or cfg.timeout) + cfg.kill_grace + 5 for e in engines)
    deadline = time.time() + worst + 60
    lock_dir = state / "locks"
    held: list[locks.Lock] = []
    try:
        for name in [f"brief-{h}"] + ([f"job-{re.sub(r'[^A-Za-z0-9_.-]', '_', job)}"] if job else []):
            lk = locks.acquire(lock_dir / f"{name}.lock", dict(base), deadline)
            held.append(lk)
            if lk.reclaimed is not None:
                ledger.write("reclaimed", **base, lock=lk.path.name, stale_pid=lk.reclaimed.get("pid"),
                             stale_run=lk.reclaimed.get("run_id"))
                say(f"reclaimed stale lock {lk.path.name} (pid {lk.reclaimed.get('pid')} is gone)")
    except locks.LockHeld as exc:
        for lk in held:
            lk.release()
        why = f"already running: {exc}"
        ledger.write("refused", **base, status=REFUSED_RUNNING, detail=why)
        return RunResult(run_id, REFUSED_RUNNING, str(path), h, job, detail=why)

    result = RunResult(run_id, ERROR, str(path), h, job)
    run_dir = state / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    prev = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    except ValueError:
        prev = None                   # not the main thread: leave signals alone
    try:
        ledger.write("start", **base, engines=[e.name for e in engines])
        for n, engine in enumerate(engines, 1):
            say(f"attempt {n}: {engine.name} ...")
            a = run_attempt(cfg, engine, text, run_dir, n, {"BRIEF_RUN_ID": run_id})
            result.attempts.append(a)
            ledger.write("attempt", **base, attempt=n, engine=a.engine, status=a.status, rc=a.rc, secs=a.secs,
                         report=a.report, log=a.log, detail=a.detail)
            say(f"attempt {n}: {engine.name} -> {a.status} in {a.secs}s" + (f" ({a.detail})" if a.detail else ""))
            if a.status == OK:
                break
        last = result.attempts[-1]
        result.status, result.detail = last.status, last.detail
    except KeyboardInterrupt:
        result.status, result.detail = "interrupted", "brief-runner was interrupted"
        raise
    finally:
        ledger.write("run", **base, status=result.status, detail=result.detail,
                     engines=[a.engine for a in result.attempts], attempts=len(result.attempts),
                     report=result.report)
        for lk in held:
            lk.release()
        if prev is not None:
            signal.signal(signal.SIGTERM, prev)
    return result
