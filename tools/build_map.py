"""Turn the per-test traces into the map the selector reads.

usage: python3 tools/build_map.py <traces dir> <tests.json> <sqlglot dir> <out map.json.gz>

The map (keyed by test id, so it survives renumbering):
  tests    [test id, ...]                      index = position here
  sets     [[test index, ...], ...]            unique test sets, delta-encoded
  lines    {file: {line: set index}}           tests that executed the line (cold start)
  classes  {"module:Qual.Name": set index}     tests that ran a method on an instance of it (or a subclass)
  opens    {file: set index}                   tests that opened a data file
  touched  {file: set index}                   tests that executed any line of the file
  names    {identifier: set index}             tests that executed a statement reading or writing it
"""

import ast
import glob
import gzip
import json
import os
import sys
from collections import defaultdict

traces, tests_json, repo, out = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]

lines = defaultdict(lambda: defaultdict(list))
classes = defaultdict(list)
opens = defaultdict(list)
touched = defaultdict(list)
rlines = defaultdict(lambda: defaultdict(list))
has_rlines = False
missing = []

for i, t in enumerate(tests):
    path = os.path.join(traces, f"{t['number']}.json")
    if not os.path.exists(path):
        missing.append(t["id"])
        continue
    d = json.load(open(path))
    # merge what child processes (pool workers) recorded
    for extra in glob.glob(path + ".*"):
        try:
            e = json.load(open(extra))
        except ValueError:
            continue
        for key in ("lines", "rlines"):
            for f, ls in e.get(key, {}).items():
                d.setdefault(key, {})[f] = sorted(set(d.get(key, {}).get(f, [])) | set(ls))
        d["classes"] = sorted(set(d["classes"]) | set(e.get("classes", [])))
        d["opens"] = sorted(set(d["opens"]) | set(e.get("opens", [])))
    for f, ls in d["lines"].items():
        touched[f].append(i)
        for ln in ls:
            lines[f][ln].append(i)
    if "rlines" in d:
        has_rlines = True
        for f, ls in d["rlines"].items():
            for ln in ls:
                rlines[f][ln].append(i)
    for c in d["classes"]:
        classes[c].append(i)
    for f in d["opens"]:
        opens[f].append(i)

if missing:
    print(
        f"WARNING: {len(missing)} tests have no trace: {missing[:5]}", file=sys.stderr
    )

# identifier -> tests that executed a statement mentioning it
BODIES = ("body", "orelse", "finalbody", "handlers", "cases")


def stmt_spans(tree):
    """(first, last, node) for every statement; compound statements count their header only."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.stmt, ast.ExceptHandler, ast.match_case)) and hasattr(node, "lineno"):
            first, last = node.lineno, node.end_lineno or node.lineno
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.AST):
                last = max(first, body[0].lineno - 1)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
                    first = min(d.lineno for d in node.decorator_list)
            yield first, last, node


def mentioned(node, first, last):
    out = set()
    for sub in ast.walk(node):
        ln = getattr(sub, "lineno", None)
        if ln is not None and not (first <= ln <= last):
            continue
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
        elif isinstance(sub, ast.alias):
            out.add((sub.asname or sub.name).split(".")[0])
            out.add(sub.name.split(".")[-1])
        elif isinstance(sub, ast.keyword) and sub.arg:
            out.add(sub.arg)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(sub.name)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.isidentifier():
            out.add(sub.value)
    return out


known = set()
for root_dir in ("sqlglot", "tests"):
    for dirpath, _, fnames in os.walk(os.path.join(repo, root_dir)):
        for fn in fnames:
            if fn.endswith(".py"):
                try:
                    tree = ast.parse(open(os.path.join(dirpath, fn), encoding="utf-8").read())
                except SyntaxError:
                    continue
                for first, last, node in stmt_spans(tree):
                    known |= mentioned(node, 1, 10**9)
import builtins

known |= set(dir(builtins))

name_tests = defaultdict(set)
for f, lm in lines.items():
    if not f.endswith(".py") or not os.path.exists(os.path.join(repo, f)):
        continue
    tree = ast.parse(open(os.path.join(repo, f), encoding="utf-8").read())
    for first, last, node in stmt_spans(tree):
        ran = set()
        for ln in range(first, last + 1):
            ts = lm.get(ln)
            if ts:
                ran.update(ts)
        if not ran:
            continue
        for name in mentioned(node, first, last):
            name_tests[name] |= ran

set_index: dict[tuple, int] = {}
sets: list[list[int]] = []


def sid(members):
    key = tuple(sorted(set(members)))
    i = set_index.get(key)
    if i is None:
        i = set_index[key] = len(sets)
        prev, enc = 0, []
        for m in key:
            enc.append(m - prev)
            prev = m
        sets.append(enc)
    return i


data = {
    "tests": ids,
    "lines": {
        f: {str(ln): sid(ts) for ln, ts in sorted(m.items())}
        for f, m in sorted(lines.items())
    },
    "classes": {c: sid(ts) for c, ts in sorted(classes.items())},
    "opens": {f: sid(ts) for f, ts in sorted(opens.items())},
    "touched": {f: sid(ts) for f, ts in sorted(touched.items())},
    "names": {n: sid(ts) for n, ts in sorted(name_tests.items())},
}
if has_rlines:
    # Only lines whose run-time set differs from the all-code set (lines a module or class body ran).
    data["rlines"] = {}
    for f, m in lines.items():
        rm = rlines.get(f, {})
        diff = {str(ln): sid(rm.get(ln, [])) for ln, ts in m.items() if len(rm.get(ln, [])) != len(ts)}
        if diff:
            data["rlines"][f] = diff
data["known"] = sorted(known)
data["sets"] = sets
with gzip.open(out, "wt", compresslevel=9) as f:
    json.dump(data, f, separators=(",", ":"))
print(
    f"{len(sets)} unique sets, {sum(len(m) for m in lines.values())} lines, "
    f"{len(classes)} classes, {len(name_tests)} names, {len(opens)} data files, {os.path.getsize(out) / 1e6:.2f} MB"
)
