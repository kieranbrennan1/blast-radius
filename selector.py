"""Blast Radius selector: for each sqlglot patch, pick the tests that can fail.

How it decides (every map was made from the pinned sqlglot commit, see tools/):
  1. Apply the patch to a pristine copy of the file and compare the old and new
     Python syntax trees. Edits that leave the tree unchanged (comments, blank
     lines, formatting) cannot change behaviour, so they select nothing.
  2. For each statement that really changed, look at where it lives:
       inside a function  -> tests that executed those lines (cold-start trace)
       in a class body    -> tests that used the class (or a subclass) and ran
                             a line that reads the changed name
       at module level    -> tests that ran a line that reads the changed name
  3. A changed data file (SQL fixtures) selects the tests that opened it.
  4. Anything we cannot reason about selects every test that could be affected:
     a miss costs 300 s, while running everything costs about 37 s.
Then the chosen tests are packed onto the number of workers (1-8) that costs least.
"""

import ast
import copy
import difflib
import json
import os
import pickle
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
REPO = Path(os.environ.get("BR_REPO", "/app/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", "/app/map.pkl"))
IMPORTS = Path(os.environ.get("BR_IMPORTS", str(MAP.parent / "imports.json")))
WORKER_MS = 2000
MAX_WORKERS = 8

# Files whose change can alter how pytest itself runs: select everything.
PYTEST_CONFIG = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini"}
COMPOUND_BODIES = ("body", "orelse", "finalbody", "handlers", "cases")


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- the map


class Map:
    def __init__(self, path: Path):
        if path.suffix == ".pkl":
            d = pickle.loads(path.read_bytes())
        else:
            import gzip

            with gzip.open(path, "rt") as f:
                d = json.load(f)
        self.ids = d["tests"]
        self.sets = d["sets"]
        self.lines = d["lines"]
        self.classes = d["classes"]
        self.opens = d["opens"]
        self.touched = d["touched"]
        self.names = d.get("names", {})
        self.rlines = d.get("rlines")
        self.known = set(d.get("known", ()))
        self.import_reads = set(d.get("import_reads", ()))
        self.names_rt = d.get("names_rt")
        self.import_lines = d.get("import_lines", {})
        self._decoded: dict = {}

    def s(self, i) -> frozenset:
        got = self._decoded.get(i)
        if got is None:
            acc, out = 0, []
            for delta in self.sets[i]:
                acc += delta
                out.append(acc)
            got = self._decoded[i] = frozenset(out)
        return got

    def line_tests(self, file: str, first: int, last: int, runtime=False) -> set:
        """Tests that executed any of these lines; runtime=True counts only function code."""
        fm = self.lines.get(file)
        rm = (self.rlines or {}).get(file, {}) if runtime else {}
        out: set = set()
        if fm:
            for ln in range(first, last + 1):
                i = rm.get(ln, rm.get(str(ln))) if runtime else None
                if i is None:
                    i = fm.get(ln, fm.get(str(ln)))
                    if runtime and i is not None and (ln in self.import_lines.get(file, ()) or str(ln) in self.import_lines.get(file, ())):
                        continue  # executed only by a module or class body
                if i is not None:
                    out |= self.s(i)
        return out

    def _get(self, table, key):
        i = table.get(key)
        return set(self.s(i)) if i is not None else None

    def file_tests(self, file: str) -> set:
        return self._get(self.touched, file) or set()

    def open_tests(self, file: str) -> set:
        return self._get(self.opens, file) or set()

    def class_tests(self, key: str):
        return self._get(self.classes, key)

    def name_tests(self, name: str):
        return self._get(self.names, name)

    def exp_users(self, name: str) -> set:
        """Tests that built an expression of class `name` (any sqlglot.expressions module)."""
        if not hasattr(self, "_exp_index"):
            self._exp_index = {}
            for key, i in self.classes.items():
                mod, qual = key.split(":", 1)
                if mod.startswith("sqlglot.expressions"):
                    self._exp_index.setdefault(qual, []).append(i)
        out = set()
        for i in self._exp_index.get(name, ()):
            out |= self.s(i)
        return out

    def reader_tests(self, name: str):
        """Tests that read the name. If nothing reads it while a module or class body runs,
        only function code can see a change, so count run-time reads only."""
        if self.names_rt is not None and name not in self.import_reads:
            got = self._get(self.names_rt, name)
            return got if got is not None else set()
        return self.name_tests(name)


# ---------------------------------------------------------------- patches


def parse_patch(text: str):
    """Return [{old, new, hunks, ...}]; a hunk is {start, olen, old: [...], new: [...]}."""
    files: list = []
    cur = None
    hunk = None

    def new_file():
        f = {"old": None, "new": None, "hunks": [], "binary": False}
        files.append(f)
        return f

    for line in text.split("\n"):
        if hunk is not None and (hunk["oleft"] > 0 or hunk["nleft"] > 0):
            tag, body = (line[:1] or " "), line[1:]
            if tag == "\\":
                continue
            hunk["seq"].append(tag)
            if tag in (" ", "-"):
                hunk["old"].append(body)
                hunk["oleft"] -= 1
            if tag in (" ", "+"):
                hunk["new"].append(body)
                hunk["nleft"] -= 1
            continue
        if line.startswith("diff --git "):
            cur, hunk = new_file(), None
            parts = line[len("diff --git ") :].split(" b/", 1)
            if len(parts) == 2:
                cur["old"] = parts[0][2:] if parts[0].startswith("a/") else parts[0]
                cur["new"] = parts[1]
        elif line.startswith("--- "):
            if cur is None or cur["hunks"]:
                cur = new_file()
            p = line[4:].split("\t")[0].strip()
            cur["old"] = (
                None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
            )
        elif line.startswith("+++ ") and cur is not None:
            p = line[4:].split("\t")[0].strip()
            cur["new"] = (
                None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            )
        elif line.startswith("@@ ") and cur is not None:
            head = line.split("@@")[1].strip().split()
            o = head[0][1:].split(",")
            n = head[1][1:].split(",")
            olen = int(o[1]) if len(o) > 1 else 1
            nlen = int(n[1]) if len(n) > 1 else 1
            hunk = {
                "start": int(o[0]),
                "olen": olen,
                "old": [],
                "new": [],
                "seq": [],
                "oleft": olen,
                "nleft": nlen,
            }
            cur["hunks"].append(hunk)
        elif cur is not None and cur["hunks"] and line[:1] in ("+", "-") and not line.startswith(("+++ ", "--- ")):
            cur["stray"] = True  # a change line outside every hunk: the counts are wrong
        elif cur is not None:
            if line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur["binary"] = True
            elif line.startswith("new file mode"):
                cur["created"] = True
            elif line.startswith("deleted file mode"):
                cur["deleted"] = True
            elif line.startswith("rename from "):
                cur["old"] = line[len("rename from ") :]
            elif line.startswith("rename to "):
                cur["new"] = line[len("rename to ") :]
    for f in files:
        if f.get("created"):
            f["old"] = None
        if f.get("deleted"):
            f["new"] = None
    return files


def hunks_match(old_text: str, hunks) -> bool:
    """Every hunk's old lines are really at its place in the file, and nothing is left half-read."""
    old = old_text.split("\n")
    for h in hunks:
        if h["oleft"] or h["nleft"]:
            return False
        start = h["start"] - 1 if h["olen"] else h["start"]
        if old[start : start + len(h["old"])] != h["old"]:
            return False
    return True


def apply_hunks(old_text: str, hunks) -> str:
    old = old_text.split("\n")
    out, pos = [], 0
    for h in hunks:
        start = h["start"] - 1 if h["olen"] else h["start"]
        out.extend(old[pos:start])
        out.extend(h["new"])
        pos = start + len(h["old"])
    out.extend(old[pos:])
    return "\n".join(out)


# ---------------------------------------------------------------- syntax-tree diff


def dump(node):
    return ast.dump(node, annotate_fields=False, include_attributes=False)


class Lines:
    """Which old/new lines the patch touched, and where each untouched old line moved to."""

    old_changed: set = set()
    new_changed: set = set()
    o2n: dict = {}
    n2o: dict = {}

    @classmethod
    def load(cls, hunks, old_count):
        cls.old_changed, cls.new_changed, cls.o2n = set(), set(), {}
        o = n = 1
        for h in hunks:
            start = h["start"] if h["olen"] else h["start"] + 1
            while o < start:
                cls.o2n[o] = n
                o += 1
                n += 1
            for tag in h["seq"]:
                if tag == " ":
                    cls.o2n[o] = n
                    o += 1
                    n += 1
                elif tag == "-":
                    cls.old_changed.add(o)
                    o += 1
                else:
                    cls.new_changed.add(n)
                    n += 1
        while o <= old_count + 1:
            cls.o2n[o] = n
            o += 1
            n += 1
        cls.n2o = {v: k for k, v in cls.o2n.items()}


def stmt_key(node, old):
    """A cheap key equal for the old and new copy of an untouched statement; a full dump otherwise."""
    first, last = node_span(node, "all")
    if old:
        a, b = Lines.o2n.get(first), Lines.o2n.get(last)
        clean = a is not None and b is not None and b - a == last - first
        clean = clean and not any(ln in Lines.old_changed for ln in range(first, last + 1))
    else:
        a, b = Lines.n2o.get(first), Lines.n2o.get(last)
        clean = a is not None and b is not None and b - a == last - first
        clean = clean and not any(ln in Lines.new_changed for ln in range(first, last + 1))
        a, b = first, last
    if clean:
        return ("same", a, b)
    if any(isinstance(getattr(node, f, None), list) for f in COMPOUND_BODIES):
        # a touched block: pair it with its twin by kind and name, then look inside
        return ("block", type(node).__name__, getattr(node, "name", None))
    return dump(node)


class Change:
    __slots__ = ("node", "ctx", "part", "neighbors", "new")

    def __init__(self, node, ctx, part, neighbors=None, new=None):
        self.node, self.ctx, self.part, self.neighbors, self.new = node, ctx, part, neighbors, new


def header_dump(node):
    fields = []
    for name, value in ast.iter_fields(node):
        if name in COMPOUND_BODIES:
            continue
        if isinstance(value, list):
            fields.append(
                (name, [dump(v) if isinstance(v, ast.AST) else v for v in value])
            )
        elif isinstance(value, ast.AST):
            fields.append((name, dump(value)))
        else:
            fields.append((name, value))
    return repr(fields)


def diff_lists(old, new, ctx, parent, out):
    od = [stmt_key(s, True) for s in old]
    nd = [stmt_key(s, False) for s in new]
    if od == nd and not any(k[0] == "block" for k in od if isinstance(k, tuple)):
        return
    sm = difflib.SequenceMatcher(None, od, nd, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                if isinstance(od[i1 + k], tuple) and od[i1 + k][0] == "block":
                    diff_node(old[i1 + k], new[j1 + k], ctx, out)
            continue
        if tag == "replace" and i2 - i1 == j2 - j1 and all(type(o) is type(n) for o, n in zip(old[i1:i2], new[j1:j2])):
            for o, n in zip(old[i1:i2], new[j1:j2]):
                diff_node(o, n, ctx, out)
            continue
        if tag in ("replace", "delete"):
            for o in old[i1:i2]:
                out.append(Change(o, ctx, "all"))
        if tag in ("replace", "insert"):
            nb = (old[i1 - 1] if i1 else None, old[i2] if i2 < len(old) else None)
            out.append(Change(parent, ctx, "insert", nb))
            for n in new[j1:j2]:
                out.append(Change(n, ctx, "new"))


def diff_node(o, n, ctx, out):
    if type(o) is not type(n):
        out.append(Change(o, ctx, "all"))
        return
    if not any(isinstance(getattr(o, f, None), list) for f in COMPOUND_BODIES):
        out.append(Change(o, ctx, "all", new=n))
        return
    if header_dump(o) != header_dump(n):
        out.append(Change(o, ctx, "header", new=n))
    if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
        inner = ctx + (("func", o.name),)
    elif isinstance(o, ast.ClassDef):
        inner = ctx + (("class", o.name),)
    else:
        inner = ctx
    for f in COMPOUND_BODIES:
        ol, nl = getattr(o, f, None), getattr(n, f, None)
        if not isinstance(ol, list) or not isinstance(nl, list):
            continue
        if f in ("handlers", "cases"):
            if len(ol) != len(nl):
                out.append(Change(o, ctx, "all"))
                continue
            for a, b in zip(ol, nl):
                if dump(a) != dump(b):
                    if header_dump(a) != header_dump(b):
                        out.append(Change(a, inner, "header"))
                    diff_lists(a.body, b.body, inner, a, out)
        else:
            diff_lists(ol, nl, inner, o, out)


EQUIV_OPS = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}


def singleton_like(node):
    """None, or an enum-style member such as NormalizationStrategy.UPPERCASE (not True/False: 1 == True)."""
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Attribute) and node.attr.isupper() and len(node.attr) > 1:
        base = node.value
        name = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""
        return name in PURE_ENUMS
    return False


# Enums that are also str or int compare equal to plain values: `"duckdb" == Dialects.DUCKDB` is True, `is` is False.
# In the pinned code only Dialects is compared against plain strings; TokenType, NormalizationStrategy,
# UsingColumnOrder and AliasPrecedence values always come from enum members (Dialect.__init__ converts settings).
MIXIN_ENUMS = {"Dialects"}
# `x is C.M` == `x == C.M` only when C is an enum: a class constant like Drill.DATE_FORMAT is a plain string.
# Every enum class in the pinned sqlglot (and DataType.Type, an alias of DType), minus the str one above.
PURE_ENUMS = {"TokenType", "DType", "Type", "ErrorLevel", "PropertiesLocation", "ScopeType", "IsPredicatePrecedence",
              "TrieResult", "NormalizationStrategy", "UsingColumnOrder", "AliasPrecedence"} - MIXIN_ENUMS


def same_compare(o, n):
    """`x is None` vs `x == None` (and enum members): same result for every type sqlglot compares."""
    if not (isinstance(o, ast.Compare) and isinstance(n, ast.Compare)) or len(o.ops) != len(n.ops):
        return False
    if dump(o.left) != dump(n.left) or [dump(c) for c in o.comparators] != [dump(c) for c in n.comparators]:
        return False
    operands = [o.left, *o.comparators]
    for i, (a, b) in enumerate(zip(o.ops, n.ops)):
        if type(a) is type(b):
            continue
        if (type(a), type(b)) not in EQUIV_OPS:
            return False
        if not (singleton_like(operands[i]) or singleton_like(operands[i + 1])):
            return False
    return True


def diff_spots(o, n):
    """Smallest old nodes that differ; a lambda counts as one unit."""
    if dump(o) == dump(n) or same_compare(o, n):
        return []
    if type(o) is not type(n) or isinstance(o, ast.Lambda):
        return [o]
    spots = []
    for (name, a), (_, b) in zip(ast.iter_fields(o), ast.iter_fields(n)):
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return [o]
            for x, y in zip(a, b):
                if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                    spots += diff_spots(x, y)
                elif x != y:
                    return [o]
        elif isinstance(a, ast.AST) and isinstance(b, ast.AST):
            spots += diff_spots(a, b)
        elif a != b:
            return [o]
    return spots


def header_spots(o, n, part):
    if part != "header":
        return diff_spots(o, n)
    spots = []
    for (name, a), (_, b) in zip(ast.iter_fields(o), ast.iter_fields(n)):
        if name in COMPOUND_BODIES:
            continue
        if isinstance(a, ast.AST) and isinstance(b, ast.AST):
            spots += diff_spots(a, b)
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for x, y in zip(a, b):
                spots += diff_spots(x, y) if isinstance(x, ast.AST) else ([o] if x != y else [])
        elif a != b:
            spots.append(o)
    return spots


def node_span(node, part):
    first = getattr(node, "lineno", 1)
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.decorator_list
    ):
        first = min(first, min(d.lineno for d in node.decorator_list))
    last = getattr(node, "end_lineno", first) or first
    if part == "header":
        body = getattr(node, "body", None)
        if body:
            last = max(first, body[0].lineno - 1)
    return first, last


HARMLESS_DECORATORS = {"property", "staticmethod", "classmethod", "cached_property", "abstractmethod", "overload"}


def simple_value(node):
    """A constant, a name or a dotted name: evaluating it at import cannot fail in a new way."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, (ast.Constant, ast.Name)) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant)
    )


def is_overload(node):
    """A def decorated with typing.overload (as overload, t.overload or typing.overload)."""
    for d in getattr(node, "decorator_list", ()):
        name = d.attr if isinstance(d, ast.Attribute) else d.id if isinstance(d, ast.Name) else None
        if name == "overload":
            return True
    return False


def _overload_only(decs):
    return all((d.attr if isinstance(d, ast.Attribute) else d.id if isinstance(d, ast.Name) else None) == "overload"
               for d in decs)


def _shadowed_later(tree, node):
    """node sits in a body where the next statements are only defs of its name, the last without @overload:
    that def replaces node's name before anything can read it."""
    for parent in ast.walk(tree):
        for f in COMPOUND_BODIES:
            body = getattr(parent, f, None)
            if isinstance(body, list) and any(s is node for s in body):
                i = next(k for k, s in enumerate(body) if s is node)
                for s in body[i + 1:]:
                    if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) or s.name != node.name:
                        return False
                    if not s.decorator_list:
                        return True
                    if not _overload_only(s.decorator_list):
                        return False
                return False
    return False


def overload_toggle(old_tree, new_tree, old, new):
    """Only a bare @overload added to or removed from a stub that a later plain def of the same name replaces
    (in the old and the new file): typing.overload just returns a dummy, and nothing in sqlglot or its tests
    reads typing's overload registry (no get_overloads), so no test can see the difference."""
    if type(old) is not type(new) or not isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if old.name != new.name or not (_overload_only(old.decorator_list) and _overload_only(new.decorator_list)):
        return False
    a, b = copy.copy(old), copy.copy(new)
    a.decorator_list, b.decorator_list = [], []
    if dump(a) != dump(b):
        return False
    return _shadowed_later(old_tree, old) and _shadowed_later(new_tree, new)


def import_time_risk(old, new):
    """Decorators and default values run when the module is imported: a new one there can break every importer."""
    olds = {dump(d) for d in old.decorator_list}
    for d in new.decorator_list:
        if dump(d) in olds:
            continue
        name = d.attr if isinstance(d, ast.Attribute) else d.id if isinstance(d, ast.Name) else None
        if name not in HARMLESS_DECORATORS:
            return True
    od = {dump(x) for x in old.args.defaults + [k for k in old.args.kw_defaults if k is not None]}
    for x in new.args.defaults + [k for k in new.args.kw_defaults if k is not None]:
        if dump(x) not in od and not simple_value(x):
            return True
    return False


_BUILTINS = set(dir(__import__("builtins"))) | {"__name__", "__file__", "__doc__", "__spec__", "__package__",
                                                 "__loader__", "__path__", "__builtins__", "__annotations__",
                                                 "__module__", "__qualname__", "__class__"}


def module_names(tree):
    """Every name a module defines at its top level (None if it star-imports)."""
    out = set(_BUILTINS)

    def visit(n):
        if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names):
            raise LookupError
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
            return
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
            return
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            out.add(n.id)
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        for ch in ast.iter_child_nodes(n):
            visit(ch)

    try:
        for st in tree.body:
            visit(st)
    except LookupError:
        return None
    return out


def import_loads(n, loads, stores, header_only=False):
    """Names read while this statement runs at import (function bodies and lambdas excluded)."""
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for d in n.decorator_list + n.args.defaults + [k for k in n.args.kw_defaults if k is not None]:
            import_loads(d, loads, stores)
        stores.add(n.name)
        return
    if isinstance(n, ast.Lambda):
        for d in n.args.defaults + [k for k in n.args.kw_defaults if k is not None]:
            import_loads(d, loads, stores)
        return
    if isinstance(n, ast.ClassDef):
        for d in n.decorator_list + n.bases + [k.value for k in n.keywords]:
            import_loads(d, loads, stores)
        stores.add(n.name)
        if not header_only:
            inner = set()
            for st in n.body:
                import_loads(st, loads, inner)
            stores |= inner
        return
    if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        for g in n.generators:
            for t in ast.walk(g.target):
                if isinstance(t, ast.Name):
                    stores.add(t.id)
    if isinstance(n, ast.Name):
        (loads if isinstance(n.ctx, ast.Load) else stores).add(n.id)
    for ch in ast.iter_child_nodes(n):
        import_loads(ch, loads, stores)


def target_names(node):
    """Names a module- or class-level statement defines or reads."""
    names = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            names.add((a.asname or a.name).split(".")[0])
    return names


def identifiers(node):
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
        elif isinstance(sub, ast.alias):
            out.add(sub.name.split(".")[-1])
            out.add(sub.name.split(".")[0])
    return out


def defined_names(body):
    out = set()
    for st in body:
        out |= target_names(st)
    return out


def bare_string(node):
    """A statement that is only a string (a docstring): no test reads __doc__, so it changes nothing."""
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def exp_class(node):
    """'IntDiv' for `exp.IntDiv`, else None."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "exp" and node.attr[:1].isupper():
        return node.attr
    return None


def entry_classes(stmt, spots):
    """If every changed spot sits in a dict entry or set/list element keyed by an `exp.X` class, return
    those class names; such an entry is only looked at when an X expression is handled."""
    parents = {}
    for node in ast.walk(stmt):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found = set()
    for spot in spots:
        hit = exp_class(spot) if isinstance(parents.get(spot), (ast.Dict, ast.Set, ast.List, ast.Tuple)) else None
        node = spot
        while not hit and node in parents:
            parent = parents[node]
            if isinstance(parent, ast.Dict):
                for k, v in zip(parent.keys, parent.values):
                    if node is k or node is v:
                        hit = exp_class(k)
            elif isinstance(parent, (ast.Set, ast.List, ast.Tuple)):
                hit = exp_class(node)
            node = parent
        if not hit:
            return None
        found.add(hit)
    return found


def bound_names(tree):
    """Every name bound anywhere in a module (with repeats): stores, defs, classes, imports, args."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            yield n.id
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield n.name
        elif isinstance(n, ast.alias):
            yield n.asname or n.name.split(".")[0]
        elif isinstance(n, ast.arg):
            yield n.arg
        elif isinstance(n, ast.ExceptHandler) and n.name:
            yield n.name
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            yield from n.names
        elif isinstance(n, ast.MatchAs) and n.name:
            yield n.name
        elif isinstance(n, ast.MatchStar) and n.name:
            yield n.name
        elif isinstance(n, ast.MatchMapping) and n.rest:
            yield n.rest


def import_time_loads(tree, name):
    """(load node, dotted name of the innermost enclosing class or "") for each load of `name` as a
    bare name or an attribute that runs while the module or a class body runs: function bodies and
    lambda bodies are skipped, their decorators and defaults are not."""
    out = []

    def visit(node, cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list + node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                visit(d, cls)
            if node.returns is not None:
                visit(node.returns, cls)
            for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + [node.args.vararg, node.args.kwarg]:
                if a is not None and a.annotation is not None:
                    visit(a.annotation, cls)
            return
        if isinstance(node, ast.Lambda):
            for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                visit(d, cls)
            return
        if isinstance(node, ast.ClassDef):
            for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                visit(d, cls)
            inner = f"{cls}.{node.name}" if cls else node.name
            for s in node.body:
                visit(s, inner)
            return
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            out.append((node, cls))
        elif isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.ctx, ast.Load):
            out.append((node, cls))
        for child in ast.iter_child_nodes(node):
            visit(child, cls)

    visit(tree, "")
    return out


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


# ---------------------------------------------------------------- selection


class Selector:
    def __init__(self, m: Map):
        self.m = m
        self.all = set(range(len(m.ids)))
        self._new_tree, self._defined = None, None
        self._listers = None

    def select(self, patch_text: str):
        tests: set = set()
        notes = []
        try:
            files = parse_patch(patch_text)
        except Exception as e:  # noqa: BLE001  a patch we cannot read: run everything
            return set(self.all), [f"unreadable patch {type(e).__name__}"]
        if not files and patch_text.strip():
            return set(self.all), ["no file headers found"]
        self._py_paths = {p for f in files for p in (f.get("old"), f.get("new")) if p and p.endswith(".py")}
        for f in files:
            try:
                got, why = self.select_file(f)
            except Exception as e:  # never crash: be safe instead
                got, why = self.all, f"error {type(e).__name__}: {e}"
            tests |= got
            notes.append(f"{f.get('new') or f.get('old')}: {why} -> {len(got)}")
        return tests, notes

    def select_file(self, f):
        old_path, new_path = f.get("old"), f.get("new")
        path = old_path or new_path or ""
        if (
            path in PYTEST_CONFIG
            or os.path.basename(path) == "conftest.py"
            or (new_path and os.path.basename(new_path) == "conftest.py")
        ):
            return self.all, "pytest config"
        if old_path and new_path and old_path != new_path:
            got, _ = (
                self.select_file({**f, "old": new_path, "new": new_path})
                if (REPO / new_path).exists()
                else (set(), "")
            )
            return self.gone(old_path) | got | self.listers(old_path) | self.listers(new_path), "rename"
        if old_path is None:
            if (
                new_path
                and new_path.endswith(".py")
                and (new_path.startswith("sqlglot/") or new_path.startswith("tests/"))
            ):
                # a new module can shadow a lazily imported one (e.g. a new package dir)
                return self.shadow(new_path), "new file"
            return set(), "new file"
        if not (REPO / old_path).exists():
            if old_path.startswith(("sqlglot/", "tests/")) or self.m.file_tests(old_path) or self.m.open_tests(old_path):
                return set(self.all), "unknown file in sqlglot or tests"  # e.g. a path we misread
            return set(), "unknown file"
        if new_path is None:
            return self.gone(old_path) | self.listers(old_path), "deleted"
        if f.get("binary"):
            return self.gone(old_path), "binary"
        if f.get("stray"):
            return self.m.file_tests(old_path) | self.m.open_tests(old_path) or set(self.all), "malformed hunk counts"
        if old_path.endswith(".py") and old_path.startswith(("sqlglot/", "tests/")):
            return self.python(old_path, f["hunks"])
        if old_path.startswith(("tests/", "sqlglot/")):
            return self.m.open_tests(old_path) or self.data_fallback(
                old_path
            ), "data file"
        # outside sqlglot and tests: only tests that ran or opened the file can notice
        return self.m.file_tests(old_path) | self.m.open_tests(old_path), "outside sqlglot and tests"

    def shadow(self, path):
        stem = path[:-3]
        if stem.endswith("/__init__"):
            stem = stem[: -len("/__init__")]
        return self.m.file_tests(stem + ".py") | self.listers(path)

    def listers(self, path):
        """Tests that find modules by listing a folder (pkgutil, os.listdir, glob) and ran a file in this one:
        a file added to or removed from the folder changes what they see."""
        if self._listers is None:
            import re

            pat = re.compile(r"iter_modules|walk_packages|listdir|scandir|\.glob\(|\.iterdir\(|os\.walk")
            files = {tid.split("::")[0] for tid in self.m.ids}
            hit = set()
            for tf in files:
                try:
                    if pat.search((REPO / tf).read_text(encoding="utf-8", errors="ignore")):
                        hit.add(tf)
                except OSError:
                    pass
            self._listers = {i for i, tid in enumerate(self.m.ids) if tid.split("::")[0] in hit}
        folder = os.path.dirname(path) + "/"
        near = set()
        for f in self.m.touched:
            if f.startswith(folder) and "/" not in f[len(folder):]:
                near |= self.m.file_tests(f)
        return near & self._listers

    def data_fallback(self, path):
        """Data file no test opened from Python (e.g. read by duckdb): tests whose file names it."""
        base = os.path.basename(path)
        got = set()
        for tf in {tid.split("::")[0] for tid in self.m.ids}:
            p = REPO / tf
            if p.exists() and base.split(".")[0] in p.read_text(
                encoding="utf-8", errors="ignore"
            ):
                got |= {
                    i for i, tid in enumerate(self.m.ids) if tid.startswith(tf + "::")
                }
        return got

    def gone(self, path):
        if path.endswith(".py"):
            return self.m.file_tests(path) or (
                self.all if path.startswith(("sqlglot/", "tests/")) else set()
            )
        return self.m.open_tests(path) or self.data_fallback(path)

    def python(self, path, hunks):
        old_text = (REPO / path).read_text(encoding="utf-8")
        for h in hunks:  # an encoding line, BOM or NUL changes how Python reads the file: not a comment edit
            for ln in h["old"] + h["new"]:
                if "\ufeff" in ln or "\x00" in ln or "\ufffd" in ln or ("coding" in ln and h["start"] <= 3 and ln.lstrip().startswith("#")):
                    return self.m.file_tests(path) or set(self.all), "encoding line"
        if not hunks_match(old_text, hunks):
            for h in hunks:  # CRLF patches: try again without the carriage returns
                for k in ("old", "new"):
                    h[k] = [s[:-1] if s.endswith("\r") else s for s in h[k]]
            if not hunks_match(old_text, hunks):
                return self.m.file_tests(path) or set(self.all), "patch does not match the file"
        new_text = apply_hunks(old_text, hunks)
        old_tree = ast.parse(old_text)
        try:
            new_tree = ast.parse(new_text)
        except (SyntaxError, ValueError):
            return self.m.file_tests(path), "syntax error"
        Lines.load(hunks, old_text.count("\n") + 1)
        self._new_tree, self._defined = new_tree, module_names(new_tree)
        self._old_tree = old_tree
        changes: list = []
        diff_lists(old_tree.body, new_tree.body, (), old_tree, changes)
        if not changes:
            return set(), "no code change"
        tests: set = set()
        whys = []
        for c in changes:
            got, why = self.change_tests(path, c)
            tests |= got
            whys.append(why)
        return tests, ",".join(whys)

    def change_tests(self, path, c):
        m = self.m
        if bare_string(c.node) and (c.new is None or bare_string(c.new)) and c.part in ("all", "new"):
            return set(), "docstring"
        in_func = any(kind == "func" for kind, _ in c.ctx)
        node = c.node
        if not in_func and c.part != "insert" and self.undefined_at_import(c):
            return m.file_tests(path), "name not defined at import"
        if c.part == "new":
            # a statement only in the new file: at class/module level it can define or override names
            if in_func:
                return set(), "new line"
            if isinstance(node, (ast.Import, ast.ImportFrom)) or (m.known and identifiers(node) - m.known):
                return m.file_tests(path), "new top statement may break import"
            if isinstance(node, ast.ClassDef) or getattr(node, "decorator_list", None):
                # a new class registers itself at import (metaclass, __init_subclass__, subclass scans such
                # as the parser's function table) and a decorator runs at import: nobody needs to read its name
                return m.file_tests(path) | self.listers(path), "new class or decorated def"
            if any(n.startswith("__") and n.endswith("__") for n in target_names(node)):
                # __all__, __slots__, __bool__, __eq__ ...: used implicitly by Python, never read by name
                return m.file_tests(path), "new dunder name"
            if c.ctx and c.ctx[-1][0] == "class" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # a new method is found through dir()/getattr (e.g. the generator's *_sql dispatch), not by name
                return self.class_tests(path, c.ctx), "new method"
            names = target_names(node)
            return self.scope_tests(path, c.ctx, names or None), "new top statement"
        if c.part == "insert":
            prev, nxt = c.neighbors
            if in_func or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                got = set()
                for nb in (prev, nxt):
                    if nb is not None:
                        got |= m.line_tests(path, *node_span(nb, "all"))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # the def line runs at import for everyone; whoever calls the function runs a body line
                    body = m.line_tests(path, node.body[0].lineno, node.end_lineno)
                    got |= body or m.line_tests(path, *node_span(node, "header"))
                elif not isinstance(node, ast.Module):
                    got |= m.line_tests(path, *node_span(node, "header"))
                return got, "insert in function"
            return set(), "insert at top"
        if c.new is not None and not header_spots(node, c.new, c.part):
            return set(), "equivalent edit"
        if c.part == "header" and c.new is not None and overload_toggle(self._old_tree, self._new_tree, node, c.new):
            return set(), "overload stub"
        if in_func:
            return m.line_tests(path, *node_span(node, c.part)), "function line"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = m.line_tests(path, node.body[0].lineno, node.end_lineno)
            if c.part == "all":
                # replaced, renamed or deleted def: whoever reads the name too
                return body | (m.name_tests(node.name) or set()) | self.scope_tests(
                    path, c.ctx, None
                ), "def changed"
            if c.new is not None and import_time_risk(node, c.new):
                return body | m.file_tests(path), "def header runs at import"
            if c.new is not None and is_overload(node) and is_overload(c.new) and node.name == c.new.name:
                # a typing.overload stub never runs: the next def of the same name replaces it at import, and
                # its new defaults/decorators passed import_time_risk above (nothing reads typing's registry)
                return set(), "overload stub"
            return body, "def header"
        if isinstance(node, ast.ClassDef):
            inner = self.class_tests(path, c.ctx + (("class", node.name),))
            if c.part == "header":
                # bases, keywords and decorators run at import and change every subclass and registry
                return inner | m.file_tests(path), "class header"
            return inner | self.scope_tests(path, c.ctx, {node.name}), "class changed"
        if isinstance(node, (ast.Import, ast.ImportFrom)) and not c.ctx:
            return m.file_tests(path), "import changed"
        if c.new is not None and m.known and identifiers(c.new) - m.known:
            return m.file_tests(path), "unknown name at import time"
        if c.new is not None:
            # each rule below gives a safe superset; keep what all of them agree on
            got, whys = self.scope_tests(path, c.ctx, target_names(node) or None), ["top statement"]
            olds = diff_spots(node, c.new)
            news = diff_spots(c.new, node)
            keys_old = entry_classes(node, olds) if olds else None
            keys_new = entry_classes(c.new, news) if news else None
            if keys_old is not None and keys_new is not None:
                users = set()
                for name in keys_old | keys_new:
                    users |= m.exp_users(name)
                got &= users
                whys.append("table entry " + ",".join(sorted(keys_old | keys_new)))
            if m.rlines is not None and olds and all(isinstance(sp, ast.Lambda) for sp in olds):
                lam = set()
                for sp in olds:
                    lam |= m.line_tests(path, sp.lineno, sp.end_lineno, runtime=True)
                got &= lam
                whys.append("lambda")
            return got, "+".join(whys)
        names = target_names(node)
        if c.new is None and names and self.plain_class_constant(path, c, node, names):
            # a deleted class constant now resolves to the base class's value (or raises): the same as
            # changing its value, so only its run-time readers among the class's users can see it
            if not any(n in m.import_reads for n in names):
                return self.scope_tests(path, c.ctx, names), "class constant deleted"
            # read while modules or class bodies run, but only as other classes' attributes
            # (import_reads_elsewhere): only run-time reads can see the edited class lose it
            readers = set()
            for n in names:
                readers |= m._get(m.names_rt, n) or set()
            return readers & self.class_tests(path, c.ctx), "class constant deleted (import reads elsewhere)"
        if c.new is None and names:
            # deleted: anything that reads the name may now crash, even at import
            readers = set()
            for n in names:
                readers |= m.name_tests(n) or set()
            return readers | self.scope_tests(path, c.ctx, None), "top statement deleted"
        return self.scope_tests(path, c.ctx, names or None), "top statement"

    def plain_class_constant(self, path, c, node, names):
        """`NAME = <value without calls>` in a plain class body (no enum or named-tuple ancestor, no
        decorators), where nothing reads NAME while modules or class bodies run, no dir() dispatch name,
        and no code or test names it in a string (`__dict__.get("NAME")`, getattr, hasattr)."""
        m = self.m
        if m.names_rt is None or not c.ctx or c.ctx[-1][0] != "class" or any(k != "class" for k, _ in c.ctx):
            return False
        if not isinstance(node, ast.Assign) or not all(isinstance(t, ast.Name) for t in node.targets):
            return False
        if any(isinstance(x, (ast.Call, ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom)) for x in ast.walk(node.value)):
            return False
        for n in names:
            if not n.isupper() or n.startswith("_") or n.endswith("_sql"):
                return False
            if n in m.import_reads and not self.import_reads_elsewhere(path, c, n):
                return False
        texts, bases = self._class_index()
        for n in names:
            if f'"{n}"' in texts or f"'{n}'" in texts:
                return False
        # the class and every ancestor must be found in the repo, ending in classes with no bases
        cls = self._new_tree
        for kind, name in c.ctx:
            cls = next((s for s in getattr(cls, "body", ()) if isinstance(s, ast.ClassDef) and s.name == name), None)
            if cls is None:
                return False
        if cls.decorator_list:
            return False
        todo = [ast.unparse(b) for b in cls.bases]
        seen = set()
        while todo:
            b = todo.pop().split("[")[0].split(".")[-1].strip()
            if b in seen or b == "object":
                continue
            seen.add(b)
            if "Enum" in b or b in ("Flag", "IntFlag", "NamedTuple", "TypedDict", "Protocol") or b not in bases:
                return False
            todo.extend(bases[b])
        return True

    def import_reads_elsewhere(self, path, c, name):
        """True when every load of `name` made while a sqlglot or test module or class body runs provably
        reads something other than the edited class (or a subclass): a bare name in another class's body
        or at module level (class scopes do not nest or inherit), or `K.name` / `mod.K.name` where K is a
        top-level class, found by its import, that is neither the edited class nor below it. Anything
        else (an alias, `cls.name`, a call result, a relative import, a name bound twice) says no.
        Only for a patch whose one Python file is this one, so every other file is as at eb11aec."""
        if getattr(self, "_py_paths", None) != {path}:
            return False
        qual = ".".join(nm for kind, nm in c.ctx if kind == "class")
        me = (module_name(path), qual)
        texts, bases = self._class_index()
        low, todo = set(), [qual.split(".")[-1]]
        while todo:  # every class name below the edited one (by bare name: over-counts, the safe side)
            b = todo.pop()
            if b in low:
                continue
            low.add(b)
            todo.extend(k for k, bs in bases.items() if any(x.split("[")[0].split(".")[-1].strip() == b for x in bs))
        if getattr(self, "_top_classes", None) is None:
            self._top_classes = {}

        def top_classes(mod):
            if mod not in self._top_classes:
                got = None
                f = REPO / (mod.replace(".", "/") + ".py")
                if not f.exists():
                    f = REPO / mod.replace(".", "/") / "__init__.py"
                if f.exists():
                    try:
                        t = ast.parse(f.read_text(encoding="utf-8"))
                        cnt = {}
                        for b in bound_names(t):
                            cnt[b] = cnt.get(b, 0) + 1
                        got = {s.name for s in t.body if isinstance(s, ast.ClassDef) and cnt.get(s.name) == 1}
                    except (OSError, SyntaxError, ValueError):
                        got = None
                self._top_classes[mod] = got
            return self._top_classes[mod]

        def ok_class(mod, k):
            tops = top_classes(mod)
            if not mod.startswith("sqlglot"):
                return False  # tests' classes are not in the subclass index
            return tops is not None and k in tops and (mod, k) != me and k not in low

        for top in ("sqlglot", "tests"):
            for p in sorted((REPO / top).rglob("*.py")):
                rel = p.relative_to(REPO).as_posix()
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    return False
                if name not in text:
                    continue
                try:
                    tree = self._new_tree if rel == path else ast.parse(text)
                except (SyntaxError, ValueError):
                    return False
                loads = import_time_loads(tree, name)
                if any(isinstance(ld, ast.Attribute) for ld, _ in loads):
                    cnt, mods, classes = self._file_info(rel, tree)
                for load, inside in loads:
                    if isinstance(load, ast.Name):
                        if rel == path and inside == qual:
                            return False  # the edited class's own body reads it
                        continue
                    v = load.value
                    if isinstance(v, ast.Name) and cnt.get(v.id) == 1 and v.id in classes:
                        mod, k = classes[v.id]
                    elif (
                        isinstance(v, ast.Attribute)
                        and isinstance(v.value, ast.Name)
                        and cnt.get(v.value.id) == 1
                        and v.value.id in mods
                    ):
                        mod, k = mods[v.value.id], v.attr
                    else:
                        return False
                    if not ok_class(mod, k):
                        return False
        return True

    def _file_info(self, rel, tree):
        """For one module: {bound name: times bound}, {local name: imported sqlglot module},
        {local name: (module, class name)} for top-level classes and names imported from modules."""
        cache = self.__dict__.setdefault("_file_infos", {})
        if rel in cache and tree is not self._new_tree:
            return cache[rel]
        cnt, mods, classes = {}, {}, {}
        for b in bound_names(tree):
            cnt[b] = cnt.get(b, 0) + 1
        this = module_name(rel)
        for st in tree.body:
            if isinstance(st, ast.ClassDef):
                classes[st.name] = (this, st.name)
        for st in ast.walk(tree):
            if isinstance(st, ast.ImportFrom) and st.level == 0 and st.module:
                for a in st.names:
                    local = a.asname or a.name
                    sub = f"{st.module}.{a.name}"
                    if (REPO / (sub.replace(".", "/") + ".py")).exists() or (REPO / sub.replace(".", "/") / "__init__.py").exists():
                        mods[local] = sub
                    else:
                        classes[local] = (st.module, a.name)
            elif isinstance(st, ast.Import):
                for a in st.names:
                    if a.asname:
                        mods[a.asname] = a.name
        if tree is not self._new_tree:
            cache[rel] = (cnt, mods, classes)
        return cnt, mods, classes

    def _class_index(self):
        """All sqlglot and test source text (for string mentions) and {class name: base names} from sqlglot."""
        if getattr(self, "_classes_idx", None) is None:
            import re

            pat = re.compile(r"^[ \t]*class[ \t]+(\w+)[ \t]*(?:\(([^)]*)\))?[ \t]*:", re.M)
            parts, bases = [], {}
            for top in ("sqlglot", "tests"):
                for p in sorted((REPO / top).rglob("*.py")):
                    try:
                        text = p.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    parts.append(text)
                    if top != "sqlglot":
                        continue
                    for name, args in pat.findall(text):
                        got = bases.setdefault(name, [])
                        for a in args.split(","):
                            a = a.strip()
                            if a and "=" not in a and not a.startswith("*"):
                                got.append(a)
            self._classes_idx = ("\n".join(parts), bases)
        return self._classes_idx

    def undefined_at_import(self, c):
        """A changed module- or class-level statement that loads, while the module imports, a name the
        module never defines or imports: importing it raises NameError, so every importer fails."""
        defined = self._defined
        if defined is None:
            return False  # `from x import *`: cannot tell
        stmt = c.new if c.new is not None else (c.node if c.part == "new" else None)
        if stmt is None:
            return False
        loads, stores = set(), set()
        import_loads(stmt, loads, stores, header_only=c.part == "header")
        scope = set(defined) | stores
        body = self._new_tree.body
        for kind, name in c.ctx:  # names set earlier in the enclosing class bodies
            cls = next((n for n in body if isinstance(n, ast.ClassDef) and n.name == name), None)
            if cls is None:
                return False
            for st in cls.body:
                scope |= target_names(st)
            body = cls.body
        return bool(loads - scope)

    def class_tests(self, path, ctx):
        qual = ".".join(name for kind, name in ctx if kind == "class")
        got = self.m.class_tests(f"{module_name(path)}:{qual}")
        return self.m.file_tests(path) if got is None else got

    def scope_tests(self, path, ctx, names):
        """Tests affected by a change to a class-level or module-level statement."""
        m = self.m
        in_class = bool(ctx) and ctx[-1][0] == "class"
        users = self.class_tests(path, ctx) if in_class else m.file_tests(path)
        if names:
            readers = set()
            for n in names:
                got = m.reader_tests(n)
                if got is None:
                    return users
                readers |= got
            return readers & users
        return users


# ---------------------------------------------------------------- packing


def pack(tests, durations):
    """Cheapest split over 1-8 workers: largest test first onto the lightest worker."""
    if not tests:
        return []
    order = sorted(tests, key=lambda t: (-durations[t], t))
    best, best_cost = None, None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for t in order:
            i = loads.index(min(loads))
            workers[i].append(t)
            loads[i] += durations[t]
        cost = max(loads) + WORKER_MS * k
        if best_cost is None or cost < best_cost:
            best, best_cost = workers, cost
    return best


def pad_order(m, patch_text, number_of, chosen):
    """Unselected tests to slip into idle worker time, best first: tests that ran lines within 3 of an
    edit, then tests that touched an edited file, then the edited module's own test file. Free insurance:
    padding never raises the busiest worker or adds one, so it costs 0 s and can only catch a miss."""
    near, touched, own = [], [], []
    try:
        for f in parse_patch(patch_text):
            path = f.get("old") or f.get("new")
            if not path:
                continue
            spots = []
            for h in f["hunks"]:
                ln = h["start"]
                for tag in h["seq"]:
                    if tag == "-":
                        spots.append(ln)
                        ln += 1
                    elif tag == "+":
                        spots.append(ln)
                    else:
                        ln += 1
            for s in spots:
                near.append(m.line_tests(path, max(1, s - 3), s + 3))
            touched.append(m.file_tests(path))
            if path.startswith("sqlglot/") and path.endswith(".py"):
                stem = Path(path).stem
                own.append({i for i, tid in enumerate(m.ids) if tid.startswith((f"tests/test_{stem}.py::", f"tests/dialects/test_{stem}.py::"))})
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set(chosen)
    for group in (near, touched, own):
        for ids in group:
            for i in sorted(ids):
                n = number_of.get(i)
                if n is not None and n not in seen:
                    seen.add(n)
                    out.append(n)
    return out


def pad(workers, durations, extra):
    """Fill each worker up to the busiest one's load with tests from extra (in order), never past it."""
    if len(workers) < 2 or not extra:
        return workers
    loads = [sum(durations[t] for t in w) for w in workers]
    top = max(loads)
    n_top = len(workers[loads.index(top)])
    for t in extra:
        d = durations.get(t)
        if d is None:
            continue
        i = min(range(len(workers)), key=lambda k: loads[k])
        # durations are rounded to ms: allow 0.5 ms of error per test on both workers, plus 1 ms
        if loads[i] + d + (len(workers[i]) + 1 + n_top) // 2 + 1 <= top:
            workers[i].append(t)
            loads[i] += d
    return workers


def pack_cost(tests, durations):
    ws = pack(tests, durations)
    return max(sum(durations[t] for t in w) for w in ws) + WORKER_MS * len(ws) if ws else 0


# ---------------------------------------------------------------- run-time check

VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
PAD = os.environ.get("BR_PAD", "1") == "1"  # free insurance in idle worker time
VERIFY_MAX_MS = 1000  # tests slower than this are kept without running them
RUN_RATIO = 0.75  # in-process run time / recorded time, measured ~0.6
# tests that read sqlglot from disk (a subprocess, runpy, source read as text) never see in-memory patches
NEVER_VERIFY = {
    "tests/dialects/test_dialect.py::TestDialect::test_lazy_load",
    "tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata",
}
NEVER_VERIFY_FILES = ("tests/test_sqlglotc.py", "tests/leakcheck.py")
HEAVY_IMPORT_MS = 1900  # duckdb + pandas, needed by the optimizer and executor tests (once)
HEAVY_MODULES = {"tests.test_optimizer", "tests.test_executor"}
HEAVY_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")
SLOW_SETUP = "tests/test_executor.py::TestExecutor::"
SLOW_SETUP_MS = 2200
SLOW_SETUP_CACHED_MS = 200  # duckdb views + sqlglot Table + SQL fixtures, rows from the cache
# Off: the board showed the fakes mislead on changes that alter which tests are checked (28beab2), so the cache only
# lowers CPU and every choice stays as in c174142. BR_SETUP_ONCE=1 also prices the cached set-up (fakes: a tie).
SETUP_ONCE = os.environ.get("BR_SETUP_ONCE", "0") == "1" and os.environ.get("BR_EXEC_CACHE", "1") == "1"
AFTER_FAIL_MIN_MS = float(os.environ.get("BR_AFTER_FAIL_MIN_MS", "1e9"))  # failing tests run slowly: stop at the first  # TestExecutor.setUpClass, paid in every child that runs one

# The slowest tests are never run as they are (a run costs as much CPU as it saves). A stand-in runs the
# same code on a smaller input (standins.py); if it passes on the patched code, the real test is dropped.
STANDINS = {
    "tests/test_parser.py::TestParser::test_max_nodes": ("standins.py::MaxNodes::test_small", 1200),
    "tests/test_generator.py::TestGenerator::test_generator_memory": ("standins.py::GeneratorMemory::test_small", 2500),
}
TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCDS_STANDIN = "standins_tpcds.py::Tpcds::test_q"
TPCDS_MAX_MS = int(os.environ.get("BR_TPCDS_MAX_MS", "6000"))  # CPU budget for its queries
PROBE_MIN = 3
# A probe (some of the queries that run the changed code) drops the real test only when the commit's other
# candidates were checked too and all passed. In ~200 labelled commits tpcds never failed alone.
TPCDS_PROBE = os.environ.get("BR_TPCDS_PROBE", "0") == "1"  # measured: ~14 s saved for ~14 s CPU on 102 fakes, so off
STANDIN_OFF = os.environ.get("BR_STANDINS", "1") != "1"


TPCDS_FP = os.environ.get("BR_TPCDS_FP", "1") == "1"
TPCDS_FP_ID = "standins_tpcds_fp.py::TpcdsPrint::test_q"


class TpcdsPrint:
    """Which functions run only in TPC-DS's compile stages (tools/trace_tpcds_fp.py): a change confined to
    them is proven harmless by equal fingerprints (standins_tpcds_fp.py), with no DuckDB and no data."""

    def __init__(self):
        self.ok = False
        for p in (MAP.parent / "tpcds_fp.json", Path(__file__).parent / "map" / "tpcds_fp.json"):
            if p.exists():
                d = json.loads(p.read_text())
                self.cg, self.e = set(d["cg"]), set(d["e"]) | set(d.get("both", ()))
                self.ok = True
                return

    def plan(self, changed, tmap):
        """(query numbers, est ms) to fingerprint, or None when the proof does not apply."""
        if not self.ok or not changed or changed & self.e:
            return None
        # functions the test never starts cannot change its answer (one that ran at import time is refused
        # by the checker itself); what is left must run only in the compile stages
        changed = changed & self.cg
        if not changed:
            return None
        qs = set()
        if tmap is not None and tmap.ok:
            for name in changed:
                for q in tmap.users.get(name, ()):
                    qs.add(q["i"])
        else:
            return None
        if not qs:
            return None
        return sorted(qs), 300 + 220 * len(qs)  # measured: ~0.13 s optimize + ~0.08 s replay and dumps per query


class TpcdsMap:
    """Which TPC-DS queries run which sqlglot functions (tools/trace_tpcds.py on the pinned commit)."""

    def __init__(self):
        self.ok = False
        for p in (os.environ.get("BR_TPCDS_MAP"), MAP.parent / "tpcds.json", Path(__file__).parent / "map" / "tpcds.json"):
            if p and Path(p).exists():
                d = json.loads(Path(p).read_text())
                self.funcs = d["funcs"]  # index -> "path::qualname"
                self.common = set(d["common"])  # function indexes every run uses (loading and shipping tables)
                self.queries = d["queries"]  # [{"i", "ms", "funcs": [idx], "tables": [...]}]
                self.table_ms = d["table_ms"]
                self.base_ms = d["base_ms"]
                self.users: dict = {}
                for q in self.queries:
                    for f in q["funcs"]:
                        self.users.setdefault(self.funcs[f], []).append(q)
                self.common_names = {self.funcs[f] for f in self.common}
                self.ok = True
                return

    def cost(self, qs):
        tables = set().union(*(q["tables"] for q in qs)) if qs else set()
        return self.base_ms + sum(q["ms"] for q in qs) + sum(self.table_ms.get(t, 200) for t in tables)

    def plan(self, changed, budget):
        """changed: 'path::qualname' of every changed function.

        (query numbers, est ms, exact?) or None. Exact: every query that runs a changed function.
        Otherwise a probe within the budget: for each changed function the cheapest query that runs it,
        then the cheapest other such queries (at least PROBE_MIN queries when there are that many)."""
        if not self.ok or not changed:
            return None
        if changed & self.common_names:
            return None  # every query could see it: as dear as the real test
        hit = {}
        for name in changed:
            for q in self.users.get(name, ()):
                hit[q["i"]] = q
        if not hit:
            return None  # the map says tpcds runs none of them: nothing to check, keep the test (be safe)
        if self.cost(list(hit.values())) <= budget:
            return sorted(hit), self.cost(list(hit.values())), True
        pick = {}
        for name in sorted(changed):
            qs = self.users.get(name)
            if qs:
                q = min(qs, key=lambda q: (q["ms"], q["i"]))
                pick[q["i"]] = q
        for q in sorted(hit.values(), key=lambda q: (q["ms"], q["i"])):
            if q["i"] in pick:
                continue
            if len(pick) >= PROBE_MIN and self.cost(list(pick.values()) + [q]) > budget:
                break
            pick[q["i"]] = q
        ms = self.cost(list(pick.values()))
        if ms > 1.5 * budget:
            return None
        return sorted(pick), ms, False


class Checker:
    """Decides, per commit, whether running the cheap candidates beats just selecting them."""

    def __init__(self, ids_by_number, durations):
        self.ids = ids_by_number
        self.durations = durations
        self.v = None
        self.proc = None
        self.starts = 0
        self.no_heavy = False
        self.heavy_paid = False
        self.setup_paid = False
        self.tpcds = None
        self.tpcds_fp = None
        self.number_of = {v: k for k, v in ids_by_number.items()}

    def verifier(self):
        if self.v is None:
            import sysconfig

            from fastverify import Verifier

            self.v = Verifier(str(REPO), sysconfig.get_paths()["purelib"], str(IMPORTS))  # static use only
        return self.v

    def files(self, patch_text):
        out = {}
        for f in parse_patch(patch_text):
            path = f.get("old") or f.get("new")
            if not f.get("old") or not f.get("new") or f.get("binary") or f["old"] != f["new"]:
                return None
            p = REPO / f["old"]
            if not p.exists():
                return None
            old = p.read_text(encoding="utf-8")
            if not hunks_match(old, f["hunks"]):
                return None  # we may have misread the patch: never run tests against a guess
            out[path] = (old, apply_hunks(old, f["hunks"]))
        return out

    def candidates(self, numbers, limit=None):
        d = self.durations
        limit = limit or VERIFY_MAX_MS
        # optimizer/executor tests are checked again: on the hidden practice set, keeping them unchecked (28beab2)
        # cost 23 s of tests for 19 s of CPU saved (197.0 -> 201.1 s). BR_SKIP_HEAVY=1 turns the checks off.
        skip = ("tests/test_optimizer.py", "tests/test_executor.py") if os.environ.get("BR_SKIP_HEAVY", "0") == "1" else ("\0",)
        return [
            t
            for t in numbers
            if d[t] < limit and self.ids.get(t) and self.ids[t] not in NEVER_VERIFY and not self.ids[t].startswith(skip)
            and not self.ids[t].startswith(NEVER_VERIFY_FILES)
        ]

    def standins(self, files, pairs, numbers):
        """[(real test number, [stand-in ids], est ms, heavy?)] for the slow tests among numbers."""
        if STANDIN_OFF:
            return []
        out = []
        for t in numbers:
            tid = self.ids.get(t)
            if tid in STANDINS and tid.split("::")[0] not in files:
                sid, ms = STANDINS[tid]
                out.append((t, [sid], ms, False))
            elif tid == TPCDS and pairs and "tests/test_executor.py" not in files:
                if self.tpcds is None:
                    self.tpcds = TpcdsMap()
                changed = {f"{p}::{o.co_qualname}" for p, _fn, o, _n, _own in pairs}
                if TPCDS_FP:
                    if self.tpcds_fp is None:
                        self.tpcds_fp = TpcdsPrint()
                    fp = self.tpcds_fp.plan({f"{p}::{o.co_qualname}" for p, _fn, o, _n, own in pairs if own}, self.tpcds)
                    if fp:
                        out.append((t, [f"{TPCDS_FP_ID}{i}" for i in fp[0]], fp[1], False))
                        continue
                if self.no_heavy:
                    continue
                got = self.tpcds.plan(changed, TPCDS_MAX_MS)
                if got and (got[2] or TPCDS_PROBE):
                    # exact -> heavy=True; a probe is marked "probe" and needs other candidates beside it
                    out.append((t, [f"{TPCDS_STANDIN}{i}" for i in got[0]], got[1], True if got[2] else "probe"))
        return out

    def estimate(self, patch_text, numbers):
        """(gain ms, run cost ms, files, test modules, tests to run, reload size, stand-ins) for checking this
        commit, or None."""
        d = self.durations
        slow = [t for t in numbers if self.ids.get(t) in STANDINS or self.ids.get(t) == TPCDS]
        if not self.candidates(numbers, 7000) and not slow:
            return None
        files = self.files(patch_text)
        if files is None:
            return None
        from fastverify import Verifier

        reload_n = 0
        pairs = Verifier.static_pairs(str(REPO), files)
        if pairs is None:
            reload = self.verifier().reload_set(files)
            if reload is None:
                return None
            reload_n = len(reload[1])
        sts = self.standins(files, pairs, numbers) if slow else []
        best = None
        changed = set(files)
        options = []
        for limit in (1000, 2600, 7000):
            base = [t for t in self.candidates(numbers, limit) if self.ids[t].split("::")[0] not in changed]
            options.append(base)
            # leave out tests whose class set-up is slow (they are kept, not run)
            options.append([t for t in base if not self.ids[t].startswith(SLOW_SETUP)])
        uses = [[]]
        if sts:
            uses.append(sts)
            if len(sts) > 1:
                uses += [[s] for s in sts]
        for cand in options + [[]]:
            for use in uses:
                use = [s for s in use if s[0] not in cand]  # the real test is run: no stand-in needed
                if not cand:
                    use = [s for s in use if s[3] != "probe"]  # a probe never decides alone
                if not cand and not use:
                    continue
                keep = set(numbers) - set(cand) - {s[0] for s in use}
                gain = pack_cost(numbers, d) - pack_cost(keep, d)
                est = 60 + RUN_RATIO * sum(d[t] for t in cand) + 5 * reload_n + sum(s[2] for s in use)
                if any(self.ids[t].startswith(SLOW_SETUP) for t in cand):
                    # the checker reads the executor's tables once and reuses them (fastverify._exec_rows)
                    cached = SETUP_ONCE and self.setup_paid and not any(p.startswith("tests/") for p in files)
                    est += SLOW_SETUP_CACHED_MS if cached else SLOW_SETUP_MS
                heavy = any(self.ids[t].startswith(HEAVY_FILES) for t in cand) or any(s[3] for s in use)
                if not self.heavy_paid and heavy:
                    est += HEAVY_IMPORT_MS
                if est > GAIN_SHARE * gain:
                    continue
                if best is None or gain - est > best[0] - best[1]:
                    mods = {self.ids[t].split("::")[0][:-3].replace("/", ".") for t in cand}
                    mods |= {sid.split("::")[0][:-3] for s in use for sid in s[1]}
                    best = (gain, est, files, mods, cand, reload_n, use)
        if best is None or best[0] < 1000:
            return None
        if any(self.ids[t].startswith(HEAVY_FILES) for t in best[4]) or any(s[3] for s in best[6]):
            self.heavy_paid = True
        if any(self.ids[t].startswith(SLOW_SETUP) for t in best[4]) and not any(p.startswith("tests/") for p in files):
            self.setup_paid = True
        return best

    def server(self):
        """The checking process (started once, again after a crash, at most twice)."""
        import subprocess
        import sysconfig

        if self.proc is not None and self.proc.poll() is None:
            return self.proc
        if self.starts >= 3:
            return None
        self.starts += 1
        here = os.path.dirname(os.path.abspath(__file__))
        self.proc = subprocess.Popen(
            [sys.executable, "-S", os.path.join(here, "fastverify.py"), str(REPO), sysconfig.get_paths()["purelib"], str(IMPORTS)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
            env=dict(os.environ, PYTHONPATH=""),
            start_new_session=True,  # its own process group: a kill also stops the test child it forked
        )
        return self.proc

    def kill(self):
        """Stop the checking process and any test child it forked."""
        import signal

        p = self.proc
        if p is None:
            return
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def ask(self, proc, req, timeout):
        """One request; None if the server dies or takes too long (then it is stopped)."""
        import select
        import time

        proc.stdin.write((json.dumps(req) + "\n").encode())
        proc.stdin.flush()
        buf = b""
        end = time.monotonic() + timeout
        while not buf.endswith(b"\n"):
            left = end - time.monotonic()
            if left <= 0 or not select.select([proc.stdout], [], [], left)[0]:
                self.kill()
                return None
            chunk = os.read(proc.stdout.fileno(), 1 << 20)
            if not chunk:
                return None
            buf += chunk
        return json.loads(buf)

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self.kill()  # and any test child still running

    def check(self, files, numbers, cand, use=()):
        d = self.durations
        heavy = [t for t in cand if self.ids[t].startswith(HEAVY_FILES)]
        if self.no_heavy:
            use = [s for s in use if not s[3]]
            if heavy:
                cand = [t for t in cand if t not in set(heavy)]
        if not cand and not use:
            return numbers, "heavy tests unavailable"
        heavy = heavy or any(s[3] for s in use)
        # stand-ins run after the real candidates of similar cost; each counts as (est ms, id)
        jobs = [(d[t], self.ids[t]) for t in cand]
        for _real, sids, ms, _h in use:
            jobs += [(ms / len(sids), sid) for sid in sids]
        jobs.sort()
        ids = [j[1] for j in jobs]
        order = [self.number_of[i] for i in ids if i in self.number_of]
        # after a failure, keep checking only if dropping the rest still pays (e.g. a test edit plus a code edit)
        est = RUN_RATIO * sum(d[t] for t in order) + sum(s[2] for s in use)
        everything = set(order) | {s[0] for s in use}
        late_gain = pack_cost(numbers, d) - pack_cost(set(numbers) - everything, d) - WORKER_MS - max(j[0] for j in jobs)
        limits = {self.ids[t]: max(3.0, 8 * d[t] / 1000) for t in order}  # slower = kept, never dropped
        for _real, sids, ms, _h in use:
            for sid in sids:
                limits[sid] = max(5.0, 3 * ms / 1000)
        req = {
            "files": {p: [o, n] for p, (o, n) in files.items()},
            "ids": ids,
            "limits": limits,
            # after a failure, spend at most half of what dropping the rest could still save
            "stop_on_fail": True if late_gain < AFTER_FAIL_MIN_MS else round(late_gain / 2000, 3),
        }
        proc = self.server()
        if proc is None:
            return numbers, "no server"
        import time

        left = DEADLINE - time.monotonic()
        if left < 5:
            return numbers, "out of time"
        try:
            ans = self.ask(proc, req, min(left, (15 if heavy else 30) + 3 * est / 1000))
        except Exception as e:  # noqa: BLE001
            log("check request failed:", type(e).__name__, e)
            ans = None
        if ans is None:
            if heavy:
                self.no_heavy = True  # e.g. duckdb cannot load on this CPU: stop using those tests
            return numbers, "server died or too slow"
        res = ans.get("res")
        if res is None:
            return numbers, ans.get("how", "?")
        dropped = {t for t in order if res.get(self.ids[t]) is True}
        failed = sum(1 for i in ids if res.get(i) is False)
        stood = []
        clean = all(res.get(i) is True for i in ids)  # everything sent ran and passed
        for real, sids, _ms, kind in use:
            # every stand-in of the real test ran on the patched code and passed (a probe: and all the rest too)
            if sids and all(res.get(sid) is True for sid in sids) and (kind != "probe" or clean):
                dropped.add(real)
                stood.append(self.ids[real].split("::")[-1])
        saved = pack_cost(numbers, d) - pack_cost(set(numbers) - dropped, d)
        return set(numbers) - dropped, (
            f"{ans['how']}: ran {len(res)}/{len(ids)}, {failed} failed, dropped {len(dropped)}"
            + (f" (stand-ins passed: {','.join(stood)})" if stood else "")
            + f", saved {saved} ms for {ans.get('cpu_ms')} ms cpu"
        )


import time as _time

STARTED_AT = _time.monotonic()

# the whole run has 3 minutes (4.5 in the final): stop checking well before that
DEADLINE = _time.monotonic() + float(os.environ.get("BR_CHECK_SECONDS", "100"))
GAIN_SHARE = float(os.environ.get("BR_GAIN_SHARE", "1.0"))  # check when run cost <= this share of the best-case gain
SETUP_MS = 700


class Run:
    """What gets printed. Every commit always has a safe answer: all tests until the selector has a better one."""

    def __init__(self):
        import threading

        self.lock = threading.Lock()
        self.done = False
        self.ids: list = []
        self.all: list = []
        self.answers: dict = {}
        self.pads: dict = {}
        self.durations: dict = {}
        self.checker = None
        self.fd = None
        try:  # answers go to a private copy of stdout; anything else printed lands on stderr
            sys.stdout.flush()
            self.fd = os.dup(1)
            os.dup2(2, 1)
        except OSError:
            self.fd = None

    def emit(self, why=""):
        with self.lock:
            if self.done:
                return
            self.done = True
            out = []
            for cid in self.ids:
                numbers = self.answers.get(cid)
                if numbers is None:
                    numbers = self.all
                    log(cid, "fallback: all tests", why)
                try:
                    workers = pack(numbers, self.durations)
                    if PAD and cid in self.pads:
                        workers = pad(workers, self.durations, self.pads[cid])
                except Exception:  # noqa: BLE001
                    workers = [sorted(numbers)]
                out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
            data = "".join(line + "\n" for line in out).encode()
            fd = self.fd if self.fd is not None else 1
            while data:
                data = data[os.write(fd, data) :]


def hard_seconds(n_commits):
    """Print whatever we have by then: the scorer allows ~6 s a commit (3 min for 30, 4.5 for 45)."""
    return float(os.environ.get("BR_HARD_SECONDS", min(150.0, 40 + 2.4 * n_commits)))


def watchdog(run, seconds):
    import threading

    def fire():
        run.emit("(time limit)")
        c = run.checker
        if c is not None:
            c.kill()
        os._exit(0)

    t = threading.Timer(max(1.0, seconds - (_time.monotonic() - STARTED_AT)), fire)
    t.daemon = True
    t.start()


def main():
    global DEADLINE
    run = Run()
    try:
        tests = json.loads((DATA / "tests.json").read_text())["tests"]
        run.all = [t["number"] for t in tests]
        run.durations = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        run.ids = sorted(p.stem for p in (DATA / "commits").glob("*.patch"))
        hard = hard_seconds(len(run.ids))
        DEADLINE = min(DEADLINE, STARTED_AT + hard - 20)
        watchdog(run, hard)
        _main(run, tests)
    except BaseException as e:  # noqa: BLE001  never leave a commit without an answer
        import traceback

        log("selector error, safe answers for the rest:", type(e).__name__, e)
        log("".join(traceback.format_exc())[-2000:])
    finally:
        if run.checker is not None:
            try:
                run.checker.close()
            except Exception:  # noqa: BLE001
                run.checker.kill()
        run.emit("(error)")


def _main(run, tests):
    m = Map(MAP)
    index = {tid: i for i, tid in enumerate(m.ids)}
    number_of = {index[t["id"]]: t["number"] for t in tests if t["id"] in index}
    unmapped = {t["number"] for t in tests if t["id"] not in index}
    durations = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    checker = Checker({t["number"]: t["id"] for t in tests}, durations) if VERIFY else None
    run.checker = checker
    sel = Selector(m)
    rows = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        chosen, notes = sel.select(text)
        numbers = {number_of[i] for i in chosen if i in number_of}
        if chosen and unmapped:
            numbers |= (
                unmapped  # tests the map never saw: run them whenever anything runs
            )
        plan = None
        if numbers and checker is not None:
            try:
                plan = checker.estimate(text, numbers)
            except Exception as e:  # noqa: BLE001
                notes.append(f"check: estimate error {type(e).__name__}")
            if plan is None:
                notes.append("check: no")
        rows.append([patch.stem, numbers, notes, plan])
        run.answers[patch.stem] = set(numbers)  # safe answer now; the check may shrink it later
        if PAD and numbers:
            run.pads[patch.stem] = pad_order(m, text, number_of, numbers)
    todo = [r for r in rows if r[3] is not None]
    # loading sqlglot and heavy test modules is paid once: only if the batch earns it back
    heavy = set().union(*(r[3][3] for r in todo)) if todo else set()
    fixed = SETUP_MS
    if todo and (os.environ.get("BR_FORCE") or sum(r[3][0] - r[3][1] for r in todo) * GAIN_SHARE > fixed):
        import time

        for r in todo:
            t0 = time.process_time() + sum(os.times()[2:4])
            try:
                r[1], why = checker.check(r[3][2], r[1], r[3][4], r[3][6])
                why += f" [{(time.process_time() + sum(os.times()[2:4]) - t0) * 1000:.0f} ms, est {r[3][1]:.0f}, gain {r[3][0]}]"
            except Exception as e:  # noqa: BLE001
                why = f"check error {type(e).__name__}"
            r[2].append("check: " + why)
            run.answers[r[0]] = set(r[1])
    for cid, numbers, notes, _ in rows:
        log(cid, len(numbers), "|", "; ".join(notes))


if __name__ == "__main__":
    main()
