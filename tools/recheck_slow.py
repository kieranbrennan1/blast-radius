"""Re-run given tests (no timeout) on each patched commit and fix answers.json where a timeout faked a failure.

usage: python3 tools/recheck_slow.py <mut dir> <venv python> <commit id> [...]
"""
import json, os, shutil, subprocess, sys, tempfile
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, here)
import selector

out, py, *cids = sys.argv[1:]
SLOW = ["tests/test_executor.py::TestExecutor::test_execute_tpcds", "tests/test_parser.py::TestParser::test_max_nodes"]
tests = {t["id"]: t["number"] for t in json.load(open(os.path.join(out, "tests.json")))["tests"]}
ap = os.path.join(out, "answers.json")
for cid in cids:
    work = tempfile.mkdtemp(prefix="slow")
    root = os.path.join(work, "repo")
    shutil.copytree(os.path.join(here, "work", "sqlglot"), root, ignore=shutil.ignore_patterns(".git"))
    for f in selector.parse_patch(open(os.path.join(out, "commits", cid + ".patch")).read()):
        p = os.path.join(root, f["old"] or f["new"])
        old = open(p).read()  # read first: open(p, "w") empties the file
        assert selector.hunks_match(old, f["hunks"]), f"patch does not apply to {p}"
        new = selector.apply_hunks(old, f["hunks"])
        with open(p, "w") as fh:
            fh.write(new)
    for tid in SLOW:
        if tid not in tests:
            continue
        for attempt in range(3):  # a crash under memory pressure is not a failure: try again
            r = subprocess.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:timeout", tid], cwd=root,
                               env=dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=root),
                               capture_output=True, text=True)
            passed = r.returncode == 0
            if passed or r.returncode == 1 and " failed" in r.stdout:
                break
        if not passed:
            print("  rc", r.returncode, (r.stdout + r.stderr).strip().splitlines()[-3:], flush=True)
        a = json.load(open(ap))
        fl = set(a["commits"][cid]["failing"])
        n = tests[tid]
        if passed and n in fl:
            fl.discard(n)
            a["commits"][cid]["failing"] = sorted(fl)
            json.dump(a, open(ap, "w"), indent=1)
        print(cid, tid.split("::")[-1], "pass" if passed else "FAIL", flush=True)
    shutil.rmtree(work, ignore_errors=True)
