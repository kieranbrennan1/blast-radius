"""Make fake commits like the practice ones, find their real failing tests, and check the selector.

usage:
  python3 tools/mutate.py make <sqlglot dir> <out dir> <count> [seed]   # write <out>/commits/*.patch
  python3 tools/mutate.py truth <sqlglot dir> <venv python> <out dir> <tests.json>  # run the suite per patch
Then score with: python3 tools/evaluate.py <map> <out dir>
"""

import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

FLIPS = [
    (r" == ", " != "),
    (r" != ", " == "),
    (r" < ", " <= "),
    (r" <= ", " < "),
    (r" > ", " >= "),
    (r" >= ", " > "),
    (r" and ", " or "),
    (r" or ", " and "),
    (r" is not ", " is "),
    (r" is ", " is not "),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"\bnot ", ""),
    (r"\[0\]", "[1]"),
    (r"\[1\]", "[0]"),
    (r"\[-1\]", "[0]"),
    (r" \+ 1\b", " + 2"),
    (r" - 1\b", " - 2"),
    (r"\breturn None\b", "return 0"),
    (r"\bcopy=False\b", "copy=True"),
    (r"\bcopy=True\b", "copy=False"),
]
COMMENTS = [
    "# Cheap check first",
    "# Kept for backwards compatibility",
    "# NOTE: see upstream",
    "# Intentionally not cached",
]


def py_files(repo, under):
    out = []
    for root, _, files in os.walk(os.path.join(repo, under)):
        out += [
            os.path.relpath(os.path.join(root, f), repo)
            for f in files
            if f.endswith(".py")
        ]
    return sorted(out)


def git_diff(path, old, new):
    body = difflib.unified_diff(
        old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=3
    )
    text = "".join(body)
    if not text:
        return ""
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + text


def mutate_code(rng, repo, under="sqlglot", class_level=None):
    files = py_files(repo, under)
    for _ in range(200):
        path = rng.choice(files)
        lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
        idx = rng.randrange(len(lines))
        line = lines[idx]
        s = line.strip()
        if not s or s.startswith(
            ("#", '"', "'", "def ", "class ", "@", "import ", "from ")
        ):
            continue
        indent = len(line) - len(s)
        if class_level is True and indent != 4 and indent != 8:
            continue
        rules = [(a, b) for a, b in FLIPS if re.search(a, line)]
        nums = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line))
        if not rules and not nums:
            continue
        if rules and (not nums or rng.random() < 0.7):
            a, b = rng.choice(rules)
            hits = list(re.finditer(a, line))
            h = rng.choice(hits)
            new = line[: h.start()] + b + line[h.end() :]
        else:
            h = rng.choice(nums)
            new = (
                line[: h.start()]
                + str(int(h.group(1)) + rng.choice([1, 2, -1]))
                + line[h.end() :]
            )
        if new == line:
            continue
        old_text = "\n".join(lines)
        lines[idx] = new
        new_text = "\n".join(lines)
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            continue
        return path, old_text, new_text
    return None


def mutate_comment(rng, repo):
    path = rng.choice(py_files(repo, "sqlglot"))
    lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
    for _ in range(50):
        idx = rng.randrange(1, len(lines))
        prev = lines[idx - 1]
        if prev.rstrip().endswith("\\"):
            continue
        indent = re.match(r"\s*", lines[idx]).group(0)
        new_lines = lines[:idx] + [indent + rng.choice(COMMENTS)] + lines[idx:]
        new_text = "\n".join(new_lines)
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            continue
        return path, "\n".join(lines), new_text
    return None


def mutate_fixture(rng, repo):
    fixtures = []
    for root, _, files in os.walk(os.path.join(repo, "tests", "fixtures")):
        fixtures += [
            os.path.relpath(os.path.join(root, f), repo)
            for f in files
            if f.endswith(".sql")
        ]
    path = rng.choice(sorted(fixtures))
    lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
    for _ in range(50):
        idx = rng.randrange(len(lines))
        line = lines[idx]
        if not line.strip() or line.startswith(("#", "--")):
            continue
        words = line.split(" ")
        pos = rng.randrange(len(words))
        words.insert(pos, rng.choice(["42", "x", "0", "NULL", "1"]))
        new = " ".join(words)
        return path, "\n".join(lines), "\n".join(lines[:idx] + [new] + lines[idx + 1 :])
    return None


def mutate_delete(rng, repo):
    path = rng.choice(py_files(repo, "sqlglot"))
    lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
    for _ in range(100):
        idx = rng.randrange(len(lines))
        s = lines[idx].strip()
        if (
            not s
            or s.startswith("#")
            or s.endswith((":", "(", "[", "{", ",", "\\"))
            or s.startswith((")", "]", "}"))
        ):
            continue
        new_text = "\n".join(lines[:idx] + lines[idx + 1 :])
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            continue
        return path, "\n".join(lines), new_text
    return None


def mutate_test(rng, repo):
    return mutate_code(rng, repo, under="tests")


def mutate_string(rng, repo):
    path = rng.choice(py_files(repo, "sqlglot"))
    lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
    for _ in range(100):
        idx = rng.randrange(len(lines))
        m = re.search(r'"([A-Z_]{2,})"', lines[idx])
        if not m or lines[idx].strip().startswith("#"):
            continue
        new = lines[idx][: m.start(1)] + m.group(1)[:-1] + lines[idx][m.end(1) :]
        new_text = "\n".join(lines[:idx] + [new] + lines[idx + 1 :])
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            continue
        return path, "\n".join(lines), new_text
    return None


def mutate_table(rng, repo):
    """Change one `exp.X: value,` entry in a class-level table (rename target or swap the value)."""
    files = [f for f in py_files(repo, "sqlglot") if "/generators/" in f or "/parsers/" in f or "/typing/" in f or f.endswith(("generator.py", "parser.py"))]
    for _ in range(200):
        path = rng.choice(files)
        lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
        cands = [i for i, ln in enumerate(lines) if re.match(r"^ {4,8}exp\.\w+: .+,$", ln)]
        if len(cands) < 2:
            continue
        idx = rng.choice(cands)
        line = lines[idx]
        m = re.search(r'"([A-Za-z_]{3,})"', line)
        if m and rng.random() < 0.6:
            new = line[: m.start(1)] + m.group(1)[:-1] + line[m.end(1):]
        else:
            other = lines[rng.choice(cands)]
            key = line.split(":", 1)[0]
            new = key + ":" + other.split(":", 1)[1]
        if new == line:
            continue
        new_text = "\n".join(lines[:idx] + [new] + lines[idx + 1:])
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            continue
        return path, "\n".join(lines), new_text
    return None


def mutate_equiv(rng, repo):
    """`x is None` -> `x == None` style swaps (should break nothing)."""
    files = py_files(repo, "sqlglot")
    for _ in range(300):
        path = rng.choice(files)
        lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
        cands = [i for i, ln in enumerate(lines) if re.search(r" is (not )?(None|[A-Z][A-Za-z]+\.[A-Z_]{2,})\b", ln) and not ln.strip().startswith("#")]
        if not cands:
            continue
        idx = rng.choice(cands)
        new = re.sub(r" is not (None|[A-Z][A-Za-z]+\.[A-Z_]{2,})\b", r" != \1", lines[idx], count=1)
        if new == lines[idx]:
            new = re.sub(r" is (None|[A-Z][A-Za-z]+\.[A-Z_]{2,})\b", r" == \1", lines[idx], count=1)
        return path, "\n".join(lines), "\n".join(lines[:idx] + [new] + lines[idx + 1:])
    return None


def mutate_default(rng, repo):
    """Change a default argument value on a one-line `def` (a function header edit)."""
    files = py_files(repo, "sqlglot")
    for _ in range(300):
        path = rng.choice(files)
        lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
        cands = [i for i, ln in enumerate(lines) if re.match(r"^\s*def \w+\(.*=\s*(True|False|None|\d+)\b.*\).*:$", ln)]
        if not cands:
            continue
        idx = rng.choice(cands)
        line = lines[idx]
        h = rng.choice(list(re.finditer(r"=\s*(True|False|None|\d+)\b", line)))
        v = h.group(1)
        nv = {"True": "False", "False": "True", "None": "0"}.get(v) or str(int(v) + 1)
        new_text = "\n".join(lines[:idx] + [line[: h.start(1)] + nv + line[h.end(1) :]] + lines[idx + 1 :])
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            continue
        return path, "\n".join(lines), new_text
    return None


def mutate_decorator(rng, repo):
    """Remove one decorator line (e.g. @property, @classmethod, @unsupported_args(...))."""
    files = py_files(repo, "sqlglot")
    for _ in range(300):
        path = rng.choice(files)
        lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
        cands = [i for i, ln in enumerate(lines) if re.match(r"^\s*@[\w.]+(\(.*\))?$", ln) and i + 1 < len(lines) and lines[i + 1].strip().startswith(("def ", "@"))]
        if not cands:
            continue
        idx = rng.choice(cands)
        new_text = "\n".join(lines[:idx] + lines[idx + 1 :])
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            continue
        return path, "\n".join(lines), new_text
    return None


def mutate_newattr(rng, repo):
    """Add a class-level setting (borrowed from elsewhere, maybe flipped) to a class that only inherits it."""
    files = [f for f in py_files(repo, "sqlglot") if any(k in f for k in ("/dialects/", "/generators/", "/parsers/", "/tokenizers/"))]
    donors = []
    for f in py_files(repo, "sqlglot"):
        for ln in open(os.path.join(repo, f), encoding="utf-8").read().split("\n"):
            if re.match(r"^ {4,8}[A-Z][A-Z_]{3,} = (True|False|\d+)$", ln):
                donors.append(ln.strip())
    for _ in range(300):
        path = rng.choice(files)
        lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
        heads = [i for i, ln in enumerate(lines) if re.match(r"^ {0,4}class \w+\(.*\):$", ln)]
        if not heads:
            continue
        idx = rng.choice(heads)
        indent = len(lines[idx]) - len(lines[idx].lstrip()) + 4
        name, val = rng.choice(donors).split(" = ")
        flip = {"True": "False", "False": "True"}.get(val) or str(int(val) + 1)
        new = " " * indent + f"{name} = {rng.choice([val, flip])}"
        new_text = "\n".join(lines[: idx + 1] + [new] + lines[idx + 1 :])
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            continue
        return path, "\n".join(lines), new_text
    return None


def mutate_base(rng, repo):
    """Change a class's base to another base used in the same file."""
    files = py_files(repo, "sqlglot")
    for _ in range(300):
        path = rng.choice(files)
        lines = open(os.path.join(repo, path), encoding="utf-8").read().split("\n")
        heads = [(i, m) for i, ln in enumerate(lines) if (m := re.match(r"^(\s*class \w+\()([\w.]+)(\):)$", ln))]
        if len(heads) < 3:
            continue
        (idx, m), (_, other) = rng.sample(heads, 2)
        if other.group(2) == m.group(2):
            continue
        new_text = "\n".join(lines[:idx] + [m.group(1) + other.group(2) + m.group(3)] + lines[idx + 1 :])
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            continue
        return path, "\n".join(lines), new_text
    return None


KINDS = {
    "code": (mutate_code, 6),
    "class": (lambda r, repo: mutate_code(r, repo, class_level=True), 3),
    "comment": (mutate_comment, 2),
    "fixture": (mutate_fixture, 3),
    "delete": (mutate_delete, 2),
    "test": (mutate_test, 1),
    "string": (mutate_string, 2),
    "table": (mutate_table, 0),
    "equiv": (mutate_equiv, 0),
    "default": (mutate_default, 0),
    "decorator": (mutate_decorator, 0),
    "newattr": (mutate_newattr, 0),
    "base": (mutate_base, 0),
}


def make(repo, out, count, seed):
    rng = random.Random(seed)
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    only = os.environ.get("MUT_KINDS")
    if only:
        kinds = only.split(",")
    else:
        kinds = [k for k, (_, w) in KINDS.items() for _ in range(w)]
    meta = {}
    made = 0
    while made < count:
        kind = rng.choice(kinds)
        parts = []
        n = 1 if rng.random() < 0.75 else rng.randint(2, 3)
        for _ in range(n):
            got = KINDS[kind if not parts else rng.choice(kinds)][0](rng, repo)
            if got:
                parts.append(got)
        seen, diff = set(), ""
        for path, old, new in parts:
            if path in seen:
                continue
            seen.add(path)
            diff += git_diff(path, old, new)
        if not diff:
            continue
        cid = f"m{seed:02d}{made:03d}"
        open(os.path.join(out, "commits", f"{cid}.patch"), "w").write(diff)
        meta[cid] = kind if n == 1 else "multi"
        made += 1
    json.dump(meta, open(os.path.join(out, "kinds.json"), "w"), indent=1)


PLUGIN = """
import json, os
FAILED = set()
def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        FAILED.add("collect:" + report.nodeid)
def pytest_sessionfinish(session):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    json.dump(sorted(FAILED), open(os.environ["TRUTH_OUT"], "w"))
"""


def truth(repo, py, out, tests_json):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import selector

    tests = json.load(open(tests_json))["tests"]
    by_id = {t["id"]: t["number"] for t in tests}
    files_of = {}
    for t in tests:
        files_of.setdefault(t["file"], []).append(t["number"])
    answers_path = os.path.join(out, "answers.json")
    answers = (
        json.load(open(answers_path))["commits"] if os.path.exists(answers_path) else {}
    )
    kinds = json.load(open(os.path.join(out, "kinds.json")))
    work = tempfile.mkdtemp(prefix="mut")
    shutil.copytree(
        repo, os.path.join(work, "repo"), ignore=shutil.ignore_patterns(".git")
    )
    root = os.path.join(work, "repo")
    plug = os.path.join(work, "plug")
    os.makedirs(plug)
    open(os.path.join(plug, "truthplug.py"), "w").write(PLUGIN)
    for name in sorted(os.listdir(os.path.join(out, "commits"))):
        cid = name[:-6]
        if cid in answers:
            continue
        patch = open(os.path.join(out, "commits", name)).read()
        saved = {}
        for f in selector.parse_patch(patch):
            p = f["old"] or f["new"]
            dst = os.path.join(root, p)
            saved[p] = open(dst).read()
            open(dst, "w").write(selector.apply_hunks(saved[p], f["hunks"]))
        res = os.path.join(work, "res.json")
        env = dict(
            os.environ,
            SKIP_INTEGRATION="0",
            PYTHONPATH=f"{root}:{plug}",
            TRUTH_OUT=res,
            PATH=os.path.dirname(py) + ":" + os.environ["PATH"],
            PYTHONDONTWRITEBYTECODE="1",
        )
        subprocess.run(
            [
                py,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "truthplug",
                "-n",
                "8",
                "-p",
                "timeout",
                "--timeout",
                "900",
                "tests",
            ],
            cwd=root,
            env=env,
            capture_output=True,
        )
        failed = set()
        for nid in (
            json.load(open(res)) if os.path.exists(res) else ["collect:everything"]
        ):
            if nid.startswith("collect:"):
                f = nid[len("collect:") :].split("::")[0]
                failed |= (
                    set(files_of.get(f, []))
                    if f in files_of
                    else {t["number"] for t in tests}
                )
            else:
                base = nid.split(" ")[0].split("[")[0]
                if base in by_id:
                    failed.add(by_id[base])
        if os.path.exists(res):
            os.remove(res)
        for p, text in saved.items():
            open(os.path.join(root, p), "w").write(text)
        answers[cid] = {"failing": sorted(failed), "kind": kinds.get(cid, "?")}
        json.dump({"commits": answers}, open(answers_path, "w"), indent=1)
        print(cid, kinds.get(cid), len(failed), flush=True)
    shutil.rmtree(work)


if __name__ == "__main__":
    if sys.argv[1] == "make":
        make(
            sys.argv[2],
            sys.argv[3],
            int(sys.argv[4]),
            int(sys.argv[5]) if len(sys.argv) > 5 else 1,
        )
    else:
        truth(*sys.argv[2:6])
