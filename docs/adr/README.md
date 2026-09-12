# Architecture Decision Records

Every change to this project beyond a typo starts here. ADR-001 defines the
process and what needs an ADR; `template.md` is the format.

| ADR | Title | Status | Date |
| --- | --- | --- | --- |
| [ADR-001](ADR-001-record-architecture-decisions.md) | Record architecture decisions | Accepted | 2026-09-12 |
| [ADR-002](ADR-002-modernise-toolchain-around-uv.md) | Modernise the toolchain around uv | Implemented | 2026-09-12 |
| [ADR-003](ADR-003-dependency-version-policy-and-pandas-3-hold.md) | Dependency version policy and the pandas 3 hold | Implemented | 2026-09-12 |
| [ADR-004](ADR-004-track-account-ledgers-in-git.md) | Track the account ledgers in git | Implemented | 2026-09-12 |
| [ADR-005](ADR-005-kite-session-cache-and-local-redirect-capture.md) | Cache the Kite session and capture the login redirect locally | Accepted | 2026-09-12 |
| [ADR-006](ADR-006-per-run-artifacts.md) | Write per-run artifacts to runs/, outside version control | Accepted | 2026-09-12 |
| [ADR-007](ADR-007-typed-settings-object.md) | Replace module-level os.getenv calls with a typed settings object | Accepted | 2026-09-12 |
| [ADR-008](ADR-008-broker-protocol-and-dependency-injection.md) | A Broker protocol, a Kite adapter, and injected dependencies | Accepted | 2026-09-12 |
| [ADR-009](ADR-009-golden-file-regression-test.md) | A golden-file regression test on a frozen candle cache | Accepted | 2026-09-12 |
| [ADR-010](ADR-010-type-checking-with-ty.md) | Static type checking with ty | Implemented | 2026-09-12 |
| [ADR-011](ADR-011-continuous-integration-on-github-actions.md) | Continuous integration on GitHub Actions | Implemented | 2026-09-12 |
| [ADR-012](ADR-012-secret-scanning-in-pre-commit.md) | Secret scanning in pre-commit and CI | Implemented | 2026-09-12 |
| [ADR-013](ADR-013-automated-dependency-refresh.md) | Automated dependency refresh with Dependabot | Accepted | 2026-09-12 |

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
