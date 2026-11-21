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
3. **Packing**: the chosen tests are spread, longest first, over the number of workers (1–8) that costs least.

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
- Python standard library only at run time. pytest and the packages in `requirements.lock` were used
  offline to trace the tests.

Written with help from Claude (Anthropic). Released under the MIT License.
