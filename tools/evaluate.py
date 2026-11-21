"""Run the selector locally on the sample and show cost and misses per commit.

usage: python3 tools/evaluate.py [map path] [data dir with commits/, tests.json, answers.json]
"""
import json
import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
mp = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "work", "map.json.gz")
data = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, "sample")
env = dict(os.environ, BR_DATA=data, BR_REPO=os.path.join(here, "work", "sqlglot"), BR_MAP=mp)
t0 = time.process_time()
start = time.time()
p = subprocess.run([sys.executable, os.path.join(here, "selector.py")], env=env, capture_output=True, text=True)
wall = time.time() - start
if p.returncode:
    print(p.stderr)
    sys.exit(1)
notes = {ln.split(" ", 1)[0]: ln for ln in p.stderr.splitlines()}
tests = {t["number"]: t for t in json.load(open(os.path.join(data, "tests.json")))["tests"]}
ans = json.load(open(os.path.join(data, "answers.json")))["commits"]
total = misses = 0
for line in p.stdout.splitlines():
    d = json.loads(line)
    ws = [w for w in d["workers"] if w]
    chosen = {t for w in ws for t in w}
    cost = (max(sum(round(tests[t]["seconds"] * 1000) for t in w) for w in ws) + 2000 * len(ws)) / 1000 if ws else 0
    fail = set(ans[d["commit"]]["failing"])
    miss = fail - chosen
    total += cost + (300 if miss else 0)
    misses += bool(miss)
    flag = "MISS " + ",".join(tests[t]["id"].split("::")[-1] for t in miss) if miss else ""
    print(f"{d['commit']} {ans[d['commit']]['kind']:8} fail={len(fail):3} chose={len(chosen):5} cost={cost:7.2f} {flag}")
    print("     ", notes.get(d["commit"], "")[5:][:220])
print(f"TOTAL simulated {total:.1f} s, misses {misses}, selector wall {wall:.2f} s")
