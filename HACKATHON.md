# Builders Buildathon · RWX Blast Radius — ends Sun 2026-11-22 11:00 ET

## The problem in one breath
Each commit is one patch to sqlglot @ eb11aec. Print, per commit, which of the 1620 tests to run, split over ≤ 8
workers. Pay: busiest worker's recorded seconds + 2 s per worker + 300 s if any failing test is left out, plus the
CPU seconds our container burns. Middle of 3 runs. Lower wins. Only the final counts.

## Rules that shape strategy
- Machine-scored, no pitch. AI allowed. Public repo, Dockerfile at top, + commit hash. 1 submission an hour.
- Build: network, 4 cores, 8 GB, 10 min. Only PyPI / Docker Hub / GitHub releases at a tag / sqlglot @ eb11aec.
- Run: no network, 2 cores, 4 GB, 3 min (~40 practice commits; the sample has 30); final 4.5 min (~60 commits). Read-only disk, 64 MB /tmp,
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
- ~~Real misses on m03000, m03009, m03012~~ **False alarm.** Run directly, test_execute_tpcds and test_max_nodes pass
  on m03000, m03009, m03012 and m01061. The recheck tool emptied the test file before running it (pytest exit 4, "no
  collectors"; fixed in 7a53ac2), so its FAILs were fake. Labels corrected in work/mut1, mut3, eval_new. The tpcds /
  max_nodes labels in work/mut3 keep changing while the Mac is overloaded: trust only a direct rerun.
- ad92489: optimizer/executor tests are kept without checking by default. Sample 109.1–110.6 total (was 111.3);
  fakes 943.0 (was 975.0, no check 952.3).

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

## What the problem really is (research 2026-11-20, brain: insights/blast-radius-problem-deep-dive)
Read before any new build step. Full notes: ~/brain/raw/sources/2026-11-20-blast-radius-problem-notes/.
- The commits are mostly one-line fake bugs (mutants) from a generator (sample: 3 comment-only, 3 multi-file): the job is "which tests kill this mutant".
  The final is ~60 unseen commits (1.5× the 40 practice) with mutation kinds not seen in practice.
  Likely new kinds (from the top team's own harness): deleted/reordered statements, changed defaults, removed dict
  entries, renamed module functions, syntax errors, decorator removal, package __init__ edits, edits to test files
  and tests/helpers.py, new method overrides. Every one must fall to the safe side.
- The miss rule = a strict per-commit safe rate (stricter than Meta's "change recall"): one missed failing test = 300 s (≈8 full runs of 36.8 s).
  Extra passing tests only cost seconds, and a test that fits in a non-busiest worker's spare time costs 0:
  use that slack for uncertain tests (free insurance). Exclude a test only when 300·p(fail) < what it adds
  to the busiest worker. Verify at run time only when CPU + p·Δ < min(Δ, 300·p).
- Workers: run LPT for every m = 1..8 and keep the cheapest (a worker pays only when S > 2m(m+1)).
- RWX's own stance (Dan Manges): caching "never produces a false positive"; skipping a test ≈ deleting it.
  So: no risk-taking drops (JstD-style), explainable rules, README showing every map comes from eb11aec.
- The gap to first (197.0 vs 153.7) is mostly test time on the hidden commits, not CPU: our simulated 176.4 s alone
  is worse than the leader's whole 153.7 s. Rough split (if their CPU ≈ 9 s): ~31 s tests, ~12 s CPU (20.6 s ours).
  On the 30 sample commits we beat the leader's own claim (~106 s vs ~132 s of tests), so we lose on the hidden commits.
  Likely causes (guesses, not measured): the big tests (28.8 max_nodes / 21.5 tpcds / 6.7 s) kept when nothing breaks,
  and unfamiliar edits falling back to all tests. Biggest known lever: replay the TPC-H/TPC-DS
  executor tests from build-time recordings (likely #1 team's brreplay.py, ~10 other entries do partial
  replay); any query not recorded → run it / send the test to CI.
- Production systems all keep a "run everything" list for global files plus a full-run safety net; ours is the
  all-tests fallback on any error. Keep it.
- Our tracer uses sys.monitoring with DISABLE but traces each test in its own process, so the coverage
  "shared helper dropped" bug (testmon #277) can't hit us. Never trace several tests in one process.

## Lessons from winners of contests like this one (research 2026-11-20 ~11 PM, brain: insights/hackathon-winners-study)
Kaggle, AtCoder Heuristic, 1BRC, the Netflix Prize, ARC Prize and the test-selection tool contests all reward the
robust entry on the hidden set, not the one tuned to the practice board. What that means here:
- **The final is the 1BRC re-run.** 1BRC's organiser re-ran the top 40 on wider data (10K keys, not 413); entries
  tuned to the 413 fell out of the top 10 (the official winner among them), and the only entry top 3 everywhere won. Our "kinds of change not in
  practice" is that wider data: judge every change on the 102 fakes (all kinds), not on the sample or the board.
- **Ties within 1 s go to the earliest submission** (the Netflix Prize was decided by a submission 20 minutes earlier).
  The best practice score is entered in the final automatically, so: submit the robust version as early as it is
  validated, and only resubmit later if it is better by more than the noise. Holding a validated version costs
  tie-break position.
- **We are inside the last 36 hours: converge.** No new big rules or long truth runs after ~Sat 2026-11-21 evening;
  small robustness fixes only; then the exact final image end to end under the real limits (amd64, 2 cores, 4 GB,
  read-only, no network, ~60 commits, 4.5 min) before Sun 11:00 ET. AtCoder's system test runs a few percent slower
  than the practice runs: keep the watchdog margin.
- **Top 5 are re-run 5 times on an idle machine:** CPU that swings with load is a risk; prefer stable CPU over a
  lower average.
- **Other teams' tricks (checked in their code 2026-11-20):** (1) Operator-swap rules. NecritsSoup: `is`/`==` and
  `is not`/`!=` swaps count as no change when the recorded operand *types* use plain identity equality (its own
  claim: 178.7 → 128.5 s on the 30 sample commits, never run in Docker or on the board). That is close to our
  None/enum rule, applied per observed site. Warrior-hound also covers `<`/`<=`, `in`, `and`/`or`, skipping a file
  only when no recorded test evaluation would flip; its value recorder is not in the repo and it publishes no score
  (a researcher's "179.5 → 126.7 s" for it was false). (2) TPC compile-stage fingerprint (Warrior-hound
  `e1_cutoff.py`): when the changed code only runs while TPC queries are parsed/optimized/planned, re-run just that
  stage and compare to the pinned output; identical → drop the executor test, else keep it.
- **Measured for us (static selection, BR_VERIFY=0, 2026-11-20 23:15, HEAD ~7069665):** 102 fakes → 719.8 s, 33 commits
  break nothing and still cost 157.1 s. The biggest: decorator / def-header edits selecting most of the suite
  (m03010 36.8 s, m03020 29.5 s, m01060 27.5 s), then multi-file def-header edits (m03019 12.7, m01056 10.7).
  Operator swaps are not where our waste is (sample already ~106 s vs NecritsSoup's 128.5 s), so trick (1) is likely
  worth little to us (guess). Trick (2) targets the 21.5 s tpcds test; worth a measured try by the session that owns
  selector.py, if Kieran says go. The 2 static "misses" (m01072, m03002) are the known test_sqlglotc truth quirk.
