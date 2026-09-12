# ADR-021: One snapshot per symbol, and rules that are pure functions of it

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

Every strategy rule takes the `RunContext` and fetches what it needs. For
one held, ranked symbol in one run:

- `evaluate_instrument` calls `candles.get(token, 100)`, computes the
  100-day EMA, the 20-day volume, the ATR and the score.
- `exit_reasons` calls `_trailing_stop`, which calls `candles.get(token, 45)`
  and computes the ATR again and the 40-day high.
- `resize_positions` calls `size_position`, which calls
  `candles.get(token, 20)` and computes the ATR a third time.

Each `get` reads and parses the symbol's CSV from disk. The three frames
have different lengths, so a reader has to convince themselves the three
ATRs are the same number. They are, because the ATR uses only the last
twenty true ranges and the rolling high only the last forty closes, but
nothing in the code says so.

Because the rules take the context, none of them can run without a candle
store, a broker and settings. A unit test of the trailing stop builds a
fake broker with candles. A backtest (ADR-023) cannot call them at all
without a fake of everything.

Prices come from three places inside one decision. Sizing caps the position
by the last candle close. Affordability and equity use the live last price.
Orders on `BE` and `BZ` names use the quote's top of book. Nothing states
which price a given number came from.

The strategy constants are module-level names in `momentum.py`. The
`Settings` object holds the operator's knobs (`RISK_FACTOR`, `MAX_WEIGHT`,
`CUT_OFF_PCT`, `EXIT_MULTIPLE`, `MIN_VOLUME`, `MAX_ATR_PCT`, `ATR_PERIOD`).
A rule reads both. A backtest that wants to try `LOOKBACK_SHORT = 21` has to
edit the module.

## Decision

Rules become pure functions of a **snapshot** and a **parameter set**. All
market data is gathered once per run; no rule touches the candle store,
the broker or the settings.

**`StrategyParams`**, a frozen dataclass in `rules.py`: every strategy
constant (lookbacks and weights, regression lookback, the two moving-average
periods, trading days per year, minimum shares) and every strategy knob
from `Settings` (ATR period, risk factor, maximum weight, cut-off, exit
multiple, minimum volume, maximum ATR fraction). `StrategyParams.from_settings(settings)`
builds the live one; a backtest builds variants. The module-level constants
become the dataclass defaults and are deleted as names.

**`Snapshot`**, a frozen dataclass in `indicators.py`, built once per
instrument by `Snapshot.from_candles(symbol, token, frame, params)`:

- the closes, highs, lows and volumes as arrays, and the row count;
- the derived values every rule reads: last close, the trend moving
  average, the ATR, the 20-day average volume, the 40-day high, and the
  score inputs (the three trailing returns, the regression slope and R²);
- `enough_history`, so the filter's first rule is a field, not a length
  check in three places.

Fields are `nan` where the frame is too short. Building a snapshot does no
I/O and logs nothing.

**Gather step.** The pipeline builds `snapshots: dict[str, Snapshot]` for
the universe and the current holdings, one `candles.get(token, history_days)`
per instrument where `history_days` is the maximum any rule needs (today
100), and one for the regime index at its own 200. The candle store is
called in exactly one function.

**Rules**, all in `rules.py`, all pure:

- `evaluate(snapshot, params) -> Evaluation`
- `rank(evaluations) -> list[RankItem]`
- `exit_check(snapshot, rank, pct_rank, params) -> ExitCheck`
- `size(snapshot, equity, params) -> Sizing`
- `regime(index_snapshot, params) -> Regime`, a new record with `bull`, the
  last close and the moving average, replacing the tuple `index_trend`
  returns.

**Prices.** The snapshot's last close is a candle close and is named so.
Live last prices stay a separate `prices: dict[str, float]` the pipeline
fetches once per step where it needs them, for affordability, equity and
the trailing valuation. This ADR does not change which price any decision
uses; it makes the source visible at each call.

**Equivalence.** The numbers do not change. The ATR is the mean of the last
`atr_period` true ranges whatever the frame length; the 40-day high is the
maximum of the last forty closes; the 100-day EMA is computed on the same
100-day frame the filter uses today; the score reads fixed offsets from the
end. The golden test stays byte-identical, and the plan checks it after
every commit.

## Consequences

### Positive

- One CSV read and one indicator computation per symbol per run, instead
  of up to three.
- A rule is a function of two values. Its tests are a snapshot built from a
  list of closes and a parameter set, no broker, no candle store, no
  settings.
- The backtest (ADR-023) calls the same functions the live run calls. That
  is the whole point of the change.
- Which price each decision uses is written at the call, not implied by
  which helper was reached for.

### Negative

- Holdings that are not in the universe now get a snapshot too, one extra
  candle fetch per such name per run. Today they are exited as `unranked`
  without one. Small, and the fetch is cached after the first run.
- `StrategyParams` duplicates seven fields of `Settings`. The alternative,
  rules reading `Settings` directly, ties them to environment configuration
  and to pydantic. The duplication is one constructor.
- Some 400 lines move again, right after ADR-020. Sequenced so that ADR-020
  puts the rules in one file first and this ADR changes their signatures.

### Neutral

- `Evaluation`, `ExitCheck`, `Sizing` and `RankItem` are unchanged; their
  rows in the artifacts are unchanged.
- `index_trend`'s tuple becomes a `Regime` record; `run.json` keeps the same
  three keys.

## Alternatives Considered

### Option 1: Status quo, rules over the context

**Pros:**

- Each rule fetches exactly what it needs and nothing more.

**Cons:**

- Three reads and three computations per held symbol; no rule is testable
  or backtestable without the whole apparatus; the price source is implicit.

### Option 2: One indicators DataFrame for the whole universe

Compute every indicator for every symbol as columns of one frame,
vectorised.

**Pros:**

- Fast, and one table to inspect.

**Cons:**

- Five hundred names and a handful of indicators do not need vectorising.
  A per-symbol record reads as the rule it feeds; a wide frame reads as
  pandas.

### Option 3: A `Strategy` class holding params and rules as methods

**Pros:**

- One object to construct, pass and subclass for variants.

**Cons:**

- Methods on a class invite state. Functions of a snapshot and a params
  value cannot hold any, which is the property ADR-008 asked for and this
  ADR extends to the rules.

## Implementation Plan

1. **Prerequisite**: ADR-020 at Implemented, so the rules sit in one module.
2. **Params**, one commit: `StrategyParams`, `from_settings`, the constants
   removed as module names, every reader taking `params`.
3. **Snapshot**, one commit: `Snapshot.from_candles`, unit tests for each
   derived field against hand-computed values on short series, including
   `nan` fields on a short frame.
4. **Rules**, one commit each: `evaluate`, `exit_check`, `size`, `regime`
   take a snapshot; the gather step in the pipeline; the context-based
   versions and their candle calls deleted. `tests/test_rules.py` replaces
   the rule tests in `tests/test_pipeline.py` and `tests/test_momentum.py`
   with snapshot-based ones.
5. **Validation before Implemented.** Golden files untouched after every
   commit; the suite green; `ty` clean; one paper run whose `universe.csv`,
   `exits.csv` and `sizing.csv` match the previous paper run's on the same
   candle cache, column for column.

## References

- ADR-020 (the module the rules land in), ADR-009 (the equivalence check),
  ADR-016 (the constants that become `StrategyParams` defaults), ADR-023
  (the consumer)
- `src/stocks_on_the_move/momentum.py`: `evaluate_instrument`,
  `_trailing_stop`, `size_position`, `index_trend`, `_composite_momentum`

## Implementation Status

Implemented on 2026-09-12, one pull request, golden expected files untouched.

- **`StrategyParams`** lives in `params.py`, not `rules.py` as the Decision
  said: `indicators.py` needs it to build a snapshot and `rules.py` imports
  `indicators.py`, so the parameters sit below both. Its knob fields default
  to the `Settings` defaults read off the model, so the two cannot drift; a
  test asserts `from_settings` on default settings equals `StrategyParams()`.
  The two moving-average periods are named `trend_ma_period` and
  `regime_ma_period` from the start, the names ADR-024 planned.
- **`Snapshot`** in `indicators.py`: rows, last, the trend EMA, ATR, 20-day
  volume, the rolling high over the stop window, the score triple,
  `enough_history`, the closes, and `error` for a frame that could not be
  fetched or built (`Snapshot.failed`). `composite_momentum` is public and
  takes the params.
- **Rules** in `rules.py`: `regime`, `evaluate`, `rank`, `exit_check`,
  `trailing_stop`, `size`, each a function of a snapshot and the params. The
  two log lines they keep, "Not enough candles for trailing stop" and "Not
  enough data to rank", are the ones the old code had.
- **Gather** in `pipeline.py`: `gather_snapshots(ctx, instruments)` fills
  `ctx.snapshots` for the universe and every holding, skipping symbols
  already gathered, so the steps call it with no instruments to be sure a
  direct call has its holdings covered; a fetch that throws is a failed
  snapshot and a WARNING. `index_snapshot` reads the index over its own
  window. `rank_step` gathers, evaluates into `universe.csv` and ranks.
  `size_for` reaches a gathered snapshot for resize and the buy loop, and a
  failed snapshot's own error text goes in the "size calc error" line, so the
  message for a holding without an instrument reads as before.
- **Context**: `snapshots` and an optional `params` override on
  `RunContext`; `strategy_params(ctx)` resolves it.
- **Tests**: `tests/test_rules.py` (seventeen) over snapshots built from
  synthetic candles: params, snapshot fields, the score, regime, every
  exclusion reason, ranking order, the trailing stop, every exit reason,
  sizing and its refusals. Two pipeline tests cover the gather: a fetch that
  throws becomes an error row, and a second call fetches nothing new. The
  context-based rule tests are gone. 238 tests pass; `ty` clean.
- **Equivalence held**: the golden expected files did not move.

## Notes

ADR-024 and ADR-025 add fields to the snapshot (a simple moving average, the
largest daily gap). They are written against this ADR's shape and wait for
it.
