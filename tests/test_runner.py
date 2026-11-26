import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief_runner import Engine, Config, Ledger, brief_hash, run_brief  # noqa: E402
from brief_runner import cli  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_engine.py"))


def eng(name, mode, *extra):
    return Engine(name, [sys.executable, FAKE, mode, *extra], "stdin")


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.brief = self.d / "fix-tests.md"
        self.brief.write_text("# Fix the flaky test\nMake tests/test_api.py pass.\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, *engines, **kw):
        kw.setdefault("timeout", 10)
        kw.setdefault("kill_grace", 0.2)
        return Config(engines=list(engines), state_dir=self.d / "state", **kw)

    def ledger(self):
        return Ledger(self.d / "state" / "ledger.jsonl")


class TestContract(Base):
    def test_success_writes_report_and_ledger(self):
        r = run_brief(self.brief, self.cfg(eng("a", "ok")))
        self.assertEqual(r.status, "ok")
        self.assertIn("RESULT: done by a", Path(r.report).read_text())
        runs = self.ledger().runs()
        self.assertEqual([x["status"] for x in runs], ["ok"])
        self.assertEqual(runs[0]["hash"], brief_hash(self.brief.read_text()))
        # the brief and the contract both reached the agent
        prompt = (self.d / "state" / "runs" / r.run_id / "prompt-1.md").read_text()
        self.assertIn("Fix the flaky test", prompt)
        self.assertIn("REPORT CONTRACT", prompt)

    def test_missing_report_is_failure(self):
        r = run_brief(self.brief, self.cfg(eng("a", "noreport"), max_engines=1))
        self.assertEqual(r.status, "no_report")
        self.assertFalse(r.ok)
        self.assertIsNone(r.report)

    def test_report_without_required_line_is_failure(self):
        r = run_brief(self.brief, self.cfg(eng("a", "badreport"), max_engines=1))
        self.assertEqual(r.status, "bad_report")
        self.assertIn("RESULT:", r.detail)


class TestTimeout(Base):
    def test_timeout_kills_whole_tree(self):
        pidfile = self.d / "child.pid"
        t0 = time.monotonic()
        r = run_brief(self.brief, self.cfg(eng("a", "hang", str(pidfile)), timeout=0.8, max_engines=1))
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(r.status, "timeout")
        child = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                os.kill(child, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                alive = False
        self.assertFalse(alive, "grandchild survived the timeout kill")


class TestReroute(Base):
    def test_quota_reroutes_to_next_engine(self):
        r = run_brief(self.brief, self.cfg(eng("first", "quota"), eng("second", "ok")))
        self.assertEqual(r.status, "ok")
        self.assertEqual([(a.engine, a.status) for a in r.attempts], [("first", "quota"), ("second", "ok")])
        logged = [(x["engine"], x["status"]) for x in self.ledger().attempts(r.run_id)]
        self.assertEqual(logged, [("first", "quota"), ("second", "ok")])
        self.assertIn("RESULT: done by second", Path(r.report).read_text())

    def test_chain_capped_by_max_engines(self):
        r = run_brief(self.brief, self.cfg(eng("a", "noreport"), eng("b", "quota"), eng("c", "ok"), max_engines=2))
        self.assertEqual(r.status, "quota")
        self.assertEqual([a.engine for a in r.attempts], ["a", "b"])

    def test_missing_binary_reroutes(self):
        r = run_brief(self.brief, self.cfg(Engine("ghost", ["no-such-agent-cli-xyz"]), eng("b", "ok")))
        self.assertEqual([a.status for a in r.attempts], ["error", "ok"])


class TestDedupe(Base):
    def _lock(self, name, pid, **kw):
        p = self.d / "state" / "locks" / f"{name}.lock"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"pid": pid, "host": socket.gethostname(), "run_id": "other", "deadline": time.time() + 3600, **kw}
        p.write_text(json.dumps(rec))
        return p

    def test_duplicate_refused_while_running(self):
        h = brief_hash(self.brief.read_text())
        lock = self._lock(f"brief-{h}", os.getpid())       # a live owner
        r = run_brief(self.brief, self.cfg(eng("a", "ok")))
        self.assertEqual(r.status, "refused_running")
        self.assertEqual(r.attempts, [])
        self.assertTrue(lock.exists(), "a refused run must not touch the owner's lock")

    def test_same_job_name_refused_even_with_different_brief(self):
        self._lock("job-nightly", os.getpid())
        r = run_brief(self.brief, self.cfg(eng("a", "ok")), job="nightly")
        self.assertEqual(r.status, "refused_running")
        self.assertFalse(list((self.d / "state" / "locks").glob("brief-*.lock")), "partial lock leaked")

    def test_stale_lock_reclaimed(self):
        h = brief_hash(self.brief.read_text())
        gone = dead_pid()
        self._lock(f"brief-{h}", gone)
        r = run_brief(self.brief, self.cfg(eng("a", "ok")))
        self.assertEqual(r.status, "ok")
        rec = [x for x in self.ledger().rows() if x["event"] == "reclaimed"]
        self.assertEqual(rec[0]["stale_pid"], gone)
        self.assertFalse(list((self.d / "state" / "locks").glob("*.lock")), "lock not released after run")

    def test_expired_deadline_reclaimed_even_if_pid_alive(self):
        h = brief_hash(self.brief.read_text())
        self._lock(f"brief-{h}", os.getpid(), deadline=time.time() - 1)
        self.assertEqual(run_brief(self.brief, self.cfg(eng("a", "ok"))).status, "ok")

    def test_recent_done_window(self):
        cfg = self.cfg(eng("a", "ok"), done_window=3600)
        self.assertEqual(run_brief(self.brief, cfg).status, "ok")
        self.assertEqual(run_brief(self.brief, cfg).status, "refused_done")
        self.assertEqual(run_brief(self.brief, cfg, force=True).status, "ok")
        self.brief.write_text(self.brief.read_text() + "\nAlso update the changelog.\n")
        self.assertEqual(run_brief(self.brief, cfg).status, "ok", "an edited brief is a new brief")

    def test_failed_run_is_not_done(self):
        cfg = self.cfg(eng("a", "noreport"), max_engines=1)
        run_brief(self.brief, cfg)
        self.assertEqual(run_brief(self.brief, cfg).status, "no_report")


class TestCli(Base):
    def test_run_status_list(self):
        conf = self.d / "brief-runner.json"
        conf.write_text(json.dumps({"state_dir": "state", "kill_grace": 0.2, "engines": [
            {"name": "flaky", "cmd": [sys.executable, FAKE, "quota"]},
            {"name": "backup", "cmd": [sys.executable, FAKE, "ok"]}]}))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(conf), "run", "-q", str(self.brief)]), 0)
            self.assertEqual(cli.main(["--config", str(conf), "run", "-q", str(self.brief)]), 4)
            cli.main(["--config", str(conf), "list"])
            cli.main(["--config", str(conf), "status", self.ledger().runs()[0]["run_id"]])
        text = out.getvalue()
        self.assertIn("refused_done", text)
        self.assertIn("flaky>backup", text)
        self.assertIn("#1 flaky", text)
        self.assertIn("#2 backup", text)


if __name__ == "__main__":
    unittest.main()
