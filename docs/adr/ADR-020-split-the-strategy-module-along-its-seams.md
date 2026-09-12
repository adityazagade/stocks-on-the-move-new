# ADR-020: Split the strategy module along its seams

- **Status**: Proposed
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

`src/stocks_on_the_move/momentum.py` is 1,297 lines and holds five jobs
that change for five different reasons:

- **Strategy rules**: the composite momentum score, the filter chain, the
  exit rules, the trailing stop, ATR sizing, the index regime.
- **Accounting**: the portfolio CSV, the two ledgers, cash reconstruction,
  the trade row.
- **Execution**: the price lookup per order type, order placement, the wait
  for a fill (ADR-019).
- **Data access**: the NSE archives fetch for the index constituents, the
  instrument token cache, the series-code parsing that filters instruments.
- **Orchestration and reporting**: `run()` at 150 lines, `main()`, six
  artifact column lists and the row-building for every table.

The banner comments already name these seams; the file never had a reason
to be split because until ADR-008 everything reached for module state. That
is gone. Since ADR-005 every ADR but two has touched this one file, and the
test suite imports the whole strategy as one name.

Smaller things have accumulated with it. `should_exit` has no caller.
`rank_says_exit`, `_trailing_stop_hit` and `target_shares` exist only so
older tests can call them. `RankItem` is a namedtuple among frozen
dataclasses. The trade and order records are lists of dicts. The NSE fetch
is a pandas read over HTTP with a sleep loop, the one external dependency
that sits behind no protocol: when NSE archives are unreachable on the
trading day the run aborts with an empty universe, and nothing remembers
last week's list.

## Decision

Split `momentum.py` into modules with one job each, as a pure move: no
function body changes beyond its import path, and the golden test (ADR-009)
stays byte-identical through every commit. The flat layout, no subpackage:

| Module | Holds |
| --- | --- |
| `context.py` | `RunContext`, `Portfolio`, `Fill`, `filled_qty`, the token cache (`build_token_cache`, `token_of`), `ist_now` |
| `indicators.py` | `atr`, `annualise`, `_composite_momentum`, the strategy constants (lookbacks, weights, periods) |
| `rules.py` | `evaluate_instrument`, `rank_universe`, `exit_reasons`, `_trailing_stop`, `size_position`, `index_trend`, their records (`Evaluation`, `ExitCheck`, `Sizing`, `RankItem`) |
| `universe.py` | `SERIES_CODES`, `NO_MARKET_SERIES`, `series_of`, `base_symbol`, `get_universe`, the `UniverseSource` protocol and its NSE archives implementation |
| `ledger.py` | `read_portfolio`, `write_portfolio`, `_ensure_csv`, `_append_row`, the cash-flow row, `cash_from_cash_ledger`, `trades_cash_delta`, `init_cash_balance`, `record_trade`, `TRADE_COLUMNS` |
| `execution.py` | `_price_for`, `await_fill`, `_place`, `safe_buy`, `safe_sell`, `ltp_map`, `live_value`, `gross_cost_for_buy`, `net_proceeds_for_sell`, `ORDER_COLUMNS`, the trade log lines |
| `pipeline.py` | `run`, `_finish`, `prune_portfolio`, `resize_positions`, `raise_cash_if_needed`, `liquidate_all`, the step-11 buy loop as its own function |
| `reporting.py` | the remaining column lists and the row builders for `ranking.csv` |
| `momentum.py` | `main`, `run_mode`, `authenticate`; the console script keeps pointing here |

Alongside the move, and in the same commits, the residue goes:

- `should_exit` is deleted. `rank_says_exit`, `_trailing_stop_hit` and
  `target_shares` are deleted and their tests rewritten against
  `exit_reasons` and `size_position`.
- `RankItem` becomes a frozen dataclass with the same fields in the same
  order. No code indexes it positionally.
- No compatibility façade. The tests change their imports in the same
  commit as each move, so nothing keeps re-exporting the old names.

**One behaviour change, in the last commit, on its own.** `universe.py`
gains a `UniverseSource` protocol with one method, `symbols() -> set[str]`,
and an `NseArchives` implementation that does what `nse_universe_symbols`
does today plus a last-good copy: a successful fetch writes the list to
`CACHE_DIR/universe-<index>.txt`; a failed fetch reads that file, logs a
WARNING with the copy's age, and uses it; with no copy either, the run
aborts as it does today. `RunContext.universe` becomes a `UniverseSource`
instead of a bare callable; the tests' injected sets wrap in a
`StaticUniverse`.

Documentation moves with the code: the module map in `ONBOARDING.md`
section 1, the pipeline table in section 3, and the "one module" sentence
that is no longer true. `CLAUDE.md` changes nothing: no rule moves.

## Consequences

### Positive

- A change to a rule, the ledger format, the order path or the report
  touches one small module and its one test file.
- The rules module is the seed for ADR-021: once the rules sit alone, making
  them pure is a local change.
- A dead function and three wrappers stop suggesting they are part of the
  strategy.
- An NSE outage on a Wednesday degrades to a WARNING and last week's list,
  not an aborted run. The candle cache already has this stance.

### Negative

- Every test file changes its imports; `tests/test_pipeline.py` and
  `tests/test_fills.py` most. Mechanical, but it is the bulk of the diff.
- Eight modules to know instead of one. The onboarding guide's module map
  carries that.
- The last-good universe copy can go stale unnoticed if NSE stays down for
  weeks. The WARNING carries the age; a copy older than 30 days is a second
  WARNING, and the run still proceeds.

### Neutral

- The console script stays `stocks_on_the_move.momentum:main`, so nothing
  the operator types changes.
- `runs/` artifacts are identical; `run.json` and every table keep their
  columns.

## Alternatives Considered

### Option 1: Status quo, one module with banner comments

**Pros:**

- Nothing to move; every name is one import away.

**Cons:**

- Every future ADR edits the same 1,300 lines; the rules cannot be made pure
  or reused by a backtest while they sit next to the order path.

### Option 2: A `strategy/` subpackage

**Pros:**

- Groups the rule modules under one name.

**Cons:**

- Eight files do not need a second level. Revisit if the backtest (ADR-023)
  grows a package of its own.

### Option 3: Keep `momentum.py` as a façade re-exporting every name

**Pros:**

- Tests need no import changes.

**Cons:**

- The façade would be the file everyone still imports, and the split would
  exist only on disk. The import churn is the cost of the change being real.

### Option 4: One big-bang pull request

**Cons:**

- A 2,000-line diff nobody can review. Three pull requests, each green,
  each with the golden files untouched.

## Implementation Plan

1. **Leaves**, first pull request: `indicators.py`, `universe.py` (parsing
   and `get_universe` only), `ledger.py`, `context.py`. Tests re-pointed.
2. **Middle**, second pull request: `rules.py`, `execution.py`; the dead
   function and the three wrappers removed; `RankItem` a dataclass.
3. **Top**, third pull request: `pipeline.py`, `reporting.py`, `momentum.py`
   reduced to the entry point; the onboarding guide's module map; then the
   `UniverseSource` commit with its own tests: a fetch that fails falls back
   to the copy with a WARNING naming its age; no copy aborts.
4. **Validation before Implemented.** After each commit,
   `git status tests/fixtures/golden/expected` shows no change, the suite is
   green, `ty` is clean, and CI passes on `main` after each merge.

## References

- `src/stocks_on_the_move/momentum.py`, the banner comments at lines 58,
  212, 340, 384, 403, 429, 1071
- ADR-008 (the protocols that make the split possible), ADR-009 (the guard),
  ADR-021, ADR-022, ADR-023 (what the split is for)
- `ONBOARDING.md` sections 1 and 3

## Implementation Status

Accepted on 2026-09-12 by the owner's instruction to implement. In progress.

- **Step 1, the leaves**: `context.py` (the run context, portfolio, fill,
  token cache, the IST clock), `indicators.py` (the strategy constants,
  `atr`, `annualise`, `_composite_momentum`), `universe.py` (series codes
  and parsing, `get_universe`, the NSE archives fetch) and `ledger.py` (the
  portfolio CSV, both ledgers, cash reconstruction, `record_trade`) carved
  out of `momentum.py` verbatim; the strategy module imports them back for
  what it still holds. Tests import the moved names from their new homes.
  219 tests pass, `ty` is clean, the golden expected files are untouched.

## Notes

The order of the split follows the import graph: leaves first so that each
new module imports only what already moved.
