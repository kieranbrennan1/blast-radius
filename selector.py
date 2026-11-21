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
import gzip
import json
import os
import pickle
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
REPO = Path(os.environ.get("BR_REPO", "/app/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", "/app/map.pkl"))
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
    return ("same", a, b) if clean else dump(node)


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
    if od == nd:
        return
    sm = difflib.SequenceMatcher(None, od, nd, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
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
        out.append(Change(o, ctx, "header"))
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


def diff_spots(o, n):
    """Smallest old nodes that differ; a lambda counts as one unit."""
    if dump(o) == dump(n):
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


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


# ---------------------------------------------------------------- selection


class Selector:
    def __init__(self, m: Map):
        self.m = m
        self.all = set(range(len(m.ids)))

    def select(self, patch_text: str):
        tests: set = set()
        notes = []
        for f in parse_patch(patch_text):
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
            return self.gone(old_path) | got, "rename"
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
            return set(), "unknown file"
        if new_path is None:
            return self.gone(old_path), "deleted"
        if f.get("binary"):
            return self.gone(old_path), "binary"
        if old_path.endswith(".py"):
            return self.python(old_path, f["hunks"])
        if old_path.startswith(("tests/", "sqlglot/")):
            return self.m.open_tests(old_path) or self.data_fallback(
                old_path
            ), "data file"
        return set(), "outside sqlglot and tests"

    def shadow(self, path):
        stem = path[:-3]
        if stem.endswith("/__init__"):
            stem = stem[: -len("/__init__")]
        return self.m.file_tests(stem + ".py")

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
        in_func = any(kind == "func" for kind, _ in c.ctx)
        node = c.node
        if c.part == "new":
            # a statement only in the new file: at class/module level it can define or override names
            if in_func:
                return set(), "new line"
            if isinstance(node, (ast.Import, ast.ImportFrom)) or (m.known and identifiers(node) - m.known):
                return m.file_tests(path), "new top statement may break import"
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
        if c.new is not None and m.rlines is not None:
            spots = diff_spots(node, c.new)
            if spots and all(isinstance(sp, ast.Lambda) for sp in spots):
                got = set()
                for sp in spots:
                    got |= m.line_tests(path, sp.lineno, sp.end_lineno, runtime=True)
                return got, "lambda"
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
                got = m.name_tests(n)
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


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    m = Map(MAP)
    index = {tid: i for i, tid in enumerate(m.ids)}
    number_of = {index[t["id"]]: t["number"] for t in tests if t["id"] in index}
    unmapped = {t["number"] for t in tests if t["id"] not in index}
    durations = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    sel = Selector(m)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        chosen, notes = sel.select(patch.read_text(encoding="utf-8", errors="replace"))
        numbers = {number_of[i] for i in chosen if i in number_of}
        if chosen and unmapped:
            numbers |= (
                unmapped  # tests the map never saw: run them whenever anything runs
            )
        print(
            json.dumps(
                {"commit": patch.stem, "workers": pack(numbers, durations)},
                separators=(",", ":"),
            )
        )
        log(patch.stem, len(numbers), "|", "; ".join(notes))


if __name__ == "__main__":
    main()
