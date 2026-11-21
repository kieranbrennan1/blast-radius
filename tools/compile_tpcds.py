"""Turn tools/trace_tpcds.py output into map/tpcds.json for the selector.

usage: python tools/compile_tpcds.py <sqlglot repo> <trace.json> <out map/tpcds.json> [cpu scale]

Times are CPU ms on the machine that traced, times `scale` (default 1.5, for a slower
scorer). A query's time excludes the library imports every child paid while tracing
(the cheapest query that does not execute shows that cost).
"""

import json
import os
import sys

repo, trace, out = (os.path.abspath(p) for p in sys.argv[1:4])
scale = float(sys.argv[4]) if len(sys.argv) > 4 else 1.5
sys.path.insert(0, os.path.abspath(repo))
os.environ["SKIP_INTEGRATION"] = "0"
os.chdir(repo)

from sqlglot import find_tables, parse_one  # noqa: E402
from tests.helpers import load_sql_fixture_pairs  # noqa: E402

t = json.load(open(trace))
sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
assert len(sqls) == len(t["queries"]), "trace is for another fixture file"
bad = [q["i"] for q in t["queries"] if q["ok"] is not True]
assert not bad, (
    f"queries failed while tracing (stand-in differs from the real test?): {bad}"
)
assert t["common"][2] is True, t["common"][2]

names: dict = {}


def idx(f):
    if f not in names:
        names[f] = len(names)
    return names[f]


common = sorted(idx(f) for f in t["common"][0])
imports = min(q["cpu"] for q in t["queries"] if not q["execute"])
queries = []
for q in t["queries"]:
    meta, sql, _ = sqls[q["i"]]
    tables = (
        sorted({x.name for x in find_tables(parse_one(sql))}) if q["execute"] else []
    )
    ms = max(20, round(1000 * scale * (q["cpu"] - imports))) + 20
    queries.append(
        {
            "i": q["i"],
            "ms": ms,
            "funcs": sorted(idx(f) for f in q["funcs"]),
            "tables": tables,
        }
    )
funcs = [None] * len(names)
for f, i in names.items():
    funcs[i] = f
res = {
    "funcs": funcs,
    "common": common,
    "queries": queries,
    "table_ms": {
        k: round(1000 * scale * v) + 5 for k, v in t["table_load_cpu"].items()
    },
    "base_ms": round(300 * scale),
}
json.dump(res, open(out, "w"), separators=(",", ":"))
tot = sum(q["ms"] for q in queries)
print(
    f"{len(queries)} queries, {len(funcs)} functions, {len(common)} common, all queries {tot} ms, import {imports:.2f} s"
)
