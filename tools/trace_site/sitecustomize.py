"""Trace what a test touches, in every Python process it starts (pool workers too).

Put this folder on PYTHONPATH with TRACE_ROOT and TRACE_OUT set. The first
process writes TRACE_OUT; every child process writes TRACE_OUT.<pid>. Each file:
  lines   {repo file: [lines executed by any code]}
  rlines  {repo file: [lines executed by function code, not module/class bodies]}
  classes [sqlglot classes (with their bases) whose methods ran on an instance]
  opens   [repo data files opened]
"""

import os
import sys

if os.environ.get("TRACE_OUT") and os.environ.get("TRACE_ROOT"):
    import atexit
    import json

    ROOT = os.path.realpath(os.environ["TRACE_ROOT"]) + os.sep
    if os.environ.get("TRACE_MAIN"):
        OUT = f"{os.environ['TRACE_OUT']}.{os.getpid()}"
    else:
        OUT = os.environ["TRACE_OUT"]
        os.environ["TRACE_MAIN"] = str(os.getpid())

    mon = sys.monitoring
    TOOL = mon.COVERAGE_ID
    DISABLE = mon.DISABLE
    CO_OPTIMIZED = 1
    SKIP_NAMES = {"sql_names", "__init_subclass__", "__new__", "__set_name__", "__class_getitem__"}
    lines: dict = {}
    rlines: dict = {}
    classes: dict = {}
    opens: set = set()
    _rel: dict = {}

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
        if code.co_flags & CO_OPTIMIZED:
            s = rlines.get(r)
            if s is None:
                s = rlines[r] = set()
            s.add(line)
        return DISABLE

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
                        classes[klass] = 1
        return None

    def audit(event, args):
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            try:
                r = rel(os.path.abspath(os.fsdecode(args[0])))
                if r and "__pycache__" not in r and not r.endswith(".py"):
                    opens.add(r)
            except Exception:
                pass

    def dump():
        mon.set_events(TOOL, 0)
        names = set()
        for k in classes:
            for b in k.__mro__:
                mod = getattr(b, "__module__", "") or ""
                if mod.startswith("sqlglot"):
                    names.add(f"{mod}:{b.__qualname__}")
        with open(OUT, "w") as f:
            json.dump({
                "lines": {k: sorted(v) for k, v in lines.items()},
                "rlines": {k: sorted(v) for k, v in rlines.items()},
                "classes": sorted(names),
                "opens": sorted(opens),
            }, f)

    mon.use_tool_id(TOOL, "blast-radius-tracer")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
    sys.addaudithook(audit)
    atexit.register(dump)
    try:  # multiprocessing children may leave through os._exit
        from multiprocessing import util as _mpu

        _orig = _mpu._exit_function

        def _exit_function(*a, **k):
            try:
                dump()
            finally:
                return _orig(*a, **k)

        _mpu._exit_function = _exit_function
    except Exception:
        pass
