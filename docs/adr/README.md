# Architecture Decision Records

Every change to this project beyond a typo starts here. ADR-001 defines the
process and what needs an ADR; `template.md` is the format.

| ADR | Title | Status | Date |
| --- | --- | --- | --- |
| [ADR-001](ADR-001-record-architecture-decisions.md) | Record architecture decisions | Implemented | 2026-09-12 |
| [ADR-002](ADR-002-modernise-toolchain-around-uv.md) | Modernise the toolchain around uv | Implemented | 2026-09-12 |
| [ADR-003](ADR-003-dependency-version-policy-and-pandas-3-hold.md) | Dependency version policy and the pandas 3 hold | Implemented | 2026-09-12 |
| [ADR-004](ADR-004-track-account-ledgers-in-git.md) | Track the account ledgers in git | Superseded | 2026-09-12 |
| [ADR-005](ADR-005-kite-session-cache-and-local-redirect-capture.md) | Cache the Kite session and capture the login redirect locally | Implemented | 2026-09-12 |
| [ADR-006](ADR-006-per-run-artifacts.md) | Write per-run artifacts to runs/, outside version control | Implemented | 2026-09-12 |
| [ADR-007](ADR-007-typed-settings-object.md) | Replace module-level os.getenv calls with a typed settings object | Implemented | 2026-09-12 |
| [ADR-008](ADR-008-broker-protocol-and-dependency-injection.md) | A Broker protocol, a Kite adapter, and injected dependencies | Implemented | 2026-09-12 |
| [ADR-009](ADR-009-golden-file-regression-test.md) | A golden-file regression test on a frozen candle cache | Implemented | 2026-09-12 |
| [ADR-010](ADR-010-type-checking-with-ty.md) | Static type checking with ty | Implemented | 2026-09-12 |
| [ADR-011](ADR-011-continuous-integration-on-github-actions.md) | Continuous integration on GitHub Actions | Implemented | 2026-09-12 |
| [ADR-012](ADR-012-secret-scanning-in-pre-commit.md) | Secret scanning in pre-commit and CI | Implemented | 2026-09-12 |
| [ADR-013](ADR-013-automated-dependency-refresh.md) | Automated dependency refresh with Dependabot | Implemented | 2026-09-12 |
| [ADR-014](ADR-014-claude-md-assistant-contract.md) | A CLAUDE.md contract for coding assistants | Implemented | 2026-09-12 |
| [ADR-015](ADR-015-log-level-and-per-run-log-file.md) | LOG_LEVEL, logging configured at entry, and a log file per run | Implemented | 2026-09-12 |
| [ADR-016](ADR-016-rename-the-momentum-lookbacks.md) | Name the momentum lookbacks and weights for what they are | Implemented | 2026-09-12 |
| [ADR-017](ADR-017-adjust-positions-only-after-a-placed-trade.md) | Adjust positions only after a trade was placed | Implemented | 2026-09-12 |
| [ADR-018](ADR-018-end-the-candle-window-on-the-ist-date.md) | End the candle window on the IST date | Implemented | 2026-09-12 |
| [ADR-019](ADR-019-confirm-fills-before-booking-a-trade.md) | Confirm fills against the broker before booking a trade | Implemented | 2026-09-12 |
| [ADR-020](ADR-020-split-the-strategy-module-along-its-seams.md) | Split the strategy module along its seams | Implemented | 2026-09-12 |
| [ADR-021](ADR-021-snapshots-and-pure-rules.md) | One snapshot per symbol, and rules that are pure functions of it | Implemented | 2026-09-12 |
| [ADR-022](ADR-022-trade-intents-one-executor-and-plan-mode.md) | Trade intents, one executor, bookkeeping in one place, and a plan mode | Implemented | 2026-09-12 |
| [ADR-023](ADR-023-a-backtest-harness-over-the-candle-cache.md) | A backtest harness over the candle cache | Implemented | 2026-09-12 |
| [ADR-024](ADR-024-simple-moving-averages-for-regime-and-trend.md) | Simple moving averages for the regime and trend filters | Implemented | 2026-09-12 |
| [ADR-025](ADR-025-the-gap-filter.md) | Exclude names with a daily move above 15 percent in the last 90 days | Implemented | 2026-09-12 |
| [ADR-026](ADR-026-a-minimum-size-for-a-new-position.md) | A minimum size for a new position | Implemented | 2026-09-12 |
| [ADR-027](ADR-027-resize-on-elapsed-time-not-iso-week-parity.md) | Resize on elapsed time since the last resize, not ISO-week parity | Implemented | 2026-09-12 |
| [ADR-028](ADR-028-rank-by-the-regression-slope.md) | Rank by the book's regression slope, on backtest evidence | Rejected | 2026-09-12 |
| [ADR-029](ADR-029-keep-the-account-state-in-runs-outside-git.md) | Keep the account state at the root of `runs/`, outside git | Implemented | 2026-09-13 |
| [ADR-030](ADR-030-two-strategies-clenow-and-adm-for-stocks.md) | Two strategies, Clenow's and an ADM adapted to stocks, and no hybrid | Proposed | 2026-09-13 |
| [ADR-031](ADR-031-a-local-operator-console-over-runs.md) | A local operator console over `runs/` | Implemented | 2026-09-14 |
| [ADR-032](ADR-032-an-mprocs-cockpit-over-the-console-and-the-command.md) | An mprocs cockpit over the console and the command | Implemented | 2026-09-14 |
| [ADR-033](ADR-033-rank-the-whole-universe-and-qualify-for-entry.md) | Rank the whole universe and qualify names for entry | Implemented | 2026-09-14 |

## Writing a new ADR

1. Next number: `ls docs/adr | grep '^ADR-' | sort -V | tail -1`.
2. `cp docs/adr/template.md docs/adr/ADR-NNN-short-kebab-title.md` and fill
   it in with Status **Proposed**. Include the status quo as an alternative.
3. Add a row to the table above.
4. Commit the ADR on its own: `ADR-NNN: <title>`.
5. Wait for the owner to set it **Accepted**. Then implement, naming
   `ADR-NNN` in every commit, and set **Implemented** in the last one.

To change an accepted decision, write a new ADR that supersedes it and mark
the old one **Superseded** with a pointer. Do not edit the old text.
