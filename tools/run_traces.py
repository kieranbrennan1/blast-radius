"""Trace every test in tests.json, each in its own fresh process, in parallel.

usage: python3 tools/run_traces.py <sqlglot dir> <venv python> <tests.json> <out dir> [jobs]
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

repo, py, tests_json, out = (os.path.abspath(a) for a in sys.argv[1:5])
jobs = int(sys.argv[5]) if len(sys.argv) > 5 else os.cpu_count()
tools = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(tests_json))["tests"]
os.makedirs(out, exist_ok=True)
env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{repo}:{tools}", TRACE_ROOT=repo,
           PATH=os.path.dirname(py) + ":" + os.environ["PATH"], PYTHONDONTWRITEBYTECODE="1")


def run(t):
    dest = os.path.join(out, f"{t['number']}.json")
    if os.path.exists(dest):
        return t["number"], 0, 0
    start = time.time()
    p = subprocess.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", os.environ.get("TRACER", "tracer"), t["id"]],
                       cwd=repo, env=dict(env, TRACE_OUT=dest), capture_output=True, text=True)
    return t["number"], p.returncode, time.time() - start


done = 0
with ThreadPoolExecutor(jobs) as ex:
    for num, rc, secs in ex.map(run, sorted(tests, key=lambda t: -t["seconds"])):
        done += 1
        if rc != 0:
            print(f"test {num} exit {rc}", flush=True)
        if done % 100 == 0:
            print(f"{done}/{len(tests)}", flush=True)
print("done")
