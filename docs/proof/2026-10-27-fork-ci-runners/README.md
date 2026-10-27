# Public-fork CI runner qualification — 2026-10-27

PR6 remained queued for approximately ten hours because its first CI, Nix and Docker jobs requested Blacksmith runners with no assigned runner. This source change supplies GitHub-hosted runners for public forks while preserving the exact upstream NousResearch runner choices.

The PR-call graph uses native Ubuntu 24.04 x64, Windows 2025 x64, Windows 11 ARM, and macOS 15 ARM runners. The Linux unit suite retains full discovery and file isolation: upstream runs two slices with 32 workers each; forks run sixteen disjoint slices with four workers each. Windows fork workers are bounded at four. Existing JS/Rust/Nix host-core sizing is reused.

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
