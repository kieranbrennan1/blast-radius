# Builders Buildathon · RWX Blast Radius — ends Sun 2026-11-22 11:00 ET

## The problem in one breath
Each commit is one patch to sqlglot @ eb11aec. Print, per commit, which of the 1620 tests to run, split over ≤ 8
workers. Pay: busiest worker's recorded seconds + 2 s per worker + 300 s if any failing test is left out, plus the
CPU seconds our container burns. Middle of 3 runs. Lower wins. Only the final counts.

## Rules that shape strategy
- Machine-scored, no pitch. AI allowed. Public repo, Dockerfile at top, + commit hash. 1 submission an hour.
- Build: network, 4 cores, 8 GB, 10 min. Only PyPI / Docker Hub / GitHub releases at a tag / sqlglot @ eb11aec.
- Run: no network, 2 cores, 4 GB, 3 min (~30 commits); final 4.5 min (~45 commits). Read-only disk, 64 MB /tmp,
  user nobody, ≤ 256 processes, stdout ≤ 2 MB. Repo ≤ 20 MB.
- No lookup table from a patch or kind of edit to failing tests. Maps from the pinned commit are fine. Top entries are read.
- **Final**: our chosen commit (best practice score unless we pick another) runs on unseen commits, *including kinds of
  change not in practice*. Top 5 are re-run 5 times, one at a time on an idle machine; that middle replaces the score.
- **Ties**: within 1 s of the best counts as a tie, earliest submission wins. Repo must stay public with every
  submitted commit until results.

## The score math (what each lever is worth)
- One miss = +300 s. Our whole practice score is 197 s, so **one miss in the final loses**. Dropping a test is only
  worth it if its chance of hiding a failure is under (seconds saved ÷ 300): saving 2 s needs < 0.7 % risk.
- Worker fee: proving "nothing fails" on a commit saves at least 2 s; that is most of what the run-time check buys.
- Packing: a commit that must run a big test (tpcds 21.5 s, max_nodes 28.8 s) can carry ~20 s of other tests on a
  second worker for +2 s. Checking small tests there is wasted CPU (`pack_cost` already sees this).
- CPU: every second our program runs is charged 1:1. Running a test costs ~its recorded time in CPU; it only pays
  when it removes a worker or lowers the busiest one by more.

## Where we stand (2026-11-20 22:17)
| Version | Practice board | Simulated | CPU | Misses |
|---|---|---|---|---|
| e13232b (map + syntax-tree diff) | 223.9 s | 223.4 | 0.5 | 0 |
| 312e093 (+ run-time check) — current best, goes to final | **197.0 s** | 176.4 | **20.6** | 0 |
| Board best (~22:00) | 153.7 s | ? | ? | ? |

- Sample (30 commits, answers known): perfect 94.4 s, ours 106.4 s, 0 misses. Only 4d63 (+9.1 s, string-keyed
  tokenizer table) and 5y72 (+2.5 s) are real gaps. **The sample is nearly solved; it no longer tells us where we lose.**
- Practice is hidden: we see only the totals. It is "heavier" than the sample for us (223 vs 115 simulated before the check).
- Two of 30 sample commits fail test_execute_tpcds: ~51 s of the 94 s floor is big tests that really fail.

## What we don't know yet (the blind spots)
1. **Server CPU is 8× what we measure.** Local Docker (Rosetta) shows ~2.6 s CPU because duckdb crashes there, so
   optimizer/executor tests are never checked locally. On the amd64 server they load and get run: 20.6 s. We don't
   know which part of that 20.6 s bought simulated savings and which was waste.
2. Where the 43 s gap to 153.7 sits on practice: over-selection, CPU, or both.
3. What "new kinds of change" the final adds, and whether every rule falls back safely on them.

## CPU measured (2026-11-20 22:30, native arm64 image `br-arm`, duckdb works; Mac load ~100, so lowest of 2–5 runs)
| Set | Version | Simulated | CPU | Total |
|---|---|---|---|---|
| Sample 30 | as submitted (check incl. optimizer/executor) | 106.1 | 5.2 | 111.3 |
| Sample 30 | heavy tests not checked (`BR_SKIP_HEAVY=1`) | 106.4 | 3.0 | **109.4** |
| Sample 30 | no check (`BR_VERIFY=0`) | 115.4 | 1.0 | 116.4 |
| 102 fakes | as submitted | 4801.5 | 73.5 | 4875.1 |
| 102 fakes | heavy not checked | 4829.9 | 12.6 | **4842.5** |
| 102 fakes | no check | 4849.8 | 2.5 | 4852.3 |
- Checking optimizer/executor tests costs ~2× what it saves (fakes: −28 s simulated for +61 s CPU). Turn it off.
- Checks pay only when everything passes (+1.5–2 s each: the worker fee). When a test fails the CPU is wasted
  (fakes: 19 such checks, 8.8 s CPU for 0.5 s saved; sample 535z 2.2 s CPU for 0.26 s).
- The run-cost estimate is 2–10× too low (2qn5 est 0.46 s → 1.0 s real).
- CPU swings 3–6× with Mac load: measure only when the other sessions are idle.
- **Real misses found** (rechecked with no time limit): m03000, m03009, m03012 — removing `@property` (and a changed
  default) breaks test_execute_tpcds / test_max_nodes, but the map links those two tests to only ~900 / 18 lines, none
  of them the changed ones. Their traces are incomplete. ("bigtests" copy is re-tracing tpcds now.)

## Slices
1. [x] Docker (Colima) + env, starter runs (1210 s)
2. [x] Coverage map v2 (follows pool workers), reads map
3. [x] Selector: syntax-tree diff, import-breaking names, class/table rules, safety net
4. [x] Run-time check (`fastverify.py`) → 0 misses on 75 answered fakes + sample
5. [x] Submitted e13232b (223.9), 312e093 (197.0)
6. [ ] Measure checker CPU with duckdb working (native arm64 image, or a heavy-off A/B submission)
7. [ ] Cut wasted CPU / tune when to check, then resubmit
8. [ ] Final-robustness pass on new kinds of change; pick the final commit deliberately

## Dead ends
- Re-running the whole module tree dump per commit cost ~2 s CPU: now only statements near the edit are dumped.
- Tracer as a pytest plugin only: missed pool-worker processes (3 sample misses).
- `x is True` → `x == True` as a no-op: wrong (1 == True).

## Rules from Kieran
- Ask before creating the team, making the repo public or submitting (2026-11-20). Later: "Yes, resubmit freely"
  for versions that test better; pushing still asked each time.
