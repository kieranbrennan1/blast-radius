"""Check fastverify against truth: no test may pass in verify but fail for real.

usage: work/.venv/bin/python tools/verify_check.py <data dir> [<data dir> ...]
"""
import json, os, sys, time, resource
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, here)
os.environ.setdefault("BR_MAP", os.path.join(here, "work", "map2.json.gz"))
os.environ.setdefault("BR_REPO", os.path.join(here, "work", "sqlglot"))
import selector
from fastverify import Verifier

repo = os.environ["BR_REPO"]
m = selector.Map(selector.Path(os.environ["BR_MAP"]))
sel = selector.Selector(m)
v = Verifier(repo, graph=os.path.join(here, "work", "imports.json"))
bad = tot_ver = tot_eligible = 0
cpu_tests = rec_tests = 0.0
for data in sys.argv[1:]:
    tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    secs = {t["id"]: t["seconds"] for t in tests}
    ans = json.load(open(os.path.join(data, "answers.json")))["commits"]
    for cid in sorted(ans):
        text = open(os.path.join(data, "commits", cid + ".patch"), encoding="utf-8", errors="replace").read()
        chosen, notes = sel.select(text)
        ids = [m.ids[i] for i in chosen]
        files = {}
        for f in selector.parse_patch(text):
            p = f.get("old") or f.get("new")
            old = open(os.path.join(repo, f["old"]), encoding="utf-8").read() if f.get("old") and os.path.exists(os.path.join(repo, f["old"])) else None
            new = selector.apply_hunks(old, f["hunks"]) if (old is not None and f.get("new")) else None
            files[p] = (old, new)
        t0 = time.process_time(); c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        swaps = v.plan(files)
        reload = None
        if swaps is None:
            reload = v.reload_set(files)
            swaps = [] if reload is not None else None
            if reload is not None and not reload[0]:
                reload = None
        if swaps is None or not ids:
            print(f"{cid}: not eligible ({len(ids)} tests) {'; '.join(notes)[:120]}")
            continue
        tot_eligible += 1
        cheap = sorted([t for t in ids if secs[t] < 1.0], key=lambda t: secs[t])
        res = v.run(swaps, cheap, {t: max(10.0, 20 * secs[t]) for t in cheap}, stop_on_fail=False, reload=reload) or {}
        c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        used = time.process_time() - t0 + (c1.ru_utime + c1.ru_stime) - (c0.ru_utime + c0.ru_stime)
        fail = set(ans[cid]["failing"])
        danger = [t for t, ok in res.items() if ok and num[t] in fail]
        wrongfail = [t for t, ok in res.items() if not ok and num[t] not in fail]
        bad += len(danger)
        tot_ver += len(res)
        cpu_tests += used; rec_tests += sum(secs[t] for t in res)
        print(f"{cid}: reload={len(reload[1]) if reload else 0} swaps={len(swaps)} ran={len(res)}/{len(ids)} fail_v={sum(not o for o in res.values())} fail_true={len(fail)} cpu={used:.2f}s rec={sum(secs[t] for t in res):.2f}s DANGER={danger} extra_fail={wrongfail[:3]}")
print(f"eligible {tot_eligible}, verified tests {tot_ver}, DANGER {bad}, cpu {cpu_tests:.1f}s vs recorded {rec_tests:.1f}s")
