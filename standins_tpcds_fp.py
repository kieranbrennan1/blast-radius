"""Proof stand-in for test_execute_tpcds (21.5 s): no DuckDB, no data, no execution.

test_execute_tpcds compares, per query, DuckDB's answer on transpile(sql, "duckdb") with sqlglot's
Python executor run on the optimized plan. Both answers are fixed by what sqlglot builds *before* any
row is touched: the parsed (and pickled) query, the tables it names, the optimized query, the plan, the
DuckDB SQL, and the Python code the executor generates for each step. So when a change only touches
functions that run in those compile stages (tools/trace_tpcds_fp.py lists them, and lists every function
that runs while rows are processed), equal fingerprints prove the test still passes.

The baseline is made in the Docker build from the pinned sqlglot (`python standins_tpcds_fp.py build
<out>`); the checker runs TpcdsPrint.test_q<i> on the patched code and fails on any difference.
Code-generation inputs (map/tpcds_gen.pkl.gz) come from one full real run of the test at eb11aec.
Idea seen in several public entries (front-end / plan fingerprints); this implementation is our own.
"""

import gzip
import hashlib
import os
import pickle
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
GEN = [HERE / "map" / "tpcds_gen.pkl.gz", Path("/app/tpcds_gen.pkl.gz")]
BASE = [
    Path(os.environ.get("BR_TPCDS_FP_BASE", "/app/tpcds_fp_base.pkl")),
    HERE / "work" / "tpcds_fp_base.pkl",
]


def _first(paths):
    for p in paths:
        if p.exists():
            return p
    return None


def dump(x, seen=None):
    """A complete, deterministic structural dump (types, args, comments, meta, annotated types)."""
    from sqlglot import exp

    if isinstance(x, exp.Expr if hasattr(exp, "Expr") else exp.Expression):
        return (
            type(x).__name__,
            # the parser fills args in set order (hash seed), so sort by name
            tuple(sorted((k, dump(v)) for k, v in x.args.items())),
            tuple(x.comments or ()),
            dump(getattr(x, "_type", None)),
            repr(
                sorted((getattr(x, "_meta", None) or {}).items(), key=lambda kv: kv[0])
            ),
        )
    if isinstance(x, (list, tuple)):
        return (type(x).__name__, tuple(dump(v) for v in x))
    if isinstance(x, dict):
        return ("dict", tuple(sorted((repr(k), dump(v)) for k, v in x.items())))
    if isinstance(x, (set, frozenset)):
        return ("set", tuple(sorted(repr(dump(v)) for v in x)))
    if type(x).__module__.startswith("sqlglot.planner"):
        return ("step", _step(x, {}))
    return (type(x).__name__, repr(x))


def _step(step, memo):
    """Canonical id of a plan step: its own fields plus its dependencies, never id()."""
    key = id(step)
    if key in memo:
        return memo[key]
    memo[key] = "<cycle>"
    fields = []
    for k, v in sorted(vars(step).items()):
        if k in ("dependencies", "dependents"):
            continue
        if type(v).__module__.startswith("sqlglot.planner"):
            fields.append((k, _step(v, memo)))
        elif isinstance(v, dict) and any(
            type(s).__module__.startswith("sqlglot.planner") for s in v.values()
        ):
            fields.append((k, tuple((repr(a), _step(b, memo)) for a, b in v.items())))
        else:
            fields.append((k, dump(v)))
    deps = tuple(sorted(_step(d, memo) for d in step.dependencies))
    h = hashlib.sha256(repr((type(step).__name__, fields, deps)).encode()).hexdigest()
    memo[key] = h
    return h


def plan_dump(plan):
    return (
        _step(plan.root, {}),
        tuple(sorted(repr(k) for k in (plan.ctes or {})))
        if isinstance(plan.ctes, dict)
        else repr(dump(plan.ctes)),
    )


_SQ = re.compile(r"_sq_\d+")


def fingerprint(i, sqls, gen):
    """sha256 of everything test_execute_tpcds builds for query i before touching rows."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import python as py
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA

    meta, sql, _ = sqls[i]
    e = pickle.loads(
        pickle.dumps(parse_one(sql))
    )  # the test ships the parsed query to a worker
    parts = [dump(e), repr(sorted(meta.items()))]
    if meta.get("execute"):
        parts.append(tuple(sorted(t.name for t in find_tables(e))))
        schema = ensure_schema(TPCDS_SCHEMA, dialect=None)
        opt = optimize(e, schema, leave_tables_isolated=True, dialect=None)
        parts.append(dump(opt))
        parts.append(plan_dump(Plan(opt)))
        parts.append(transpile(sql, write="duckdb")[0])
        ex = py.PythonExecutor(tables={})
        outs, ctes, exprs = [], {}, {}
        for raw_expr, raw_ctes in gen.get(i, ()):
            ex._ctes = ctes.get(raw_ctes) or ctes.setdefault(raw_ctes, pickle.loads(raw_ctes))  # one object per query, like plan.ctes
            x = exprs.get(raw_expr) or exprs.setdefault(raw_expr, pickle.loads(raw_expr))  # generate never mutates its input
            if not x:
                outs.append(None)
                continue
            src = ex.generator.generate(ex._replace_subqueries(x))
            outs.append(src)
        names = {name: sql_ for sql_, name in ex._plan_names_by_sql.items()}

        def norm(s):
            return (
                None
                if s is None
                else _SQ.sub(
                    lambda m: (
                        "SQ<"
                        + hashlib.sha256(
                            names.get(m.group(0), m.group(0)).encode()
                        ).hexdigest()[:16]
                        + ">"
                    ),
                    s,
                )
            )

        parts.append(
            tuple(sorted(repr(norm(s)) for s in outs))
        )  # the executor visits steps in set order
        parts.append(
            tuple(
                sorted(
                    (names.get(n, n), plan_dump(p))
                    for n, (p, _cache) in ex._subquery_plans.items()
                )
            )
        )
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def _sqls():
    from tests.helpers import load_sql_fixture_pairs

    return list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))


def _gen():
    p = _first(GEN)
    if p is None:
        return None
    with gzip.open(p, "rb") as f:
        d = pickle.load(f)  # deduplicated: {"exprs": [pickle], "ctes": [pickle], "calls": {i: [(e, c)]}}
    ex, ct = d["exprs"], d["ctes"]
    return {i: [(ex[e], ct[c]) for e, c in calls] for i, calls in d["calls"].items()}


class TpcdsPrint(unittest.TestCase):
    """test_q<i>: query i's fingerprint on the patched code equals the pinned baseline."""

    _ctx = None

    @classmethod
    def ctx(cls):
        if cls._ctx is None:
            b, g = _first(BASE), _gen()
            if b is None or g is None:
                raise RuntimeError("tpcds fingerprint baseline or inputs missing")
            cls._ctx = (pickle.loads(b.read_bytes()), g, _sqls())
        return cls._ctx

    def check(self, i):
        base, gen, sqls = self.ctx()
        self.assertIn(i, base)
        self.assertEqual(fingerprint(i, sqls, gen), base[i])


for _i in range(120):
    setattr(TpcdsPrint, f"test_q{_i}", lambda self, _i=_i: self.check(_i))


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "build":
    # python standins_tpcds_fp.py build <out.pkl>   (cwd = the pinned sqlglot checkout, on sys.path)
    sys.path.insert(0, os.getcwd())
    gen, sqls = _gen(), _sqls()
    base = {i: fingerprint(i, sqls, gen) for i in range(len(sqls))}
    Path(sys.argv[2]).write_bytes(pickle.dumps(base))
    print("tpcds fingerprints:", len(base), file=sys.stderr)
