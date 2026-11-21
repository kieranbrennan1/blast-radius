# Blast Radius entry

Picks which sqlglot tests a patch can break, for RWX's Blast Radius track at the Builders Buildathon (Nov 2026).

## How it works

1. **Map, made once from the pinned sqlglot commit** (`tools/`). Every test is run alone in a fresh Python
   process under `sys.monitoring`, so lazy imports and caches can't hide what it depends on. For each test we
   record the lines it executed, which sqlglot classes it used, and which data files it opened. From that,
   `tools/build_map.py` builds `map/map.json.gz`: line → tests, class → tests, data file → tests, and
   identifier → tests that ran a statement mentioning it.
2. **Selector** (`selector.py`, runs in the container with no network):
   - applies the patch to a pristine copy of the file and compares old and new Python syntax trees; edits
     that leave the tree unchanged (comments, blank lines) select nothing;
   - a changed statement inside a function selects the tests that executed it;
   - a changed class-level or module-level statement selects the tests that used that class (or module)
     *and* ran a line that reads the changed name; a changed lambda selects the tests that called it;
   - a changed fixture file selects the tests that opened it; a deleted module or a syntax error selects
     everything that imported it; pytest config changes select everything;
   - anything unexpected falls back to the safe side.
3. **Run-time check** (`fastverify.py`): when it pays, the cheap candidate tests are actually run against
   the patched code and only the ones that fail are kept. A separate process imports sqlglot once and
   forks a child per commit. If every edit is inside function bodies, the child swaps the new bytecode
   into the loaded functions (`func.__code__ = new`); otherwise it re-imports the changed modules and every
   module that imports them (graph from `tools/import_graph.py`, built from the pinned commit). Tests run
   with `unittest`, process pools run in-process, and a failure, error, timeout or crash keeps the test.
   It is only used when the expected saving beats the CPU it costs, and a function that already ran
   while modules were imported is never swapped (its results may be cached).
4. **Packing**: the chosen tests are spread, longest first, over the number of workers (1–8) that costs least.

No table from patches or kinds of edit to failing tests is used: every decision comes from the map of the
pinned commit and the patch's own syntax tree.

## Run it

```sh
make sample                      # Docker: build, run on the 30 sample commits, score
python3 tools/evaluate.py        # same selector run locally, per-commit costs and misses
```

Rebuilding the map (about 40 minutes on 8 cores):

```sh
python3 tools/run_traces.py work/sqlglot work/.venv/bin/python sample/tests.json work/traces
python3 tools/build_map.py work/traces sample/tests.json work/sqlglot map/map.json.gz
```

## Outside code

- [sqlglot](https://github.com/tobymao/sqlglot) (MIT) at commit `eb11aec`, downloaded during the build.
- The RWX starter kit (`score.py`, `Makefile`, `sample/`, `SCORING.md`, `STARTER_README.md`).
- The packages in the organisers' `requirements.lock` (installed in the image so sqlglot's tests can run);
  pytest was used offline to trace the tests.
- Idea of running candidate tests at run time with a fork server and `__code__` swaps: seen in other teams'
  public write-ups for this track; the code here is our own.

Written with help from Claude (Anthropic). Released under the MIT License.
