"""Stand-in for test_execute_tpcds (21.5 s): one test per TPC-DS query, same steps as the real test.

The run-time check runs only the queries that execute a changed function (map/tpcds.json,
made by tools/trace_tpcds.py). Kept apart from standins.py because it needs duckdb, numpy
and pandas (imported here, in the checking parent, like the real test module does).
"""

import ast
import csv
import gzip
import os
import pickle
import unittest

import duckdb  # noqa: F401
import numpy  # noqa: F401
import pandas  # noqa: F401


def _repo():
    import sqlglot

    return os.path.dirname(os.path.dirname(os.path.abspath(sqlglot.__file__)))


class Tpcds(unittest.TestCase):
    """One test per TPC-DS query; methods are added below (test_q0 ... test_q98)."""

    _sqls = None
    _tables: dict = {}
    _conn = None
    _cache: dict = {}

    @classmethod
    def sqls(cls):
        if cls._sqls is None:
            from tests.helpers import load_sql_fixture_pairs

            cls._sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
        return cls._sqls

    _views: set = set()
    _conn_pid = None

    @classmethod
    def view(cls, name):
        """A duckdb view over the table's file, like TestExecutor.setUpClass (one connection per process)."""
        import duckdb
        from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA

        if cls._conn is None or cls._conn_pid != os.getpid():
            cls._conn, cls._conn_pid, cls._views = duckdb.connect(), os.getpid(), set()
        if name not in cls._views:
            columns = TPCDS_SCHEMA[name]
            file_name = f"{FIXTURES_DIR}/optimizer/tpc-ds/{name}.csv.gz"
            cls._conn.execute(
                f"""
                CREATE VIEW {name} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
            )
            cls._views.add(name)
        return cls._conn

    @classmethod
    def table(cls, name):
        """Python rows of one table, read like TestExecutor.setUpClass does."""
        got = cls._tables.get(name)
        if got is not None:
            return got
        from sqlglot.executor.table import Table
        from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA

        columns = TPCDS_SCHEMA[name]
        file_name = f"{FIXTURES_DIR}/optimizer/tpc-ds/{name}.csv.gz"
        with open(file_name, "rb") as f:
            gz = f.read(2) == b"\x1f\x8b"
        fh = (
            gzip.open(file_name, "rt", newline="")
            if gz
            else open(file_name, encoding="utf-8", newline="")
        )
        with fh:
            reader = csv.reader(fh, delimiter="|")
            rows, ctypes = [], []
            next(reader)
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(
                    tuple(
                        None if (t is not str and v == "") else t(v)
                        for t, v in zip(ctypes, row)
                    )
                )
        got = cls._tables[name] = Table(columns=columns, rows=rows)
        return got

    def run_query(self, i):
        import numpy as np
        import pandas as pd
        from pandas.testing import assert_frame_equal
        from sqlglot import find_tables, parse_one, transpile
        from sqlglot.executor import execute
        from tests.helpers import TPCDS_SCHEMA

        meta, sql, _ = self.sqls()[i]
        expression = pickle.loads(
            pickle.dumps(parse_one(sql))
        )  # the test ships it to a worker
        if not meta.get("execute"):
            return
        tables = {}
        for t in find_tables(expression):
            tables[t.name] = self.table(t.name)
        tables = pickle.loads(pickle.dumps(tables))
        table = execute(expression, schema=TPCDS_SCHEMA, tables=tables)
        with self.subTest(f"tpc-ds {i + 1}"):
            conn = None
            for t in find_tables(parse_one(sql)):
                conn = self.view(t.name)  # duckdb views for the expected result
            a = conn.execute(transpile(sql, write="duckdb")[0]).fetchdf()
            b = pd.DataFrame(
                ((np.nan if c is None else c for c in r) for r in table.rows),
                columns=table.columns,
            )
            assert_frame_equal(a, b, check_dtype=False, check_index_type=False)


for _i in range(120):
    setattr(Tpcds, f"test_q{_i}", lambda self, _i=_i: self.run_query(_i))
