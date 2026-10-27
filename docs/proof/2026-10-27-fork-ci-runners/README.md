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
