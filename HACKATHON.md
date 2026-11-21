# Builders Buildathon · RWX Blast Radius — ends 2026-11-22 11:00 ET

## Rules
- Machine-scored, no pitch. AI allowed. Solo or 2 people. MIT license. Name outside code in README.
- Hand in: public GitHub repo, Dockerfile at top, + commit hash. 1 submission an hour.
- Build: network, 4 cores, 8 GB, 10 min. Only PyPI / Docker Hub / GitHub releases at a tag / sqlglot @ eb11aec.
- Run: no network, 2 cores, 4 GB, 3 min for ~30 commits (4.5 min, ~45 commits in the final), read-only disk,
  64 MB /tmp, user nobody. Repo ≤ 20 MB.
- No lookup table from patch (or kind of edit) to failing tests. Maps from the pinned commit are fine.

## Judged on
Simulated CI seconds, lower wins: busiest worker's test seconds + 2 s per worker + 300 s per missed commit
+ our CPU seconds. Middle of 3 runs. Final: unseen commits incl. new kinds of change. Within 1 s of best = tie,
earliest submission wins.

## The plan
1. Coverage map from the pinned commit: which tests run which lines (function-level + import-time), which
   fixture files (and lines) each test reads.
2. Selector: patch → changed lines → candidate tests. Nothing touched → run nothing.
3. Verify at run time: copy sqlglot to /tmp, apply the patch, run the cheap candidates in-process, keep only
   the ones that really fail. Big tests (max_nodes 28.8 s, tpcds 21.5 s) are included, not run, when candidates.
4. Split into the cheapest number of workers.

## Numbers
- Sample: 30 commits, 1620 tests, 103 s for the whole suite, 12 commits break nothing.
- Perfect answers on the sample would score ~107 s + CPU. Board best 197.2 s, starter 1614.1 s.

## Slices
1. [x] Docker (Colima) + Python 3.13 env, `make sample` runs the starter → 1210 s simulated locally
2. [~] Coverage map: v1 done (missed pool-worker processes); v2 re-trace follows child processes + run-time-only lines
3. [~] Selector v1: syntax-tree diff, lambda-aware, import-breaking names → check: 0 misses on sample + mutants
4. [ ] Run-time verify → check: 0 misses, lower score
5. [ ] Push public repo + submit (needs Kieran: team + yes to publish)

## Dead ends
- Re-running the whole module tree dump per commit cost ~2 s CPU: now only statements near the edit are dumped.
- Run-time verify (fork server): right answers, but ~1 s overhead per commit vs ~2 s saved; parked until measured idle.

## Rules from Kieran
- Ask before creating the team, making the repo public or submitting (2026-11-20).
