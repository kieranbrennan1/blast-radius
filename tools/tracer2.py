"""pytest plugin: record what one test touches, from a cold start.

Loaded with `-p tracer` (tools/ on PYTHONPATH) in a fresh process that runs a
single test. Writes TRACE_OUT as JSON:
  lines   {repo-relative file: [line numbers executed]}
  classes [sqlglot classes whose methods ran with them as self/cls]
  opens   [repo-relative files opened]
"""

import json
import os
import sys

ROOT = os.path.realpath(os.environ["TRACE_ROOT"]) + os.sep
OUT = os.environ["TRACE_OUT"]
mon = sys.monitoring
TOOL = mon.COVERAGE_ID
DISABLE = mon.DISABLE

lines: dict[str, set] = {}
rlines: dict[str, set] = {}
CO_OPTIMIZED = 1
classes: dict = {}
SKIP_NAMES = {"sql_names", "__init_subclass__", "__new__", "__set_name__", "__class_getitem__"}
opens: set = set()
_rel: dict[str, object] = {}


def rel(filename):
    r = _rel.get(filename)
    if r is None:
        real = os.path.realpath(filename)
        r = real[len(ROOT):] if real.startswith(ROOT) and "/.venv/" not in real else False
        _rel[filename] = r
    return r


def on_line(code, line):
    r = rel(code.co_filename)
    if not r:
        return DISABLE
    s = lines.get(r)
    if s is None:
        s = lines[r] = set()
    s.add(line)
    if code.co_flags & CO_OPTIMIZED:  # function code (incl. lambdas), not a module or class body
        s = rlines.get(r)
        if s is None:
            s = rlines[r] = set()
        s.add(line)
    return DISABLE  # one test per process: once is enough


def on_start(code, offset):
    r = rel(code.co_filename)
    if not r or not r.startswith("sqlglot/"):
        return DISABLE
    if code.co_argcount and code.co_name not in SKIP_NAMES:
        name = code.co_varnames[0]
        if name == "self" or name == "cls":
            obj = sys._getframe(1).f_locals.get(name)
            if name == "cls":
                if not isinstance(obj, type) or issubclass(obj, type):
                    return None
                klass = obj
            else:
                if isinstance(obj, type):
                    return None
                klass = type(obj)
            if klass not in classes:
                mod = getattr(klass, "__module__", "") or ""
                if mod.startswith("sqlglot"):
                    classes[klass] = code.co_name
    return None


def audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        try:
            p = os.fsdecode(args[0])
            r = rel(os.path.abspath(p))
            if r and "__pycache__" not in r and not r.endswith(".py"):
                opens.add(r)
        except Exception:
            pass


mon.use_tool_id(TOOL, "blast-radius-tracer2")
mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.register_callback(TOOL, mon.events.PY_START, on_start)
mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
sys.addaudithook(audit)


def _names(klass):
    out = set()
    for k in klass.__mro__:
        mod = getattr(k, "__module__", "") or ""
        if mod.startswith("sqlglot"):
            out.add(f"{mod}:{k.__qualname__}")
    return out


def pytest_unconfigure(config):
    mon.set_events(TOOL, 0)
    names = set()
    for k in classes:
        names |= _names(k)
    if os.environ.get("TRACE_DEBUG"):
        import collections
        print(collections.Counter(classes.values()).most_common(20), file=sys.stderr)
    data = {
        "lines": {f: sorted(s) for f, s in lines.items()},
        "rlines": {f: sorted(s) for f, s in rlines.items()},
        "classes": sorted(names),
        "opens": sorted(opens),
    }
    with open(OUT, "w") as f:
        json.dump(data, f)
