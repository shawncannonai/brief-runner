"""A stand-in agent CLI for tests. Usage: fake_engine.py MODE [PIDFILE]

Reads the prompt on stdin, reports via $BRIEF_REPORT. Modes:
  ok        write a valid report, exit 0
  noreport  print output, write nothing, exit 0
  badreport write a report without the RESULT line
  quota     print a rate-limit message, exit 1
  hang      spawn a sleeping child (pid written to PIDFILE), then sleep forever
"""
import os
import subprocess
import sys
import time

mode = sys.argv[1]
prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
report = os.environ["BRIEF_REPORT"]

if mode == "ok":
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"RESULT: done by {os.environ.get('BRIEF_ENGINE')} ({len(prompt)} chars of brief)\n")
    print("working... done")
elif mode == "noreport":
    print("I did everything, trust me")
elif mode == "badreport":
    with open(report, "w", encoding="utf-8") as f:
        f.write("all good\n")
elif mode == "quota":
    print("Error: usage limit reached, try again later", file=sys.stderr)
    sys.exit(1)
elif mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w") as f:
            f.write(str(child.pid))
    print(f"working (child pid {child.pid}) ...", flush=True)
    time.sleep(60)
