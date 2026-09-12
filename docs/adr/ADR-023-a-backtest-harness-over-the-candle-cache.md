# ADR-023: A backtest harness over the candle cache

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

There is no way to tell whether a change to a rule is an improvement. The
golden test (ADR-009) says whether the ranking, exits and sizes changed; it
says nothing about whether they changed for the better. The lookbacks were
shortened from the book's 21/63/126 to 5/15/45 before version control "for
a reason nobody recorded" (ADR-016); the ranking score is a blend of
trailing returns the book does not use; the moving averages have a
warm-up bias (ADR-024); the book's gap filter is missing (ADR-025). Each of
those is a strategy ADR waiting for evidence, and the process (ADR-001)
asks for evidence before Accepted.

The pieces exist. The candle cache holds daily candles per instrument and
can hold years: Kite's historical endpoint returns up to 2,000 days of
daily candles per request. ADR-021 makes every rule a pure function of a
snapshot and a parameter set; ADR-022 makes every step a function to a
list of intents. What is missing is a loop that walks a date range,
builds snapshots as of each run date, calls the same decide functions,
fills the intents against the candles and keeps score.

## Decision

A `backtest.py` module and a command, `python -m stocks_on_the_move.backtest`,
that replays the weekly pipeline over the candle cache using the live
rules, and writes its results under `runs/backtests/`.

**Data.**

- The same `CandleStore`, warmed once with `--history-days` (default five
  years) into the same per-token files; the live run slices by date and is
  unaffected by a longer file. Fetches over 2,000 days are chunked.
- The universe is the current constituents list, from the `UniverseSource`
  (ADR-020), fixed for the whole range. This is survivorship bias and the
  summary says so on its first line.
- The regime index is the configured one, from the same cache.

**Engine.**

- Run dates: every configured trading weekday from `--from` to `--to`,
  shifted to the next trading day with a candle when the weekday has none.
- As-of discipline: the snapshot for run date D is built from candles
  strictly before D. A test asserts no snapshot ever holds a candle dated
  D or later.
- Decisions: the live `regime`, `evaluate`, `rank`, `exit_check`, `size`
  and the step functions of ADR-022, with a `StrategyParams` built from
  the settings and any `--set NAME=VALUE` overrides.
- Fills: a `SimExecutor` fills every intent in full at run date D's close,
  with `FEES_PCT` and `SLIPPAGE_PCT` applied as the live paper booking
  does. Limit-order behaviour on `BE` names is not simulated; every intent
  fills.
- State: an in-memory portfolio and ledger; the rebalance cadence follows
  ADR-027 once Implemented, ISO-week parity until then.

**Outputs**, in `runs/backtests/<from>_<to>-<label>/`, git-ignored with the
rest of `runs/`:

- `params.json`: the settings snapshot (credentials removed, as
  `run.json` does) and the parameter set actually used.
- `equity.csv`: one row per run date with cash, market value, equity,
  position count and exposure.
- `trades.csv`: every simulated fill, same columns as the live ledger plus
  the reason.
- `weekly.csv`: per run date, the regime, the ranked count, exits, buys.
- `summary.json`: CAGR, annualised volatility of weekly returns, return
  over volatility, maximum drawdown and its dates, average and maximum
  position count, average exposure, trades per year, turnover per year.

`--compare LABEL...` prints the summaries of several runs side by side.

**What it is for.** Relative comparison of rule variants over the same
history. Not a claim about live returns: the survivorship bias, the fills
at close, the fixed slippage and the absence of delistings all flatter
every variant alike. Every strategy ADR that cites it cites two or more
variants from the same range.

## Consequences

### Positive

- A rule change has evidence before it has an ADR status. ADR-024, 025,
  026 and 028 each name the comparison they need.
- The rules under test are the functions the live run calls. There is no
  second implementation to drift.
- Five years of daily candles for the universe is about 600,000 rows in
  the cache, one-off, at three requests a second: around three minutes of
  fetching.

### Negative

- Survivorship bias is not fixable without historical constituents, which
  NSE does not publish in a usable form. Every result is stated as
  relative.
- Fills at the close with fixed slippage overstate what a live run gets on
  thin `BE` names. The live `orders.csv` (ADR-019) is the place to learn
  the real number, and the slippage setting can be raised for a pessimistic
  run.
- A few hundred lines of new code with their own tests, and a new
  directory under `runs/`.

### Neutral

- The live run's behaviour does not change. The candle files grow once.
- The harness needs a Kite login for the warm-up fetch only; replaying is
  offline.

## Alternatives Considered

### Option 1: Status quo, judge rule changes by inspection

**Cons:**

- The lookbacks were changed this way once and nobody can say why. The
  next change would be the same.

### Option 2: An off-the-shelf framework (backtrader, vectorbt, zipline)

**Pros:**

- Mature engines, plotting, many metrics.

**Cons:**

- The rules would be re-implemented in the framework's idiom, and the live
  code would drift from the tested copy. The point of ADR-021 and ADR-022
  is that the same functions run in both places. A dependency the size of
  the project, for a weekly strategy on daily candles, is the wrong trade.

### Option 3: Paper trade the variants forward

**Cons:**

- One data point a week. Years to learn anything; the strategy would
  change under it meanwhile.

### Option 4: A daily event-driven simulation

**Cons:**

- The strategy acts once a week on daily candles. A weekly loop over daily
  data is exact for it; a daily engine would add cost and no fidelity.

## Implementation Plan

1. **Prerequisites**: ADR-021 and ADR-022 at Implemented.
2. **Warm-up and as-of**, one commit: `--history-days`, the chunked fetch,
   the as-of snapshot builder, the test that no future candle leaks.
3. **Engine and outputs**, one commit: the weekly loop, `SimExecutor`, the
   in-memory portfolio and ledger, the five output files, the summary
   metrics with unit tests on a hand-built equity series.
4. **Command**, one commit: `--from`, `--to`, `--label`, `--set`,
   `--compare`; a deterministic end-to-end test over the golden fixtures'
   37 instruments and a few weeks, with an expected `summary.json`.
5. **Docs**: `ONBOARDING.md` gains a section on running a comparison and on
   what the numbers do not mean; `README.md` one paragraph.
6. **Validation before Implemented.** Two runs with identical parameters
   produce identical outputs; the baseline run over five years completes
   and its `summary.json` has every field.

## References

- ADR-009 (what the golden test can and cannot say), ADR-016 (the
  unrecorded change), ADR-021, ADR-022 (prerequisites), ADR-024, ADR-025,
  ADR-026, ADR-028 (consumers)
- Kite Connect historical data limits: https://kite.trade/docs/connect/v3/historical/

## Implementation Status

Implemented on 2026-09-13, one pull request. Plan steps 2 to 5 are done;
step 6's five-year baseline run needs a Kite login and is the owner's.

- **The replay is plan mode over history.** For each run date the harness
  builds a `RunContext` whose settings are the live ones redirected: plan
  mode, no execution, every state file under the output directory. The
  pipeline's `run` executes unchanged; `PlanExecutor` fills every sendable
  intent at the price `ReplayBroker` quotes, the run date's close; the
  ledger writes nothing, so the harness appends each date's trade rows to
  its own running ledger and writes the positions to its own portfolio file,
  which the next date's `run` reconstructs cash and positions from. That is
  the ADR's `SimExecutor` and in-memory ledger, by reuse rather than by a
  second implementation.
- **As-of discipline.** `ReplayCandles` implements the new `CandleSource`
  protocol over frames loaded once from the cache; `get` slices the same
  calendar window `CandleStore` does (`candles.window`) ending the day
  before the run date, so no rule sees the run date's candle. A test asserts
  it for three dates.
- **Warm-up** is a subcommand, `warm --history-days`, not an option on
  `run`: it needs a login and runs once. It fetches in chunks of 1,900 days,
  merges into the existing cache files as `CandleStore` would, and saves the
  instrument list beside them; `run` and `compare` are offline. The saved
  universe copy (ADR-020) is the fixed constituents list.
- **Outputs** as decided: `equity.csv`, `weekly.csv`, `trades.csv` with the
  intent's reason, `params.json` with the parameter set and the redacted
  settings, `summary.json` with the note on its first key and the metrics
  (CAGR, annualised volatility, return over volatility, maximum drawdown and
  its dates, positions, exposure, trades and turnover per year).
  `--set NAME=VALUE` is typed by the `StrategyParams` field it names.
- **Tests** (`tests/test_backtest.py`, eleven) replay the golden fixtures'
  37 instruments over eight Wednesdays: no future candle leaks, the replay
  broker prices at the close and sends nothing, run dates move to the next
  trading day and skip a closed week, overrides are typed, the replay trades
  and carries state inside its own directory, two replays are byte-identical,
  the metrics on a hand-built series, a chunked warm-up that merges without
  duplicates, the comparison table, and the refusal without a warm cache.
  260 tests pass; `ty` clean; golden untouched.
- **Step 6, the owner's**: `warm` once, then a baseline over the full range;
  the Notes' first comparison follows. The owner warmed the cache on
  2026-09-13: 500 universe equities, 414 with the full five years.


## Notes

Learned from the owner's first real run on 2026-09-13, and folded into the
harness the same day:

- A `TRADING_WEEKDAY` left at Saturday from a paper-run test made the replay
  run only on NSE's special weekend sessions, six dates in twenty-seven
  months, with numbers that looked plausible. The command now warns on a
  weekend weekday and again when the run dates are fewer than half the weeks
  in the range.
- A five-year cache cannot judge the regime on its first months: the index
  needs 200 candles before a run date. The command now starts at the first
  date it can and says so, instead of failing on the first date.
- `compare` sorted results by directory name, so an older run with a later
  range name shadowed a newer one. It sorts by modification time.
- A name whose candles end inside the range, a delisting or a rename the
  cache does not follow, stays held with no price: every exit is skipped
  with a WARNING and the position counts for nothing in equity, a total
  loss. That is conservative and the same for every variant; a run's log
  shows it as the same name warning week after week.
- The first comparison worth running once the harness exists: the current
  blend against the book's lookbacks 21/63/126 with the same weights, and
  against the book's regression score (ADR-028), over the same five years.
