# ADR-024: Simple moving averages for the regime and trend filters

- **Status**: Proposed
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

The book gates buying on the index being above its 200-day simple moving
average and ranks only stocks above their 100-day simple moving average.
This port uses exponential moving averages for both, computed with pandas
`ewm(span=N, adjust=False)`; the onboarding guide records the deviation.

The computation has a bias the deviation note does not mention. With
`adjust=False` the series is seeded with the first close in the frame and
every later value is `(1 - a) * previous + a * close`, where `a = 2 / (N + 1)`.
After `n` rows the first close still carries weight `(1 - a) ** n`. The
frames are short:

| Average | Frame rows | Weight of the first close |
| --- | --- | --- |
| Index EMA-200 | about 205 | about 13 percent |
| Stock EMA-100 | about 120 | about 9 percent |

So an eighth of the "200-day average" the regime is decided on is a single
close from ten months ago, not an average of anything. The filter that
admits a stock to the ranking has the same shape. Both comparisons are
`last > average`, so a stale first close can flip the regime or admit and
exclude names on the margin for reasons that have nothing to do with the
trend.

The frames are short because the candle store's `days` argument is a
calendar window padded by 75 days, and the callers ask for exactly the
span. Asking for more candles fixes the bias by brute force; using the
book's definition removes the question.

## Decision

Both filters use **simple moving averages** over exactly their period:
the mean of the last 200 index closes for the regime, the mean of the last
100 closes for a stock's trend filter. The snapshot (ADR-021) exposes them
as `ma200` and `ma100`; the `Snapshot.from_candles` builder computes them;
a frame shorter than the period yields `nan` and the existing `history`
exclusion.

Names follow: the `ema100` column in `universe.csv`, `ranking.csv` and
`exits.csv` becomes `ma100`; the `ema200` key in `run.json`'s regime block
becomes `ma200`; `RankItem.ema100` becomes `ma100`; `MA_FILTER_100` and
`MA_PERIOD_200` in `StrategyParams` keep their values and gain clearer
names, `trend_ma_period` and `regime_ma_period`.

The golden test's expected files change and are regenerated once with the
diff reviewed: which names moved across the filter, and whether the
regime flipped on either fixture date.

## Consequences

### Positive

- The average is an average. The regime and the trend filter depend on the
  last N closes and nothing before them.
- The code matches the book, and the onboarding guide loses a deviation.
- No extra candles fetched; a 100-row frame suffices for a 100-day mean.

### Negative

- Behaviour changes on the margin: names within a few percent of their
  average may enter or leave the ranking on the first run after this
  lands, and the regime can differ on days the index sits near its average.
  That is the point, but the first live run after the change should be
  read with it in mind.
- The rename touches three artifact tables and `run.json`; anything
  outside the repository that reads `ema100` breaks. Nothing known does.

### Neutral

- An SMA lags an EMA of the same span by construction; the book's filters
  were designed for the SMA's lag.

## Alternatives Considered

### Option 1: Status quo, `ewm(adjust=False)` on short frames

**Cons:**

- The bias above, unstated anywhere.

### Option 2: Keep the EMA, set `adjust=True`

pandas normalises the weights over the available rows, so the first close
weighs what a finite exponential window gives it and no more.

**Pros:**

- Smallest code change; the EMA's responsiveness is kept.

**Cons:**

- Still not the book, and still a truncated window whose effective length
  depends on how many rows the cache happened to return. The deviation
  note would need a paragraph on `adjust`.

### Option 3: Keep the EMA, fetch three spans of candles

**Pros:**

- Converges the seed to under 0.3 percent weight.

**Cons:**

- Three times the candles for 500 names on every first fetch, to compute a
  filter the book defines in one line.

## Implementation Plan

1. **Prerequisite**: ADR-021 at Implemented, so the averages are snapshot
   fields.
2. **Change**, one commit: `ma100` and `ma200` in the snapshot, the two
   rules reading them, the renames, `pytest --update-golden`, and the
   reviewed diff of the expected files described in the commit body.
3. **Evidence**, when ADR-023 is Implemented: one backtest run of the
   current averages against the simple ones over the cached history, with
   the count of weeks on which the regime differs and of filter verdicts
   that differ, recorded in Notes. Not a gate; the change is a defect fix.
4. **Docs**: the onboarding guide's deviation list and filter description.
5. **Validation before Implemented.** Suite green with the new golden files;
   the first paper run's `universe.csv` compared against the previous
   run's on the same day for names that changed verdict.

## References

- pandas `ewm` semantics: https://pandas.pydata.org/docs/user_guide/window.html#exponentially-weighted-window
- ADR-021 (snapshot fields), ADR-009 (golden update), ADR-023 (optional
  evidence)
- `ONBOARDING.md` section 1, deviations from the book; section 3, filters

## Implementation Status

Proposed; nothing implemented.

## Notes

The bias figures use `(1 - 2 / (N + 1)) ** rows` with the row counts the
candle store's calendar window produces today; a longer cache does not
change them, because the callers ask for the span, not the file.
