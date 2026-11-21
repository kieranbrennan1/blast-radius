"""Cheap stand-ins for sqlglot's slowest tests, run by the run-time check on the patched code.

A stand-in runs the same code as the real test on a smaller input. If it fails, or
runs much slower than it did on the pinned commit, the real test is kept; if it
passes, the real test is dropped like any other candidate that passed.

  test_max_nodes (28.8 s)        -> MaxNodes.test_small: same parses, 1/100 the width
                                    and limits scaled to match
  test_generator_memory (6.7 s)  -> GeneratorMemory.test_small: every dialect,
                                    100 measured instantiations instead of 1000
  test_execute_tpcds (21.5 s)    -> standins_tpcds.py (needs duckdb, kept apart)
"""

import ast
import csv
import gc
import gzip
import os
import pickle
import time
import tracemalloc
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _cpu():
    return time.process_time()


class _Timed(unittest.TestCase):
    """Fail when the stand-in is far slower than on the pinned commit (the real test could time out)."""

    BUDGET = {}  # method name -> seconds allowed

    def setUp(self):
        self._t0 = _cpu()

    def tearDown(self):
        limit = self.BUDGET.get(self._testMethodName)
        if limit is not None and _cpu() - self._t0 > limit:
            raise AssertionError(
                f"stand-in ran slow: {_cpu() - self._t0:.2f} s > {limit} s"
            )


class MaxNodes(_Timed):
    BUDGET = {"test_small": 3.0}

    def test_small(self):
        from sqlglot import exp, parse_one
        from sqlglot.errors import ParseError

        self.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)

        cols = [f"col{i}" for i in range(500)]
        sql = f"SELECT {','.join(cols)} FROM t"
        self.assertIsInstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

        width = 100  # the real test uses 10000
        depth = 100
        expr = "x" + "=x" * depth
        sql = f"SELECT {','.join(expr for _ in range(width))}"

        with self.assertRaises(ParseError) as ctx:
            parse_one(sql, max_nodes=10_000)
        self.assertIn("Maximum number of AST nodes", str(ctx.exception))

        with self.assertRaises(ParseError) as ctx:
            parse_one(sql, max_nodes=500)
        self.assertIn("(500)", str(ctx.exception))

        # a parse under the limit still works
        small = f"SELECT {','.join(expr for _ in range(2))}"
        self.assertIsInstance(parse_one(small, max_nodes=10_000), exp.Select)

        with self.assertRaises(ParseError):
            parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
        self.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)


class GeneratorMemory(_Timed):
    BUDGET = {"test_small": 4.0}

    def test_small(self):
        from sqlglot.dialects.dialect import Dialect, Dialects

        for name in Dialects:
            with self.subTest(dialect=name):
                dialect = Dialect.get_or_raise(name)
                for _ in range(100):
                    dialect.generator()
                gc.collect()
                tracemalloc.start()
                try:

                    def grow():
                        before = tracemalloc.get_traced_memory()[0]
                        for _ in range(100):
                            dialect.generator()
                        gc.collect()
                        return tracemalloc.get_traced_memory()[0] - before

                    first = grow()  # clean code measures 0 bytes
                    if first >= 1024 * 100 / 1000:
                        # a one-time allocation or a leak: the real test's 1000 calls would grow by about
                        # this first 100 plus nine more of the next 100 (the steady per-call leak)
                        first += 9 * grow()
                finally:
                    tracemalloc.stop()
                # the real test allows < 1024 bytes over 1000 calls; keep a margin for the estimate
                self.assertLess(first, 900)
