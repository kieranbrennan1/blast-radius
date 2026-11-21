"""Build step: which sqlglot / test modules each module imports while it is being imported.

usage: python tools/import_graph.py <sqlglot repo> <out.json>

Only imports that run at import time count (module level, class bodies, if/try
blocks), not those inside functions or under `if TYPE_CHECKING:`. A submodule
also counts as importing its parent package. The run-time check uses the
reverse of this graph: when a module changes, every module that (transitively)
imports it is re-imported too, so no one keeps a reference to the old objects.
"""

import ast
import json
import os
import sys


def modname(rel):
    mod = rel[:-3].replace(os.sep, ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def type_checking(test):
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def top_imports(body, out):
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.If) and type_checking(node.test):
            top_imports(node.orelse, out)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(node)
            continue
        for field in ("body", "orelse", "finalbody", "handlers"):
            sub = getattr(node, field, None)
            if isinstance(sub, list):
                top_imports(sub, out)
        if isinstance(node, ast.ExceptHandler):
            top_imports(node.body, out)


def main():
    repo, out = sys.argv[1:3]
    mods = {}
    for top in ("sqlglot", "tests"):
        for dirpath, _, files in os.walk(os.path.join(repo, top)):
            for fn in files:
                if fn.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, fn), repo)
                    mods[modname(rel)] = rel
    graph = {}
    for mod, rel in mods.items():
        is_pkg = rel.endswith("__init__.py")
        pkg = mod if is_pkg else mod.rpartition(".")[0]
        try:
            tree = ast.parse(open(os.path.join(repo, rel), encoding="utf-8").read())
        except SyntaxError:
            continue
        nodes = []
        top_imports(tree.body, nodes)
        deps = set()
        if "." in mod:
            deps.add(mod.rpartition(".")[0])
        for node in nodes:
            if isinstance(node, ast.Import):
                for a in node.names:
                    parts = a.name.split(".")
                    for i in range(1, len(parts) + 1):
                        deps.add(".".join(parts[:i]))
            else:
                base = node.module or ""
                if node.level:
                    anchor = pkg
                    for _ in range(node.level - 1):
                        anchor = anchor.rpartition(".")[0]
                    base = f"{anchor}.{base}" if base else anchor
                parts = base.split(".")
                for i in range(1, len(parts) + 1):
                    deps.add(".".join(parts[:i]))
                for a in node.names:
                    deps.add(f"{base}.{a.name}")
        graph[mod] = sorted(d for d in deps if d in mods and d != mod)
    json.dump({"files": mods, "imports": graph}, open(out, "w"))
    print(len(graph), "modules", file=sys.stderr)


if __name__ == "__main__":
    main()
