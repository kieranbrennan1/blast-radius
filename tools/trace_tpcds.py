"""Map each TPC-DS query of test_execute_tpcds to the sqlglot functions it runs, and time it.

The real test loads every table (setUpClass), parses every query in the parent,
pickles it to a spawned worker, runs the executor there on the queries marked
`execute`, and compares with duckdb on the query transpiled by sqlglot.

Here: "common" is what every run does (load and pickle the tables), traced once.
Then each query runs through standins.Tpcds.run_query in its own forked child
(fresh lazy state, so first-use set-up counts for every query: a superset). The
parent never starts duckdb or numpy threads, so forking is safe.

usage: python tools/trace_tpcds.py <sqlglot repo> <out.json>
"""

import json
import os
import pickle
import sys
import time

repo, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
os.environ["SKIP_INTEGRATION"] = "0"
for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[k] = "1"
sys.path.insert(0, repo)
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(repo)

import standins_tpcds as standins  # noqa: E402
import tests.test_executor  # noqa: E402,F401  (import-time code is not per query: the checker refuses changes to it)
import sqlglot.executor, sqlglot.optimizer, sqlglot.planner  # noqa: E402,F401,E401
from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA  # noqa: E402

PREFIX = repo + os.sep
TOOL = sys.monitoring.PROFILER_ID


def traced(fn):
    """Run fn in a forked child; return (sorted 'path::qualname' it started, cpu seconds, ok)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        seen = set()

        def on_start(code, offset):
            f = code.co_filename
            if f.startswith(PREFIX):
                seen.add(f[len(PREFIX) :] + "::" + code.co_qualname)
            return sys.monitoring.DISABLE

        ok = True
        t0 = time.process_time()
        sys.monitoring.use_tool_id(TOOL, "trace")
        sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, on_start)
        sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            ok = repr(e)[:300]
        sys.monitoring.set_events(TOOL, 0)
        cpu = time.process_time() - t0
        os.write(w, json.dumps([sorted(seen), cpu, ok]).encode())
        os._exit(0)
    os.close(w)
    data = b""
    while True:
        b = os.read(r, 1 << 20)
        if not b:
            break
        data += b
    os.waitpid(pid, 0)
    return json.loads(data)


T = standins.Tpcds


def common():
    # setUpClass reads every TPC-H and TPC-DS table into Table objects; the pool pickles them to workers
    for name in TPCDS_SCHEMA:
        T.table(name)
    pickle.loads(pickle.dumps((TPCDS_SCHEMA, T._tables)))
    T.sqls()


res = {"common": traced(common), "tpch_tables": list(TPCH_SCHEMA), "queries": []}
print(
    "common",
    len(res["common"][0]),
    round(res["common"][1], 2),
    res["common"][2],
    flush=True,
)
t0 = time.process_time()
for name in TPCDS_SCHEMA:
    T.table(name)  # untraced, so children do not reload them
print("tables loaded in", round(time.process_time() - t0, 2), "s", flush=True)
loads = {}
for name in TPCDS_SCHEMA:
    T._tables.pop(name)
    a = time.process_time()
    T.table(name)
    loads[name] = round(time.process_time() - a, 3)
res["table_load_cpu"] = loads
for i, (meta, sql, _) in enumerate(T.sqls()):
    funcs, cpu, ok = traced(lambda i=i: T(f"test_q{i}").run_query(i))
    res["queries"].append(
        {
            "i": i,
            "execute": bool(meta.get("execute")),
            "cpu": round(cpu, 3),
            "ok": ok,
            "funcs": funcs,
        }
    )
    print(
        i,
        bool(meta.get("execute")),
        round(cpu, 2),
        ok if ok is not True else "",
        len(funcs),
        flush=True,
    )
json.dump(res, open(out, "w"))
