"""Run candidate tests against the patched code, in-process, and keep only the ones that fail.

One parent process imports sqlglot and the test modules once. For each commit
whose edits are all inside function bodies, it forks a child that swaps the new
bytecode into the already-loaded functions (func.__code__ = new) and runs the
cheap candidate tests with unittest. The child reports which ran, which failed.

Safety rules (any doubt -> the caller keeps every candidate):
  * only edits that change function or lambda bodies; a changed module body,
    class body, default argument or decorator means import-time behaviour
    changed, so we do not verify that commit;
  * a changed function that already ran while the parent imported modules
    (seen with sys.monitoring) may have left stale state behind: not verified;
  * every live function object with the old code must be found, or (for a
    nested function) its enclosing function must be swapped too;
  * a test that errors, fails, times out or crashes the child counts as failing.
"""

import gc
import importlib
import os
import pickle
import signal
import sys
import time
import types
import unittest

CO_NEWLOCALS = 0x2
TOOL = None
STARTED: set = set()


def _on_start(code, offset):
    STARTED.add(code)
    return sys.monitoring.DISABLE


def start_watch():
    """Record every code object that starts running from now on (once each)."""
    global TOOL
    for tid in (5, 4, 3, 2):
        try:
            sys.monitoring.use_tool_id(tid, "blast-radius")
        except ValueError:
            continue
        TOOL = tid
        sys.monitoring.register_callback(tid, sys.monitoring.events.PY_START, _on_start)
        sys.monitoring.set_events(tid, sys.monitoring.events.PY_START)
        return


def stop_watch():
    if TOOL is not None:
        sys.monitoring.set_events(TOOL, 0)


def walk(co, out):
    out.append(co)
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            walk(c, out)
    return out


def shallow(co):
    consts = tuple(
        ("<code>", c.co_qualname)
        if isinstance(c, types.CodeType)
        else (type(c).__name__, c)
        for c in co.co_consts
    )
    return (
        co.co_code,
        consts,
        co.co_names,
        co.co_varnames,
        co.co_freevars,
        co.co_cellvars,
        co.co_argcount,
        co.co_posonlyargcount,
        co.co_kwonlyargcount,
        co.co_flags,
        co.co_exceptiontable,
    )


def is_function(co):
    return bool(co.co_flags & CO_NEWLOCALS) and co.co_qualname != "<module>"


class InlinePool:
    """Stand-in for ProcessPoolExecutor: run work here, with a pickle round trip like a real pool."""

    def __init__(
        self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw
    ):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
        from concurrent.futures import Future

        f = Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()


class _Timeout(BaseException):
    pass


_FIRED = [0]


def _alarm(signum, frame):
    # first time: interrupt the test; again (a subTest swallowed it and kept looping): end the child,
    # the parent then counts this unreported test as failing
    _FIRED[0] += 1
    if _FIRED[0] > 1:
        os._exit(0)
    raise _Timeout()


class Verifier:
    def __init__(self, repo: str, site: str | None = None, graph: str | None = None):
        self.repo = os.path.abspath(repo)
        self.site = site
        self.graph_path = graph
        self._rev = None
        self.ready = False
        self.broken = False
        self._index_size = -1
        self._live: dict = {}

    # ------------------------------------------------------------ parent side
    def _start(self):
        if self.ready:
            return
        self.ready = True
        os.environ["SKIP_INTEGRATION"] = "0"
        os.environ.setdefault("PYTHONHASHSEED", "0")
        sys.dont_write_bytecode = True
        if self.site and self.site not in sys.path:
            sys.path.append(self.site)
        sys.path.insert(0, self.repo)
        start_watch()
        import concurrent.futures as cf

        cf.ProcessPoolExecutor = InlinePool
        import sqlglot  # noqa: F401

        if os.environ.get("BR_PRELOAD_ALL"):
            import pkgutil

            for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
                try:
                    importlib.import_module(info.name)
                except BaseException:  # noqa: BLE001
                    pass

    def module_of(self, path: str) -> str:
        mod = path[:-3].replace("/", ".")
        return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod

    def ensure(self, mods) -> bool:
        self._start()
        for m in mods:
            if m in sys.modules:
                continue
            try:
                importlib.import_module(m)
            except BaseException:  # noqa: BLE001
                return False
            mod = sys.modules[m]
            if getattr(mod, "ProcessPoolExecutor", None) is not None:
                mod.ProcessPoolExecutor = InlinePool
        return True

    def _live_index(self):
        if len(sys.modules) == self._index_size:
            return self._live
        live: dict = {}
        prefix = self.repo + os.sep
        for o in gc.get_objects():
            if type(o) is types.FunctionType:
                co = o.__code__
                if co.co_filename.startswith(prefix):
                    live.setdefault((co.co_filename, co.co_qualname), []).append(o)
        self._live, self._index_size = live, len(sys.modules)
        return live

    @staticmethod
    def static_pairs(repo, files: dict):
        """files: {repo path: (old text, new text)} for every changed file.

        Returns a list of (function, new code) swaps (possibly empty: no behaviour
        change), or None when the commit cannot be verified safely.
        """
        pairs = []
        for path, (old, new) in files.items():
            if (
                not path.endswith(".py")
                or not path.startswith("sqlglot/")
                or new is None
                or old is None
            ):
                return None
            fn = os.path.join(os.path.abspath(repo), path)
            try:
                oc = compile(old, fn, "exec", dont_inherit=True)
                nc = compile(new, fn, "exec", dont_inherit=True)
            except SyntaxError:
                return None
            ol, nl = walk(oc, []), walk(nc, [])
            if [c.co_qualname for c in ol] != [c.co_qualname for c in nl]:
                return None
            # dirty = this code or anything nested in it changed
            dirty = {}

            def mark(o, n):
                d = shallow(o) != shallow(n)
                oi = [c for c in o.co_consts if isinstance(c, types.CodeType)]
                ni = [c for c in n.co_consts if isinstance(c, types.CodeType)]
                for a, b in zip(oi, ni):
                    d = mark(a, b) or d
                dirty[id(o)] = d
                return d

            mark(oc, nc)
            for o, n in zip(ol, nl):
                if not dirty[id(o)]:
                    continue
                if not is_function(o):
                    if shallow(o) != shallow(n):
                        return (
                            None  # module or class body changed: import-time behaviour
                        )
                    continue
                if o.co_freevars != n.co_freevars:
                    return None
                # own=False: only something nested inside changed, its own behaviour did not
                pairs.append((path, fn, o, n, shallow(o) != shallow(n)))
        return pairs

    def plan(self, files: dict):
        pairs = self.static_pairs(self.repo, files)
        if pairs is None:
            return None
        if not pairs:
            return []
        if not self.ensure(sorted({self.module_of(p) for p, *_ in pairs})):
            return None
        live = self._live_index()
        swaps = []
        for path, fn, o, n, own in pairs:
            found = [f for f in live.get((fn, o.co_qualname), ()) if f.__code__ == o]
            for f in found:
                if own and f.__code__ in STARTED:
                    return None  # ran at import time: its results may be cached already
            if (
                not found
                and "<locals>" not in o.co_qualname
                and "<lambda>" not in o.co_qualname
            ):
                return None  # a top-level function we cannot find: be safe
            if not found and "<locals>" not in o.co_qualname:
                return None  # a module/class-level lambda we cannot find
            swaps += [(f, n, own) for f in found]
        return swaps

    def _reverse(self):
        if self._rev is None:
            import json

            g = json.load(open(self.graph_path))
            self._files = g["files"]
            self._mod_of = {v: k for k, v in g["files"].items()}
            rev: dict = {}
            for m, deps in g["imports"].items():
                for d in deps:
                    rev.setdefault(d, set()).add(m)
            self._rev = rev
        return self._rev

    def reload_set(self, files: dict):
        """Modules to re-import for a change outside function bodies: changed ones plus all importers.

        Returns (sources {module: (file, text, is_package)}, modules in load order) or None.
        """
        if not self.graph_path:
            return None
        rev = self._reverse()
        sources = {}
        for path, (old, new) in files.items():
            if not path.endswith(".py") or old is None or new is None:
                return None
            mod = self._mod_of.get(path)
            if mod is None:
                return None
            fn = os.path.join(self.repo, path)
            try:
                oc = compile(old, fn, "exec", dont_inherit=True)
                nc = compile(new, fn, "exec", dont_inherit=True)
            except SyntaxError:
                return None
            if [shallow(c) for c in walk(oc, [])] == [shallow(c) for c in walk(nc, [])]:
                continue  # same bytecode: nothing to reload for this file
            sources[mod] = (fn, new, path.endswith("__init__.py"))
        if not sources:
            return {}, []
        out = set(sources)
        stack = list(sources)
        while stack:
            x = stack.pop()
            for y in rev.get(x, ()):
                if y not in out:
                    out.add(y)
                    stack.append(y)
        return sources, out

    def run(self, swaps, test_ids, limits, stop_on_fail=True, reload=None, fast_exec=False):
        """Fork, apply swaps, run tests in the given order (grouped by class).

        Returns {test id: passed?} for every test that finished, or None on trouble.
        """
        mods = sorted({tid.split("::")[0][:-3].replace("/", ".") for tid in test_ids})
        if not self.ensure(mods):
            return None
        rows = None
        if fast_exec and EXEC_CACHE and "tests.test_executor" in mods:
            try:
                rows = _exec_rows()
            except Exception:  # noqa: BLE001
                rows = None
        # the test modules' own import may have run the changed functions
        for f, _, own in swaps:
            if own and f.__code__ in STARTED:
                return None
        r, w = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                try:  # a runaway child can never eat the run's CPU: the kernel stops it
                    import resource

                    cap = int(30 + sum(limits.get(t, 10.0) for t in test_ids))
                    resource.setrlimit(resource.RLIMIT_CPU, (cap, cap + 5))
                except Exception:  # noqa: BLE001
                    pass
                try:  # if memory runs out, the kernel should kill this child, not the selector
                    with open("/proc/self/oom_score_adj", "w") as fh:
                        fh.write("1000")
                except OSError:
                    pass
                stop_watch()
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                os.chdir(self.repo)
                for f, n, _ in swaps:
                    f.__code__ = n
                if reload is not None:
                    _reload(*reload)
                if rows is not None:
                    _fast_exec_setup(rows)
                signal.signal(signal.SIGALRM, _alarm)
                _run_child(test_ids, limits, w, stop_on_fail)
                os.write(w, b"#done\n")
            except BaseException:  # noqa: BLE001
                pass
            finally:
                os._exit(0)
        os.close(w)
        chunks = []
        while True:
            b = os.read(r, 65536)
            if not b:
                break
            chunks.append(b)
        os.close(r)
        os.waitpid(pid, 0)
        text = b"".join(chunks).decode(errors="replace")
        res = {}
        wanted = set(test_ids)
        for ln in text.splitlines():
            if ln == "#done" or not ln:
                continue
            flag, _, tid = ln.partition(" ")
            if tid in wanted and flag in ("0", "1"):
                res[tid] = flag == "1" and res.get(tid, True)  # reported twice: any failure wins
        if not text.endswith("#done\n"):
            # the child died mid-run: the next test counts as failing, the rest stay unverified
            for tid in test_ids:
                if tid not in res:
                    res[tid] = False
                    break
        return res


class _Rec(unittest.TestResult):
    def __init__(self, ids, limits, w, stop_on_fail):
        super().__init__()
        self.ids, self.limits, self.w, self.stop_on_fail = ids, limits, w, stop_on_fail
        self.cur = None
        self.bad = False
        self.fail_at = None
        self.reported = set()

    def _tid(self, test):
        return self.ids.get((type(test), getattr(test, "_testMethodName", None)))

    def startTest(self, test):
        super().startTest(test)
        self.cur, self.bad = self._tid(test), False
        _FIRED[0] = 0
        signal.setitimer(signal.ITIMER_REAL, self.limits.get(self.cur, 10.0), 0.5)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        super().stopTest(test)
        if self.cur is not None:
            self.reported.add(self.cur)
            os.write(self.w, f"{0 if self.bad else 1} {self.cur}\n".encode())
            if self.bad and self.fail_at is None:
                self.fail_at = time.process_time()
            if self.fail_at is not None and self.stop_on_fail is not False:
                budget = 0 if self.stop_on_fail is True else float(self.stop_on_fail)
                if time.process_time() - self.fail_at >= budget:
                    self.stop()
        self.cur = None

    def _fail(self, test=None, err=None):
        if err is not None and os.environ.get("BR_DEBUG"):
            import traceback
            with open(os.environ["BR_DEBUG"], "a") as fh:
                fh.write(f"--- {self.cur}\n" + "".join(traceback.format_exception(*err))[-3000:])
        if self.cur is None:
            # class or module fixture failed: stop; unreported tests stay unverified
            self.stop()
            raise _Abort()
        self.bad = True

    def addError(self, test, err):
        super().addError(test, err)
        self._fail(test, err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._fail(test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._fail(test, err)

    def addSkip(self, test, reason):
        # skipped here proves nothing (CI may run it): keep the test
        super().addSkip(test, reason)
        if self.cur is not None:
            self.bad = True

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._fail(test)


class _Abort(BaseException):
    pass


# TestExecutor.setUpClass turns ~1M CSV values into Python rows (~2 s CPU), and it ran again in every child that ran an
# executor test. The rows come only from fixture files and plain Python (csv, ast), never from sqlglot, so the server
# reads them once and each child builds the same tables from them. sqlglot's Table, duckdb and the SQL fixtures are
# still made in the child, on the patched code. Used only when the commit touches no test file. BR_EXEC_CACHE=0: off.
EXEC_CACHE = os.environ.get("BR_EXEC_CACHE", "1") == "1"
_EXEC_ROWS = None


def _exec_rows():
    """Server side, once: {csv file: rows} exactly as TestExecutor.setUpClass builds them."""
    global _EXEC_ROWS
    if _EXEC_ROWS is None:
        import ast
        import csv

        m = sys.modules["tests.test_executor"]
        out = {}
        for directory, schema in ((m.DIR_TPCH, m.TPCH_SCHEMA), (m.DIR_TPCDS, m.TPCDS_SCHEMA)):
            for table in schema:
                file_name = f"{directory}{table}.csv.gz"
                with m.open_file(file_name) as fh:
                    reader = csv.reader(fh, delimiter="|")
                    rows = []
                    ctypes = []
                    next(reader)
                    for row in reader:
                        if not ctypes:
                            for v in row:
                                try:
                                    ctypes.append(type(ast.literal_eval(v)))
                                except (ValueError, SyntaxError):
                                    ctypes.append(str)
                        rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
                out[file_name] = rows
        _EXEC_ROWS = out
    return _EXEC_ROWS


class _LazyConn:
    """A duckdb connection made (with its views) the first time a test uses it."""

    def __init__(self, make):
        self._make = make
        self._conn = None

    def close(self):
        if self._conn is not None:
            self._conn.close()

    def __getattr__(self, name):
        if self._conn is None:
            self._conn = self._make()
        return getattr(self._conn, name)


def _fast_exec_setup(rows):
    """Child side: TestExecutor.setUpClass as written in the pinned tests, with the rows read from the cache."""
    m = sys.modules.get("tests.test_executor")
    cls = getattr(m, "TestExecutor", None)
    if cls is None:
        return

    def views(directory, schema):
        def make():
            conn = m.duckdb.connect()
            for table, columns in schema.items():
                file_name = f"{directory}{table}.csv.gz"
                conn.execute(
                    f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
                )
            return conn

        return _LazyConn(make)

    def setUpClass(cls):
        # duckdb's views cost ~0.9 s and only the TPC comparisons query them: made on first use
        cls.tpch_conn = views(m.DIR_TPCH, m.TPCH_SCHEMA)
        cls.tpcds_conn = views(m.DIR_TPCDS, m.TPCDS_SCHEMA)
        cls.tpch_tables = {}
        cls.tpcds_tables = {}
        for directory, schema, tables in (
            (m.DIR_TPCH, m.TPCH_SCHEMA, cls.tpch_tables),
            (m.DIR_TPCDS, m.TPCDS_SCHEMA, cls.tpcds_tables),
        ):
            for table, columns in schema.items():
                tables[table] = m.Table(columns=columns, rows=rows[f"{directory}{table}.csv.gz"])
        cls.cache = {}
        cls.tpch_sqls = list(m.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        cls.tpcds_sqls = list(m.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))

    cls.setUpClass = classmethod(setUpClass)


def _run_child(test_ids, limits, w, stop_on_fail):
    ids = {}
    groups: dict = {}
    for tid in test_ids:
        path, cls_name, meth = tid.split("::")
        mod = sys.modules[path[:-3].replace("/", ".")]
        cls = getattr(mod, cls_name)
        if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
            os.write(w, f"0 {tid}\n".encode())
            if stop_on_fail:
                return
            continue
        ids[(cls, meth)] = tid
        groups.setdefault(cls, []).append(cls(meth))
    rec = _Rec(ids, limits, w, stop_on_fail)
    for cls, tests in groups.items():
        if rec.shouldStop:
            break
        suite = unittest.TestSuite(tests)
        try:
            suite.run(rec)
        except _Abort:
            raise


class _PatchFinder:
    """Serve the patched source for the changed modules."""

    def __init__(self, sources):
        self.sources = sources

    def find_spec(self, name, path=None, target=None):
        got = self.sources.get(name)
        if got is None:
            return None
        import importlib.util

        fn, text, is_pkg = got
        return importlib.util.spec_from_file_location(
            name,
            fn,
            loader=_PatchLoader(text, fn),
            submodule_search_locations=[os.path.dirname(fn)] if is_pkg else None,
        )


class _PatchLoader:
    def __init__(self, text, fn):
        self.text, self.fn = text, fn

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.text, self.fn, "exec", dont_inherit=True), module.__dict__)


def _reload(sources, names):
    """Child only: forget the changed modules and their importers, import them again, fix stale names."""
    sys.meta_path.insert(0, _PatchFinder(sources))
    order = [n for n in list(sys.modules) if n in names]
    for n in sources:
        if n not in order:
            order.append(n)
    for n in order:
        sys.modules.pop(n, None)
    for n in order:
        importlib.import_module(n)
    # modules we kept may still hold the old objects (cached lazy attributes, runtime imports)
    for mname, mod in list(sys.modules.items()):
        if mname in names or not (mname == "sqlglot" or mname.startswith(("sqlglot.", "tests"))):
            continue
        d = getattr(mod, "__dict__", None)
        if not d:
            continue
        for k, v in list(d.items()):
            if isinstance(v, types.ModuleType):
                if v.__name__ in names and sys.modules.get(v.__name__) is not v:
                    d[k] = sys.modules[v.__name__]
                continue
            owner = getattr(v, "__module__", None)
            if owner in names and isinstance(v, (type, types.FunctionType)):
                q = getattr(v, "__qualname__", "")
                if "<" in q:
                    continue
                new = sys.modules.get(owner)
                for part in q.split("."):
                    new = getattr(new, part, None)
                    if new is None:
                        break
                if new is not None:
                    d[k] = new
    for n in order:
        mod = sys.modules.get(n)
        if mod is not None and getattr(mod, "ProcessPoolExecutor", None) is not None:
            mod.ProcessPoolExecutor = InlinePool


# ---------------------------------------------------------------- server process
#
# The selector never imports sqlglot, the tests or their libraries itself: it
# starts this file as a separate process and sends it one JSON request a line.
# If anything here crashes (a library built for a newer CPU, say), the selector
# just keeps every candidate test.


def _check(v, req):
    files = {p: (o, n) for p, (o, n) in req["files"].items()}
    reload = None
    swaps = v.plan(files)
    if swaps is None:
        reload = v.reload_set(files)
        if reload is None:
            return {"how": "not checkable", "res": None}
        swaps = []
        if not reload[0]:
            reload = None
        else:
            v.ensure([])
    fast_exec = not any(p.startswith("tests/") for p in files)
    res = v.run(swaps, req["ids"], req["limits"], stop_on_fail=req["stop_on_fail"], reload=reload, fast_exec=fast_exec)
    how = f"reload {len(reload[1])}" if reload else f"swap {len(swaps)}"
    return {"how": how, "res": res}


def serve(repo, site, graph):
    import json

    out = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)  # anything printed by imports or tests goes to stderr, never the protocol
    v = Verifier(repo, site, graph)
    for line in sys.stdin:
        req = json.loads(line)
        t = os.times()
        try:
            ans = _check(v, req)
        except Exception as e:  # noqa: BLE001
            ans = {"how": f"error {type(e).__name__}", "res": None}
        u = os.times()
        ans["cpu_ms"] = round(1000 * (sum(u[:4]) - sum(t[:4])))
        out.write(json.dumps(ans) + "\n")


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2] or None, sys.argv[3])
