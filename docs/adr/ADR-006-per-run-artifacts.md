# ADR-006: Write per-run artifacts to runs/, outside version control

- **Status**: Accepted
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

A run makes hundreds of decisions and records almost none of them. What
survives is the terminal output, the trades ledger, and the next portfolio
snapshot. The ranking is visible only as twenty `Top ...` log lines. Why a
symbol was excluded from the ranking is logged at DEBUG, below the hardwired
INFO level, and the exception that excluded it is swallowed. Why a holding was
sold is not recorded at all: `should_exit` returns a bare boolean over four
conditions. Target sizes are computed and acted on but never written down.
When a run surprises the owner, there is nothing to diff against last week.

Two other ADRs need the same data. ADR-003 requires a comparison of ranking,
exits and sizes between pandas 2.x and 3.x before the upper bound can move,
and there is currently no file to compare. A future golden-file regression
test needs realistic fixtures, and the natural source is a real run.

The owner has set one constraint: artifacts produced by executing the
strategy are personal operational data, not part of the codebase, and must
not be committed. This is a different category from the ledgers. ADR-004
versions the four ledger CSVs because the program reads them back; cash is
reconstructed from them every run. Run artifacts are derived output the
program never reads. The rule that falls out, and that this ADR adopts for
future decisions: **state the code needs in order to run is versioned;
diagnostics the code produces are not.**

Relevant code paths: `rank_universe` (filter chain and `except Exception`),
`prune_portfolio` and `should_exit`, `resize_positions` and `target_shares`,
the buy loop in `main()` step 11, `record_trade`.

## Decision

Every run that passes the weekday guard writes a run directory. Nothing in
it is ever read by the strategy, and the whole tree is git-ignored.

### Location and naming

- Root `RUNS_DIR`, default `runs/` relative to the working directory like
  the other state files.
- One directory per run: `runs/<YYYY-MM-DD>/<HHMMSS>-<mode>/`, times in IST,
  `mode` one of `live`, `paper`, `kill`. Same-day re-runs, which ADR-005
  makes cheap, never overwrite each other, and paper runs are
  distinguishable at a glance.
- `runs/latest` is a symlink to the most recent run directory.
- Runs stopped by the weekday guard write nothing. Runs that fail after the
  guard leave whatever had been written so far, plus a `run.json` marked
  failed; a partial directory is the most useful crash report we can have.

### Contents

All tables are CSV with a header row, the project's existing convention.
Metadata is JSON.

| File | One row per | Columns |
| --- | --- | --- |
| `run.json` | run | started/finished (IST), mode, package version, git commit if available, settings snapshot with credentials removed, regime (index close, EMA-200, bull), universe size, ranked count, cash and equity before and after, positions before and after, `resize_performed`, status (`completed`, `aborted:<reason>`, `failed:<exception type>`) |
| `universe.csv` | instrument considered | symbol, token, status (`ranked` or `excluded`), reason (`history`, `below_ema100`, `volume`, `atr_pct`, `insufficient_data`, `error:<type>`), and the metrics available at that point: last, ema100, avg_vol_20, atr, atr_pct |
| `ranking.csv` | ranked symbol | rank, symbol, pct_rank, score, annual_slope, r2, close, ema100, held |
| `exits.csv` | holding at prune time | symbol, qty, rank, pct_rank, close, ema100, stop_level, reasons (semicolon-separated: `unranked`, `rank_cutoff`, `below_ema100`, `trailing_stop`), decision (`SELL`, `HOLD`), price |
| `sizing.csv` | holding at resize time | symbol, qty, price, atr, risk_qty, cap_qty, target_qty, delta, action (`BUY`, `SELL`, `HOLD`, `SKIP:<reason>`). Header only when the week's resize is skipped |
| `candidates.csv` | ranked symbol visited by the buy loop | rank, symbol, pct_rank, decision (`BUY` or `SKIP:<reason>` with `beyond_cutoff`, `max_positions`, `held`, `sold_this_run`, `size_error`, `below_min_shares`, `no_cash`), qty, est_cost, cash_after |
| `portfolio_before.csv`, `portfolio_after.csv` | position | same `SYMBOL,QUANTITY` format as the portfolio files |
| `trades.csv` | trade this run | the rows appended to the trades ledger during this run, same columns |
| `run.log` | log line | everything the run logged, via a `FileHandler` attached for the run's duration at the existing level and format |

Credentials never appear in any artifact. The settings snapshot drops every
key containing `KEY`, `SECRET` or `TOKEN`.

### Writing rules

- Each file is written as soon as its step completes, not at the end, so a
  crash in step 9 still leaves steps 4 to 8 on disk.
- Artifact writing never changes a trading decision and never aborts a run.
  A write failure is logged at WARNING with the path and the run continues.
  This is the one place in the codebase where swallowing an exception is the
  intended behaviour, and it is confined to the artifacts module.
- Recording reasons requires the filter chain in `rank_universe` and the
  conditions in `should_exit` to report *which* rule fired. They are
  restructured to return a reason (or a list of reasons) with the boolean
  derived from it. Behaviour must be shown unchanged; see the validation step.

### Git

- `runs/` is added to `.gitignore` with a comment stating the rule above.
- `runs/` is never a test input. When a golden-file test is introduced, its
  fixtures are copied from a chosen run into `tests/fixtures/` deliberately,
  under that test's own ADR, and versioned there.

### Configuration

One new environment variable, `RUNS_DIR`, documented in `.env.example`. No
retention policy: a full-universe run is on the order of a few hundred
kilobytes, a year of weekly runs a few tens of megabytes. Revisit if paper
runs make this untrue.

## Consequences

### Positive

- "Why did it sell X" and "why is Y not in the ranking" become a file open,
  not a re-run with DEBUG logging.
- Two runs can be diffed with standard tools. ADR-003's pandas 3 validation
  gets its comparison files for free.
- Crashes leave a partial directory and a log instead of scrollback.
- The hidden `except Exception` in `rank_universe` stops hiding: every
  swallowed error becomes an `error:<type>` row in `universe.csv`.
- A future golden test has a realistic fixture source.

### Negative

- `rank_universe` and `should_exit` change shape to report reasons. The
  strategy's behaviour must not change, and proving that takes a
  before-and-after comparison rather than a unit test.
- Ten files per run and a directory tree that grows forever, on disk only.
- A second logging destination. `run.log` duplicates the terminal output; log
  level and format remain as they are and belong to a separate logging ADR.
- A deliberate swallow-and-continue in the artifacts module, which reviewers
  must recognise as intentional.

### Neutral

- The ledgers (ADR-004) stay versioned. This ADR does not change what the
  program reads; it adds what it writes.
- Paper runs with the ledger overrides from the onboarding guide still write
  into `./runs/`, tagged `-paper`.

## Alternatives Considered

### Option 1: Status quo, terminal output and the trades ledger

**Pros:**

- Nothing to build or store.

**Cons:**

- Rankings, exclusions, exit reasons and target sizes are unrecoverable after
  the terminal closes. DEBUG-level skips are invisible.

### Option 2: Commit the artifacts alongside the weekly ledger commit

**Pros:**

- `git diff` between weeks; backed up with the repository.

**Cons:**

- Rejected by the owner: execution output is personal operational data, not
  codebase. It would also grow the repository by tens of megabytes a year,
  more with paper runs, and bury code history under data commits.

### Option 3: One append-only history file per artifact type with a run id column

**Pros:**

- Cross-run analysis is a single `read_csv`.

**Cons:**

- Diffing two runs means filtering first; a partial write corrupts the whole
  history; files grow without a natural boundary. A history view can be
  derived from per-run directories by a script later; the reverse is harder.

### Option 4: A SQLite run database

**Pros:**

- Queries across runs; the `sqlite3` module is standard library.

**Cons:**

- Not diffable with ordinary tools, which is the primary use case; a second
  storage convention next to CSV.

### Option 5: Structured (JSON) logging instead of tables

**Pros:**

- One mechanism.

**Cons:**

- Logs are streams, not tables. Every line carries a timestamp, so two runs
  never diff cleanly, and reconstructing a ranking from log lines is the
  problem we already have.

### Option 6: Parquet

**Cons:**

- Compact and typed, but not human-diffable and adds pyarrow. Rejected.

## Implementation Plan

1. **Artifacts module**, one commit. `stocks_on_the_move/artifacts.py` with a
   `RunArtifacts` class: create the directory and `latest` symlink, attach
   the `FileHandler`, `write_table(name, rows)`, `write_json(name, data)`,
   `finish(status)`. Unit tests with `tmp_path`: naming, symlink, credential
   redaction, write failure logs and does not raise.
2. **Instrumentation**, one commit. Restructure the `rank_universe` filter
   chain into an evaluation function returning a `RankItem` or an exclusion
   reason; `should_exit` becomes a thin wrapper over an `exit_reasons`
   function; sizing, candidates, portfolio and trade slices recorded at their
   steps. Unit tests for `exit_reasons` and the evaluation function with
   synthetic frames.
3. **Docs and config**, in the same commit as step 2: `.gitignore`,
   `.env.example`, `ONBOARDING.md` sections 5 and 7, README file table.
4. **Validation before Implemented.** On one day, with the same candle cache,
   run paper mode on the last commit before step 2 and on the new build with
   identical settings. The new `ranking.csv` must match the old run's
   `Top ...` log lines, and the `SELL`, `BUY` and `PAPER` lines in the two
   logs must agree symbol for symbol and quantity for quantity. Record the
   result in Notes.

## References

- ADR-003 (needs the comparison files), ADR-004 (what is versioned and why),
  ADR-005 (cheap same-day re-runs make per-run directories necessary)
- `src/stocks_on_the_move/momentum.py`: `rank_universe`, `should_exit`,
  `prune_portfolio`, `resize_positions`, `main`
- `ONBOARDING.md`, section 5 (file lifecycle) and rough edge 6

## Implementation Status

Not started.

## Notes

The versioning rule adopted here, state the code reads is versioned and
output the code produces is not, is consistent with ADR-004 as written. If
the owner later decides the ledgers should also leave version control, that
is a new ADR superseding ADR-004, not an edit to it.
