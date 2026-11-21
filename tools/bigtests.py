"""For each commit, show expensive tests (>= 3 s) the selector picked and whether they really failed.

usage: python3 tools/bigtests.py <map> <data dir>
"""
import json
import os
import subprocess
import sys

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
mp, data = sys.argv[1:3]
env = dict(os.environ, BR_DATA=data, BR_REPO=os.path.join(here, "work", "sqlglot"), BR_MAP=mp)
p = subprocess.run([sys.executable, os.path.join(here, "selector.py")], env=env, capture_output=True, text=True)
notes = {ln.split(" ", 1)[0]: ln for ln in p.stderr.splitlines()}
tests = {t["number"]: t for t in json.load(open(os.path.join(data, "tests.json")))["tests"]}
ans = json.load(open(os.path.join(data, "answers.json")))["commits"]
waste = 0.0
for line in p.stdout.splitlines():
    d = json.loads(line)
    chosen = {t for w in d["workers"] for t in w}
    fail = set(ans[d["commit"]]["failing"])
    big = [t for t in chosen if tests[t]["seconds"] >= 3]
    if big:
        tags = [f"{tests[t]['id'].split('::')[-1]}({tests[t]['seconds']:.0f}s,{'FAIL' if t in fail else 'pass'})" for t in big]
        if not any(t in fail for t in big):
            waste += max(tests[t]["seconds"] for t in big)
        print(d["commit"], ans[d["commit"]]["kind"], " ".join(tags), "|", notes.get(d["commit"], "")[:150])
print(f"seconds of big tests picked where none failed: ~{waste:.0f}")
