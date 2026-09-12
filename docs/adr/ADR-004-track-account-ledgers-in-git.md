# ADR-004: Track the account ledgers in git

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

Four CSV files in the working directory are the entire state of the account:
`current_portfolio.csv`, `next_portfolio.csv`, `cash_ledger.csv` and
`trades_ledger.csv`. Cash is never stored; every run reconstructs it as
`STARTING_CASH` plus the sum of the cash ledger plus the sum of the trades
ledger's `cash_delta` column. There is no broker reconciliation. If these
files are lost or corrupted, the account history is gone and the next run
starts from the wrong cash figure.

The repository is hosted on GitHub. The same directory also holds
`.cache_candles/`, roughly 2,800 regenerable market-data CSVs, and `.env`
with the broker credentials.

## Decision

- The four ledger CSVs are tracked in git. `.cache_candles/`, `.env` and
  `.venv/` are ignored.
- After each weekly run, the changed ledgers are committed with the run
  date in the subject, so `git log` on `trades_ledger.csv` is the trade
  history and a bad run can be undone with `git revert`.
- `.gitignore` carries the four ledger paths as commented-out lines. If the
  repository ever becomes public or is shared beyond the owner, flipping
  those lines and rewriting history is the first step; encrypting the files
  with a tool such as git-crypt is the fallback if they must stay in a
  shared repository.
- `.env` is never committed under any circumstances.

## Consequences

### Positive

- Free backup and audit trail; a diff shows exactly what a run did to cash
  and positions.
- Recovery from a mistaken run is a git operation, not forensic work.

### Negative

- Financial data lives in a remote. Acceptable while the repository is the
  owner's private one; it is the reason for the commented-out ignore lines.
- Assumes a single machine runs the strategy. Two clones running the same
  week will produce conflicting ledgers.
- `trades_ledger.csv` grows without bound. At the weekly cadence with 25
  positions that is on the order of a thousand rows a year, which is not a
  practical problem for CSV.

### Neutral

- `next_portfolio.csv` changes on every run and will show in every diff.

## Alternatives Considered

### Option 1: Ignore the ledgers (leave them unversioned, as before git)

**Pros:**

- No account data in the remote.

**Cons:**

- No history; deletion or a corrupt write loses the account state that the
  cash reconstruction depends on.

### Option 2: A separate private data repository or submodule

**Pros:**

- Clean separation of code and data.

**Cons:**

- Two repositories to keep in step for a one-person project; the ledgers
  are meaningless without the code version that wrote them.

### Option 3: Move state to SQLite

**Pros:**

- Transactions, no partial writes.

**Cons:**

- A data-format change, out of scope here and deserving its own ADR.

## References

- `.gitignore`
- `momentum.py`: `init_cash_balance`, `record_trade`, `write_portfolio`
- `ONBOARDING.md`, sections 4 and 5

## Implementation Status

Tracking implemented in commit 6e7f593 (2026-09-12). The commit-after-each-run
habit is a rule of this ADR, not something the code enforces.
