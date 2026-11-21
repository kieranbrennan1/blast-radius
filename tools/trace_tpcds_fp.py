"""One full real run of test_execute_tpcds, inline, sorting every sqlglot function by stage.

Stages, as the real test runs them:
  C  compile: parse_one + pickle round trip, find_tables, ensure_schema, optimize, Plan, DuckDB transpile
  G  code generation: PythonExecutor.generate (subquery compilation + Python generator)
  E  everything else: loading and pickling tables, fixture loading, running steps over rows
A changed function is safe for the fingerprint check (standins_tpcds_fp.py) only if it never runs in E.
Also records, per query, every input PythonExecutor.generate got (pickled expression + current CTEs),
and the code it produced, so the replay can be checked against the real run.

usage: python tools/trace_tpcds_fp.py <sqlglot repo> <out dir>
writes <out>/tpcds_fp.json {"cg": [...], "e": [...]} and <out>/tpcds_gen.pkl.gz, <out>/tpcds_gen_out.pkl.gz
"""

import gzip
import json
import os
import pickle
import sys
import time

repo, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, repo)
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(repo)

import standins_tpcds as standins  # noqa: E402
import sqlglot.executor as sx  # noqa: E402
from sqlglot import find_tables, parse_one, transpile  # noqa: E402
from sqlglot.executor import python as py  # noqa: E402
from tests.helpers import TPCDS_SCHEMA  # noqa: E402

PREFIX = repo + os.sep
names: dict = {}
SEEN = {"C": set(), "G": set(), "E": set()}
PHASE = ["E"]


def on_start(code, offset):
    n = names.get(code)
    if n is None:
        f = code.co_filename
        n = names[code] = (
            (f[len(PREFIX) :] + "::" + code.co_qualname)
            if f.startswith(PREFIX + "sqlglot")
            else ""
        )
    if n:
        SEEN[PHASE[-1]].add(n)


def phase(p, fn):
    def run(*a, **k):
        PHASE.append(p)
        try:
            return fn(*a, **k)
        finally:
            PHASE.pop()

    return run


# compile stages inside execute()
for nm in ("optimize", "Plan", "ensure_schema"):
    setattr(sx, nm, phase("C", getattr(sx, nm)))

REC: list = []
OUT: list = []
_orig_generate = py.PythonExecutor.generate


def generate(self, expression):
    REC.append((pickle.dumps(expression), pickle.dumps(self._ctes)))
    PHASE.append("G")
    try:
        if not expression:
            OUT.append(None)
            return None
        e2 = self._replace_subqueries(expression)
        src = self.generator.generate(e2)
        OUT.append((src, dict(self._plan_names_by_sql)))
        return compile(src, src, "eval", optimize=2)
    finally:
        PHASE.pop()


py.PythonExecutor.generate = generate

TOOL = sys.monitoring.PROFILER_ID
sys.monitoring.use_tool_id(TOOL, "trace")
sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, on_start)
sys.monitoring.set_events(TOOL, sys.monitoring.events.PY_START)

t0 = time.time()
T = standins.Tpcds
for name in TPCDS_SCHEMA:
    T.table(name)
tables_all = pickle.loads(pickle.dumps(T._tables))
sqls = T.sqls()
print("loaded", round(time.time() - t0, 1), "s", flush=True)

gen, gen_out, bad = {}, {}, []
for i, (meta, sql, _) in enumerate(sqls):
    PHASE.append("C")
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    PHASE.pop()
    if not meta.get("execute"):
        continue
    PHASE.append("C")
    tnames = [t.name for t in find_tables(e)]
    PHASE.pop()
    REC.clear()
    OUT.clear()
    try:
        sx.execute(e, schema=TPCDS_SCHEMA, tables={n: tables_all[n] for n in tnames})
    except Exception as ex:  # noqa: BLE001
        bad.append((i, repr(ex)[:200]))
    PHASE.append("C")
    transpile(sql, write="duckdb")
    PHASE.pop()
    gen[i] = list(REC)
    gen_out[i] = list(OUT)
    print(i, len(REC), round(time.time() - t0, 1), flush=True)

sys.monitoring.set_events(TOOL, 0)
cg = (SEEN["C"] | SEEN["G"]) - SEEN["E"]
os.makedirs(out, exist_ok=True)
json.dump(
    {
        "cg": sorted(cg),
        "e": sorted(SEEN["E"]),
        "both": sorted((SEEN["C"] | SEEN["G"]) & SEEN["E"]),
        "bad": bad,
    },
    open(os.path.join(out, "tpcds_fp.json"), "w"),
    indent=0,
)
with gzip.open(os.path.join(out, "tpcds_gen.pkl.gz"), "wb") as f:
    pickle.dump(gen, f)
with gzip.open(os.path.join(out, "tpcds_gen_out.pkl.gz"), "wb") as f:
    pickle.dump(gen_out, f)
print(
    "cg",
    len(cg),
    "e",
    len(SEEN["E"]),
    "both",
    len((SEEN["C"] | SEEN["G"]) & SEEN["E"]),
    "bad",
    bad,
    flush=True,
)
