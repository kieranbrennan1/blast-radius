"""Run candidate tests against a patched copy of sqlglot and report which fail.

One fork server: the parent imports pytest and the heavy third-party packages
the tests use (never sqlglot), then forks a child per commit. The child points
sys.path at a patched copy of the repo in /tmp, runs the chosen tests with
pytest, and sends back the ids that failed, errored, timed out or could not be
imported. Anything that goes wrong counts every chosen test as failing (safe).
"""

import os
import shutil
import signal
import sys
import time

WORK = os.environ.get("BR_WORK", "/tmp/br")


class _Recorder:
    def __init__(self, limits):
        self.failed = set()
        self.limits = limits

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.failed.add("__collect__:" + report.nodeid)

    # one alarm per test: a test that runs far past its recorded time counts as failing
    def pytest_runtest_call(self, item):
        limit = self.limits.get(item.nodeid, 5.0)
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, limit)

    def pytest_runtest_teardown(self, item):
        signal.setitimer(signal.ITIMER_REAL, 0)


class _Timeout(BaseException):
    pass


def _on_alarm(signum, frame):
    raise _Timeout("over time")


class _OneThreadPool:
    """Stand-in for ProcessPoolExecutor: run submitted work in this process (no spawn cost)."""

    def __init__(self, *a, **k):
        pass

    def submit(self, fn, *a, **k):
        from concurrent.futures import Future

        f = Future()
        try:
            f.set_result(fn(*a, **k))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, **k):
        return map(fn, *iterables)

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()


class Runner:
    def __init__(self, repo):
        self.repo = repo
        self.ready = False

    def start(self):
        """Copy the repo to /tmp once and import the test dependencies (not sqlglot)."""
        if self.ready:
            return
        self.root = os.path.join(WORK, "repo")
        if os.path.exists(self.root):
            shutil.rmtree(self.root)
        os.makedirs(WORK, exist_ok=True)
        shutil.copytree(
            self.repo,
            self.root,
            ignore=shutil.ignore_patterns(
                ".git", "docs", "posts", "pdoc", "benchmarks", "sqlglotc", "*.png"
            ),
        )
        os.environ["SKIP_INTEGRATION"] = "0"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        sys.dont_write_bytecode = True
        import pytest  # noqa: F401
        import _pytest.unittest  # noqa: F401
        import _pytest.python  # noqa: F401

        for mod in ("duckdb", "pandas", "numpy", "dateutil", "pytz", "unittest.mock"):
            try:
                __import__(mod)
            except Exception:
                pass
        self.ready = True

    def run(self, files: dict, test_ids, limits):
        """files: {repo path: new text or None (deleted)}. Returns set of failing ids, or None on trouble."""
        self.start()
        saved = {}
        for path, text in files.items():
            dst = os.path.join(self.root, path)
            saved[path] = open(dst, "rb").read() if os.path.exists(dst) else None
            if text is None:
                if os.path.exists(dst):
                    os.remove(dst)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(text)
        try:
            return self._fork(sorted(test_ids), limits)
        finally:
            for path, data in saved.items():
                dst = os.path.join(self.root, path)
                if data is None:
                    if os.path.exists(dst):
                        os.remove(dst)
                else:
                    with open(dst, "wb") as f:
                        f.write(data)

    def _fork(self, test_ids, limits):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            code = 1
            try:
                os.chdir(self.root)
                sys.path.insert(0, self.root)
                import concurrent.futures as cf

                cf.ProcessPoolExecutor = _OneThreadPool
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                import pytest

                rec = _Recorder(limits)
                pytest.main(
                    [
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        "-p",
                        "no:randomly",
                        "--no-header",
                        "-o",
                        "addopts=",
                        *test_ids,
                    ],
                    plugins=[rec],
                )
                os.write(w, ("\n".join(sorted(rec.failed)) + "\n#done\n").encode())
                code = 0
            except BaseException:
                pass
            finally:
                os._exit(code)
        os.close(w)
        chunks = []
        while True:
            b = os.read(r, 65536)
            if not b:
                break
            chunks.append(b)
        os.close(r)
        _, status = os.waitpid(pid, 0)
        out = b"".join(chunks).decode(errors="replace")
        if not out.endswith("#done\n"):
            return None
        return {ln for ln in out.splitlines() if ln and ln != "#done"}


if (
    __name__ == "__main__"
):  # quick manual check: python verify.py <repo> <patch> <test id>...
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import selector

    repo, patch = sys.argv[1], sys.argv[2]
    files = {}
    for f in selector.parse_patch(open(patch).read()):
        p = f["new"] or f["old"]
        old = open(os.path.join(repo, f["old"])).read() if f["old"] else ""
        files[p] = None if f["new"] is None else selector.apply_hunks(old, f["hunks"])
    t = time.process_time()
    start = time.time()
    got = Runner(repo).run(files, sys.argv[3:], {})
    ch = os.times()
    print(
        "failed:",
        got,
        f"wall {time.time() - start:.2f}s cpu self {time.process_time() - t:.2f}s children {ch.children_user + ch.children_system:.2f}s",
    )
