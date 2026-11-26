# brief-runner

**One fresh agent per brief, no duplicate work.**

A brief is a markdown file. `brief-runner run brief.md` starts one fresh headless agent process for it (`claude -p`,
`codex exec`, any CLI that takes a prompt), and does what a shell loop around `claude -p` does not:

- **dedupe**: the same brief (by content hash) or the same `--job` name cannot run twice at once, and a brief that
  just succeeded is not re-run. Locks left by a crashed runner are reclaimed automatically.
- **hard wall-clock limit**: on timeout the agent *and everything it spawned* is killed.
- **report contract**: the agent must write a report file with required lines (e.g. `RESULT:`). No report, empty
  report, or missing lines = failed run, however confident stdout sounds.
- **reroute**: on failure, timeout, or output matching quota / rate-limit patterns, the brief goes to the next engine
  in the chain. Every attempt is logged.
- **ledger**: append-only JSONL, plus `status` and `list` commands.

Stdlib only, Python 3.10+, about 600 lines.

## Why

Running agents headless from cron, a queue or a chat bot fails in a few boring, expensive ways, all learned the hard
way:

1. **Twins.** A retry, a double-tap, or a scheduler firing while the last run is still going starts the same brief
   twice. Two agents edit the same files and overwrite each other's work, and you pay for both.
2. **Re-doing finished work.** The brief landed ten minutes ago; something relaunches it anyway.
3. **Silent "success".** The process exits 0 after hitting a usage cap three seconds in, or after printing "Done!"
   without doing anything. Exit codes from agent CLIs do not mean the work happened.
4. **Hangs and orphans.** An agent waits forever on a tool call; killing the parent leaves its subprocesses (a dev
   server, a test runner, a browser) running.
5. **One provider's cap stops everything.** When the primary engine is rate-limited the job should move to the next
   engine, not die.

brief-runner is the small, boring wrapper that makes each of those a rule instead of a hope.

## Install

```sh
pip install git+https://github.com/your-github-org/brief-runner
# or just copy brief_runner/ next to your scripts: python3 -m brief_runner ...
```

## Example session

This is real output, using `examples/demo.json`: a primary engine that hits a usage limit and a backup that does
the work (both are `tests/fake_engine.py`, so you can run it without any API account).

```console
$ export BRIEF_RUNNER_CONFIG=examples/demo.json
$ python3 -m brief_runner run examples/fix-flaky-test.md --job flaky-test
brief-runner: attempt 1: primary ...
brief-runner: attempt 1: primary -> quota in 0.12s (output matched a quota / rate-limit pattern)
brief-runner: attempt 2: backup ...
brief-runner: attempt 2: backup -> ok in 0.08s
ok run=20261126-041715-559fe5 attempts=2 report=/tmp/br_demo/state/runs/20261126-041715-559fe5/report-2.md

$ python3 -m brief_runner run examples/fix-flaky-test.md          # same brief again
refused_done: same brief succeeded 0s ago (run 20261126-041715-559fe5); --force to rerun
$ echo $?
4
```

Now an agent that hangs (`examples/demo-slow.json`, 3 s limit). While it runs, a second launch is refused:

```console
$ export BRIEF_RUNNER_CONFIG=examples/demo-slow.json
$ python3 -m brief_runner run examples/fix-flaky-test.md --force &
$ python3 -m brief_runner status
running: 1
  brief-7155cb482729.lock      pid=2508925 run=20261126-041715-b7047f brief=examples/fix-flaky-test.md  up 0s
last: 20261126-041715-559fe5 ok 1s ago  examples/fix-flaky-test.md
$ python3 -m brief_runner run examples/fix-flaky-test.md --force
refused_running: already running: brief-7155cb482729.lock held by pid 2508925 (run 20261126-041715-b7047f)
$ echo $?
3
brief-runner: attempt 1: stuck ...
brief-runner: attempt 1: stuck -> timeout in 3.05s (killed after 3s wall clock)
timeout run=20261126-041715-b7047f attempts=1 (killed after 3s wall clock)

$ python3 -m brief_runner list
20261126-041715-559fe5  ok              primary>backup         3s ago  examples/fix-flaky-test.md  [flaky-test]
20261126-041715-ce4858  refused_done    -                      3s ago  examples/fix-flaky-test.md
20261126-041716-050a29  refused_running -                      2s ago  examples/fix-flaky-test.md
20261126-041715-b7047f  timeout         stuck                  0s ago  examples/fix-flaky-test.md

$ python3 -m brief_runner status 20261126-041715-559fe5
run 20261126-041715-559fe5  ok  brief=examples/fix-flaky-test.md  hash=7155cb482729  job=flaky-test
  #1 primary    quota      rc=1 0.12s  output matched a quota / rate-limit pattern
  #2 backup     ok         rc=0 0.08s
  report: /tmp/br_demo/state/runs/20261126-041715-559fe5/report-2.md
```

The hung agent had started a child process; it was killed with the agent (the test suite asserts this).

Exit codes: `0` ok, `1` failed (all attempts), `3` refused (already running), `4` refused (recently done).

## Config

TOML (Python 3.11+) or JSON. Looked up in order: `--config`, `$BRIEF_RUNNER_CONFIG`, `./brief-runner.toml`,
`./brief-runner.json`. With no config, a single `claude -p` engine is used. See
[`examples/brief-runner.toml`](examples/brief-runner.toml):

```toml
state_dir = "state"          # relative to the config file; $BRIEF_RUNNER_STATE overrides
timeout = 1800               # wall-clock seconds per attempt
kill_grace = 5               # SIGTERM, then SIGKILL
max_engines = 3              # attempts per run
done_window = 3600           # refuse a brief that succeeded this recently (0 = off)
required_lines = ["RESULT:", "FILES:"]
quota_patterns = ["(?i)rate[ _-]?limit", "(?i)usage limit", "(?i)quota", "\\b429\\b"]

[[engines]]
name = "claude"
cmd = ["claude", "-p", "--permission-mode", "acceptEdits"]
prompt = "stdin"             # stdin | arg | file

[[engines]]
name = "codex"
cmd = ["codex", "exec", "--full-auto"]
prompt = "arg"
timeout = 2400               # per-engine override
```

`{report}` in any `cmd` element is replaced with the report path. State defaults to `./state`, or
`$XDG_STATE_HOME/brief-runner` when that is set and the config names no `state_dir`.

## How it works

1. **Hash** the brief (sha256 of the stripped text, first 12 hex). Editing the brief makes it a new brief.
2. **Recent-done check**: if the ledger has an `ok` run with that hash inside `done_window`, refuse (`--force` skips).
3. **Lock**: create `locks/brief-<hash>.lock` (and `locks/job-<name>.lock` with `--job`) with `O_EXCL`. The lock holds
   pid, host and a deadline (sum of the engine timeouts plus slack). An existing lock is **stale** and reclaimed when
   its pid is dead on this host or its deadline has passed (covers pid reuse and crashed runners). Reclaims are logged.
4. **Launch** each attempt as a fresh process in its own session / process group. The prompt is the brief plus a short
   contract footer naming the report path; the path is also in `$BRIEF_REPORT` (with `$BRIEF_RUN_ID`,
   `$BRIEF_ATTEMPT`, `$BRIEF_ENGINE`). stdout and stderr go to `runs/<run_id>/attempt-<n>-<engine>.log`, never a pipe,
   so a chatty agent cannot deadlock the runner.
5. **Limit**: after `timeout` seconds the whole group gets SIGTERM, then SIGKILL after `kill_grace`. Stragglers left
   behind by an agent that exited normally are swept the same way.
6. **Classify**: `timeout` > `ok` (exit 0 and the report has every required line, each with content) > `quota` (log
   matched a pattern) > `error` (non-zero exit or failed to launch) > `no_report` / `bad_report`.
7. **Reroute**: anything but `ok` moves to the next engine, up to `max_engines`. Each attempt gets its own report path,
   so a half-written report from a failed attempt can never count for the next one.
8. **Ledger**: `state/ledger.jsonl` gets `start`, `attempt`, `run`, `refused` and `reclaimed` rows.

## Python API

```python
from brief_runner import load_config, run_brief

cfg = load_config("brief-runner.toml")
r = run_brief("briefs/fix-flaky-test.md", cfg, job="flaky-test")
if r.ok:
    print(open(r.report).read())
else:
    print(r.status, r.detail, [(a.engine, a.status) for a in r.attempts])
```

## Limits

- Dedupe is per state directory. Two machines sharing one over a network filesystem get `O_EXCL` semantics only as
  good as that filesystem; a lock from another host is only reclaimed after its deadline.
- The report contract proves the agent *claimed* a result in the right shape, not that the result is true. Put
  checks you can verify (test command output, file list) in `required_lines` and check them downstream.
- It runs the attempt in the foreground. To detach, wrap it in `nohup`, `systemd-run`, a queue worker or cron.
- Windows: process-tree kill uses `taskkill /T /F`; tested mainly on Linux and macOS.
- Agent CLIs get the permissions you give them in `cmd`. brief-runner is not a sandbox.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

A fake engine (`tests/fake_engine.py`) covers success, missing and malformed reports, timeout with a grandchild
process, quota reroute, chain cap, duplicate refused (by hash and by job), stale and expired locks reclaimed, and the
recent-done window. No network, about two seconds.

## Licence

MIT
