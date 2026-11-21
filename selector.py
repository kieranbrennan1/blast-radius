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
        if tag == "replace" and i2 - i1 == j2 - j1:
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
        return name[:1].isupper() and not name.isupper() and name not in MIXIN_ENUMS
    return False


# Enums that are also str or int compare equal to plain values: `"duckdb" == Dialects.DUCKDB` is True, `is` is False.
# In the pinned code only Dialects is compared against plain strings; TokenType, NormalizationStrategy,
# UsingColumnOrder and AliasPrecedence values always come from enum members (Dialect.__init__ converts settings).
MIXIN_ENUMS = {"Dialects"}


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


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


# ---------------------------------------------------------------- selection


class Selector:
    def __init__(self, m: Map):
        self.m = m
        self.all = set(range(len(m.ids)))
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
                if not isinstance(node, ast.Module):
                    got |= m.line_tests(path, *node_span(node, "header"))
                return got, "insert in function"
            return set(), "insert at top"
        if c.new is not None and not header_spots(node, c.new, c.part):
            return set(), "equivalent edit"
        if in_func:
            return m.line_tests(path, *node_span(node, c.part)), "function line"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = m.line_tests(path, node.body[0].lineno, node.end_lineno)
            if c.part == "all":
                # replaced, renamed or deleted def: whoever reads the name too
                return body | (m.name_tests(node.name) or set()) | self.scope_tests(
                    path, c.ctx, None
                ), "def changed"
            return body, "def header"
        if isinstance(node, ast.ClassDef):
            inner = self.class_tests(path, c.ctx + (("class", node.name),))
            return inner | self.scope_tests(
                path, c.ctx, {node.name}
            ), "class header" if c.part == "header" else "class changed"
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
        if c.new is None and names:
            # deleted: anything that reads the name may now crash, even at import
            readers = set()
            for n in names:
                readers |= m.name_tests(n) or set()
            return readers | self.scope_tests(path, c.ctx, None), "top statement deleted"
        return self.scope_tests(path, c.ctx, names or None), "top statement"

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


def pack_cost(tests, durations):
    ws = pack(tests, durations)
    return max(sum(durations[t] for t in w) for w in ws) + WORKER_MS * len(ws) if ws else 0


# ---------------------------------------------------------------- run-time check

VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
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
AFTER_FAIL_MIN_MS = float(os.environ.get("BR_AFTER_FAIL_MIN_MS", "1e9"))  # failing tests run slowly: stop at the first  # TestExecutor.setUpClass, paid in every child that runs one


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
        skip = ("tests/test_optimizer.py", "tests/test_executor.py") if os.environ.get("BR_SKIP_HEAVY") else ("\0",)
        return [
            t
            for t in numbers
            if d[t] < limit and self.ids.get(t) and self.ids[t] not in NEVER_VERIFY and not self.ids[t].startswith(skip)
            and not self.ids[t].startswith(NEVER_VERIFY_FILES)
        ]

    def estimate(self, patch_text, numbers):
        """(gain ms, run cost ms, files, test modules, tests to run, reload size) for checking this commit, or None."""
        d = self.durations
        if not self.candidates(numbers, 7000):
            return None
        files = self.files(patch_text)
        if files is None:
            return None
        from fastverify import Verifier

        reload_n = 0
        if Verifier.static_pairs(str(REPO), files) is None:
            reload = self.verifier().reload_set(files)
            if reload is None:
                return None
            reload_n = len(reload[1])
        best = None
        changed = set(files)
        options = []
        for limit in (1000, 2600, 7000):
            base = [t for t in self.candidates(numbers, limit) if self.ids[t].split("::")[0] not in changed]
            options.append(base)
            # leave out tests whose class set-up is slow (they are kept, not run)
            options.append([t for t in base if not self.ids[t].startswith(SLOW_SETUP)])
        for cand in options:
            if not cand:
                continue
            keep = set(numbers) - set(cand)
            gain = pack_cost(numbers, d) - pack_cost(keep, d)
            est = 60 + RUN_RATIO * sum(d[t] for t in cand) + 5 * reload_n
            if any(self.ids[t].startswith(SLOW_SETUP) for t in cand):
                est += SLOW_SETUP_MS
            if not self.heavy_paid and any(self.ids[t].startswith(HEAVY_FILES) for t in cand):
                est += HEAVY_IMPORT_MS
            if est > GAIN_SHARE * gain:
                continue
            if best is None or gain - est > best[0] - best[1]:
                mods = {self.ids[t].split("::")[0][:-3].replace("/", ".") for t in cand}
                best = (gain, est, files, mods, cand, reload_n)
        if best is None or best[0] < 1000:
            return None
        if any(self.ids[t].startswith(HEAVY_FILES) for t in best[4]):
            self.heavy_paid = True
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

    def check(self, files, numbers, cand):
        d = self.durations
        heavy = [t for t in cand if self.ids[t].startswith(HEAVY_FILES)]
        if heavy and self.no_heavy:
            cand = [t for t in cand if t not in set(heavy)]
            if not cand:
                return numbers, "heavy tests unavailable"
        order = sorted(cand, key=lambda t: (d[t], t))
        ids = [self.ids[t] for t in order]
        # after a failure, keep checking only if dropping the rest still pays (e.g. a test edit plus a code edit)
        est = RUN_RATIO * sum(d[t] for t in order)
        late_gain = pack_cost(numbers, d) - pack_cost(set(numbers) - set(order), d) - WORKER_MS - max(d[t] for t in order)
        req = {
            "files": {p: [o, n] for p, (o, n) in files.items()},
            "ids": ids,
            "limits": {self.ids[t]: max(3.0, 8 * d[t] / 1000) for t in order},  # slower = kept, never dropped
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
        failed = sum(1 for t in order if res.get(self.ids[t]) is False)
        saved = pack_cost(numbers, d) - pack_cost(set(numbers) - dropped, d)
        return set(numbers) - dropped, (
            f"{ans['how']}: ran {len(res)}/{len(order)}, {failed} failed, dropped {len(dropped)}, saved {saved} ms for {ans.get('cpu_ms')} ms cpu"
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
    todo = [r for r in rows if r[3] is not None]
    # loading sqlglot and heavy test modules is paid once: only if the batch earns it back
    heavy = set().union(*(r[3][3] for r in todo)) if todo else set()
    fixed = SETUP_MS
    if todo and (os.environ.get("BR_FORCE") or sum(r[3][0] - r[3][1] for r in todo) * GAIN_SHARE > fixed):
        import time

        for r in todo:
            t0 = time.process_time() + sum(os.times()[2:4])
            try:
                r[1], why = checker.check(r[3][2], r[1], r[3][4])
                why += f" [{(time.process_time() + sum(os.times()[2:4]) - t0) * 1000:.0f} ms, est {r[3][1]:.0f}, gain {r[3][0]}]"
            except Exception as e:  # noqa: BLE001
                why = f"check error {type(e).__name__}"
            r[2].append("check: " + why)
            run.answers[r[0]] = set(r[1])
    for cid, numbers, notes, _ in rows:
        log(cid, len(numbers), "|", "; ".join(notes))


if __name__ == "__main__":
    main()
