# Public-fork CI runner qualification — 2026-10-27

The original runner-migration scope below is retained as provenance; later follow-ups supersede only their stated worker/job-budget observations.

PR6 remained queued for approximately ten hours because its first CI, Nix and Docker jobs requested Blacksmith runners with no assigned runner. This source change supplies GitHub-hosted runners for public forks while preserving the exact upstream NousResearch runner choices.

The PR-call graph uses native Ubuntu 24.04 x64, Windows 2025 x64, Windows 11 ARM, and macOS 15 ARM runners. The Linux unit suite retains full discovery and file isolation: upstream runs two slices with 32 workers each; forks run sixteen disjoint slices with four workers each. The initial Windows fork cap was four; the measured resource follow-up below narrows it to two. Existing JS/Rust/Nix host-core sizing is reused.

Pinned Blacksmith checkout already declines its cache path without a Blacksmith agent; its pinned source was inspected. Docker build/publish repository fences, permissions, triggers, conditional lanes, action pins, concurrency and timeouts are unchanged. Manual/release-only E2E and publishing workflows retain their existing runner policy. No service, secret, provider, self-hosted runner or runtime was changed.

Validation: 182 tests passed in 8.3 seconds across the new runner tests and existing classifier, clean-worktree and workflow-graph tests. Two existing PR/push E2E-gate replay tests passed separately in 0.8 seconds. Ruff and diff checks passed. The accompanying semantic comparison proves the YAML changes are restricted to runner selection and the unit-slice/worker settings. Actionlint was unavailable and was not installed.

This is source qualification, not a successful hosted CI claim. Real standard-runner disk headroom, dependency/build duration and GitHub concurrency admission require execution evidence. Standard hosts have less disk than the existing Blacksmith runners; timeouts remain unchanged. PR6 and its existing queued runs were not rerun or amended by this change.

Official runner contracts:
- https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- https://docs.blacksmith.sh/blacksmith-runners/overview
- https://www.blacksmith.sh/pricing
- https://github.com/useblacksmith/checkout/blob/25227e61ff9dafe400e22fa487b673eac4e4409a/src/blacksmith-cache.ts

## First hosted execution and stacked attribution scope

Automatic PR7 runs at head `cb5ca419e3f5e9bfda17f749cf695e78775d529d` admitted real GitHub-hosted runners: CI `40511400545`, Docker `40511400342`, and Nix `40511400347`. Docker and Nix completed successfully. Main CI exposed real test failures and is not qualified green.

Attribution job `122214051244` failed because its existing shell step unconditionally merge-based against `origin/main`, including historical stack commits. PR7's actual base was `e5dd0631b836d990a4b3d4021dd507d53b433d90`; its own range contained exactly two commits, both authored through the existing GitHub ID+noreply policy. No historical identities were invented or added.

The follow-up uses the event's actual PR base SHA, quoted through an environment variable, while retaining the existing main comparison for non-PR callers. Strict shell failure prevents a missing base from becoming an empty successful check. Four real temporary-Git-history regressions execute the complete workflow shell step: accepted historical stack authors are excluded, a genuinely new unmapped author still fails, non-PR callers retain their main range, and a missing base fails closed. All four passed in 3.0 seconds through the canonical runner; Ruff and diff checks passed. This scope correction does not weaken mapping policy, change identities, or claim the other hosted failures are repaired.

## Native identity and browse budget regressions

The Windows task-user lookup now captures bytes and reuses the existing native console decoder, rejecting lossy identities before its existing environment fallback. The Windows unit and subprocess encoding policy files passed independently: 792 passed, six skipped. Coverage includes cp936, cp850 and malformed output. These are mocked Windows subprocess tests on Linux, not live Windows adoption.

The browse schema reconciler now classifies an interrupted `BEGIN IMMEDIATE` within the same rollback-safe budget handling as the additive DDL. A regression interrupts the real SQLite VM during BEGIN, reproduces `OperationalError: interrupted` before the fix, and verifies a retryable budget error with unchanged store bytes afterward. The complete browse drift file passed: 30 tests in 60.50 seconds using maintained Python 3.14.7/SQLite 3.53.1 and copied existing pytest packages in a Git-free private source fixture. Hosted Python 3.14.9 remains a separate execution check. No home guard was disabled and no live user database was used.

The completed first CI run also reported JavaScript and Windows-specific failures. These remain under source/fixture diagnosis; this proof does not declare the full hosted run green.

## Second hosted execution and follow-up corrections

Run `40526503589` at `e8be539454ee084fe99df16021d35f3d76c66d47` completed. The formerly failing Python slices 9, 10, 12 and 15, contributor attribution, both Windows ARM slices, and macOS slice 1 passed. The JS workspace run passed its runtime suites and failed desktop type checking because the new connection fixtures omitted required fields. The correction supplies the full actual `HermesConnection` shape, with no casts or product changes. Using the lockfile's existing TypeScript 6.0.3, extraction of the actual published interface and both actual fixture objects reproduces two TS2345 errors on that head and zero on the corrected objects; this is a targeted data-contract check, not a full local SDK typecheck.

Ruff and code-health also identified two new test-policy findings; explicit `check=False` and a replacement table preserve the assertions. All changed Python files now pass Ruff, and the actual health command reports zero blocking/advisory findings against the real PR base. Windows x64 retained two Git locator failures and one immediate suspended-state observation failure; the source-qualified UCRT64 and bounded-observation follow-ups are documented in `windows-consumer-candidate.json`. Local Git discovery tests passed 15 cases with three native skips. A full new hosted run is still required; no live adoption or full-green claim follows from these local checks.

## JavaScript first-run workload and SDK fixture correction

The hosted JS job ran ten workspace checks with four concurrent checks on the four-core fork host. Each check can start its own worker pool. The staged-Git integration test reached its existing 5-second test ceiling; the exact unchanged test independently passed locally in 1.498 seconds. The Desktop UI check took 1262 seconds while competing with other checks. This supports a fork resource-admission correction, not a new per-test timeout or a claim that disk was exhausted. ENOSPC strings elsewhere in the UI log came from deliberate error-path fixtures and were not the failing assertion.

The existing workspace dispatcher now admits one check at a time on forks, with a bounded 60-minute fork job ceiling; upstream retains its prior dispatcher sizing and 30-minute ceiling. Every dynamically discovered workspace check still runs. No test-level timeout, selection, failure policy or publishing gate changed. Workflow regressions render both policies and verify the identical dispatcher invocation plus the fork's existing concurrency option.

The SDK `warmProfile` test had an old fixture: it expected a dial without an eligible remote connection. Existing accepted production code deliberately returns without starting local, SSH or unknown-route profiles speculatively. The test now supplies a remote fixture for the permitted path and independently asserts a local profile stays idle; production behavior is unchanged. Both failing JS test sources were byte-identical between PR2's base and the first PR7 run. Local canonical SDK collection remains dependency-blocked: the reused dependency tree lacks current `@rolldown/plugin-babel` and `blobatar` packages. No SDK PASS is claimed; current-lockfile hosted execution remains required. No dependency installation or live app action was performed.

Follow-up local validation: 13 tests passed in 11.2 seconds across the fork runner/workspace policy, real-Git attribution range and workflow dependency graph suites. SDK test formatting passed against the exact tracked Prettier configuration; Ruff and diff checks passed. These policy tests do not replace the pending canonical SDK/hosted workspace execution.

## Windows fork worker resource admission — completed head 3b822093f9

Run 40564037603, Windows x64 job 122400521911, completed the available assertions with 736 passed / 0 failed, but two file subprocesses hit their unchanged 300-second ceilings: `test_desktop_update_windows_commit_outcome.py` after 6/12 passing cases and `test_desktop_update_windows_handoff_lineage.py` after 8/10. The gate correctly remained red. Git UCRT64, process identity and suspended-state assertions passed; this evidence supports a workload-budget candidate, not a new assertion fix or proof that every timed-out case is correct.

The four-core fork host ran four file workers (one core per worker); the existing upstream 32-core pool runs 16 (two cores per worker). Fork Windows jobs now use two workers in that same pool, preserving every existing file, selector, shard, retry/failure rule and per-test/per-file timeout. macOS and upstream worker policy are unchanged. No native NIGHTFALL process probe or app/runtime action is part of this change.

Observed total file wall was 4310.6 seconds versus 1079.9 runner seconds at four workers; P99 was 299.44 seconds and maximum 301.80 seconds. Without assuming a speedup, two workers need 35.9 minutes for that measured workload. Only the fork x64 job receives a 45-minute admission ceiling for this workload plus setup/cleanup; upstream x64/mac remain 30 minutes, and both explicit ARM rows remain 60. This changes job admission, not the 300-second file ceiling. A new hosted run must establish whether contention relief resolves these timeouts; no green/native-completion claim follows from policy replay.

Local validation: seven policy tests passed in 3.42 seconds with existing PM Python 3.14.7 and the private copied pytest framework (hosted CI uses 3.14.9). The revised policy test fails against the unchanged baseline worker setting. Full YAML semantic comparison found only the two allowed leaves changed: OS worker environment and OS job admission ceiling. Actual-base code health reports zero blocking/advisory findings; changed Python Ruff and diff checks pass. Home/live-system guards stayed enabled; no packages were installed.

Independent coordinator verification matched the exact workflow/policy-test hashes and reran the same full-source private-fixture policy suite: seven tests passed in 6.12 seconds. This remains source/policy evidence; hosted native acceptance is pending.


### ARM budget follow-up: reviewed source candidate

Published PR7 head671d3d2/run40572669947 passed x64. ARM slice2/2 passed
eleven of twelve commit-outcome tests before the unchanged300-second file
limit stopped the final bounded-help test at301.10s. No assertion failure or
production timeout defect is established; handoff-lineage finished291.64s.

The reviewed candidate moves only that unchanged test into the existing
Python-handoff/runtime-probe module, importing the same real-launcher helper.
Destination was selected in the ARM list and absent from top10 durations
(tenth19.36s); this is an inferred upper bound, not an exact measurement.
Both GitHub artifact routes returned invalid ZIP data. Exact test AST including
decorators is unchanged; production, probe10s, handoff120s, file300s, workers2,
assertions and cleanup guards are preserved.

Resume from owned hermes-windows-worker-budget-20261027 worktree. Root approved normal publication; native hosted acceptance remains required. No rerun, merge, runtime action or PR6 base
advance occurred. Failed log: /tmp/phoenix-pr7-arm-671d-122430823482-20261027.log.

Source-equivalence validation preserved all13 test-function ASTs/decorators across
both modules. Actual pytest import/collection returned23 parametrized cases in
0.15s using the existing Git-free full-source/PM3.14.7 test overlay. Both changed
Python files passed Ruff and the diff check. No native claim follows collection.
