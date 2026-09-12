# ADR-028: Rank by the book's regression slope, on backtest evidence

- **Status**: Rejected
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-13
- **Author**: Aditya Zagade

## Context

The book ranks stocks by the slope of an exponential regression over the
last 90 trading days, annualised, multiplied by the R² of that fit: a
measure of how steadily a stock has climbed, penalised for noise. This
port computes that slope and that R² and writes both to `ranking.csv`, and
then ranks by something else: a blend of the 5-, 15- and 45-day simple
returns weighted 0.6, 0.3 and 0.1, multiplied by the same R² (ADR-016).

The blend was introduced before version control, with lookbacks already
shortened from the book's 21/63/126, for a reason nobody recorded. Its
character is short: sixty percent of the score is one week's move. That
means high turnover in the top of the ranking, sensitivity to a single
day (ADR-025), and a ranking that can reorder between two runs on noise
the 90-day fit was meant to smooth.

None of this is evidence that the blend is worse. There has been no way to
compare. ADR-023 provides one, and this ADR commits in advance to what the
comparison has to show, so the decision is not made after seeing the
numbers.

## Decision

The ranking score becomes the book's: `annualise(slope_90) * r2_90`, the
two values the code already computes. The trailing-return blend and its
five constants retire from `StrategyParams`. `ranking.csv` keeps `score`,
`annual_slope` and `r2`; `score` becomes the product of the other two.

**Acceptance gate.** This ADR moves from Proposed to Accepted only on the
following comparison, run with the ADR-023 harness over at least four
years of the cached history, all other parameters equal:

1. **A**: the current blend, 5/15/45 at 0.6/0.3/0.1, times R².
2. **B**: the book's score, slope times R².
3. **C**: the blend with the book's lookbacks 21/63/126, same weights.

The book's score is adopted if B is not worse than A on return over
volatility and on maximum drawdown, and has lower annual turnover. If B is
worse on either of the first two, this ADR is marked **Rejected** with the
three summaries in Notes and the blend stays; if C beats both, a follow-up
ADR proposes C instead. "Not worse" means within two percent of A's figure
or better, to keep noise from deciding.

The three summaries are committed to Notes whatever the outcome, so the
next person who wonders why the score is what it is can read the numbers.

## Consequences

### Positive

- The ranking measures what the book argues for: steady trend, penalised
  for noise, over a horizon that a weekly strategy can act on.
- Lower turnover, if the gate passes, means fewer trades, less friction
  and fewer `BE` limit orders waiting for fills.
- The decision has a recorded reason, which the current score lacks.

### Negative

- If the gate fails, the work is the backtest run and a Rejected ADR. That
  is a result too.
- The golden fixtures were built to exercise the blend (a collapse
  anchored on the even-week date, a steady name); under the book's score
  the fixture ranking reorders and the expected files are regenerated,
  with the diff reviewed as an intended change.
- Every holding will be re-ranked under a different score on the first
  run after the change; the exits that follow are the cost of switching.
  The first run should be a plan (ADR-022) read before the real one.

### Neutral

- `annual_slope` and `r2` are already in the artifact; readers of
  `ranking.csv` see the same columns.
- `REG_LOOKBACK = 90` is unchanged.

## Alternatives Considered

### Option 1: Status quo, the blend

**Pros:**

- Known behaviour; the golden fixtures encode it.

**Cons:**

- No recorded reason, a one-week horizon, and exposure to single-day moves.
  Kept if the gate says it is better.

### Option 2: The blend with the book's lookbacks (variant C)

**Pros:**

- Smallest change to the code; longer horizon.

**Cons:**

- Still a construction the book does not use, with weights nobody chose
  deliberately. Included in the comparison so that if it wins, it wins on
  evidence.

### Option 3: Tune the weights and lookbacks on the backtest

**Cons:**

- Five free parameters fit to five years of one market is a curve fit. The
  book's score has none.

## Implementation Plan

1. **Prerequisites**: ADR-021 (params), ADR-023 (harness) at Implemented;
   ADR-025 lands first so the comparison includes the gap filter all three
   variants share.
2. **Comparison**: three runs, same range, same settings; the summaries
   into Notes; the owner sets Accepted or Rejected.
3. **Change**, if Accepted, one commit: the score, the retired constants,
   the golden update with its reviewed diff, the onboarding guide's
   deviation list and scoring section.
4. **Validation before Implemented.** Suite green; a `PLAN_ONLY=1` run read
   before the first live run under the new score.

## References

- ADR-016 (the blend's constants and the unrecorded change), ADR-021,
  ADR-023 (prerequisites), ADR-025 (the gap rule the blend makes urgent),
  ADR-022 (the plan before the first live run)
- Clenow, *Stocks on the Move*, the ranking chapter: exponential regression
  slope, 90 days, annualised, multiplied by R²
- `ONBOARDING.md` section 1, deviations; section 3, filters and scoring

## Implementation Status

**Rejected on 2026-09-13** by the acceptance gate, on the comparison below.
The blend stays. Nothing from the Decision was implemented; the only code
this ADR produced is the `score` switch that let the comparison run.

- On 2026-09-13 the harness gained what the comparison needs and nothing
  more: `StrategyParams.score`, `"blend"` by default and `"slope"` for the
  book's score, read by `composite_momentum`; `--set score=slope` selects
  it, and the override parser now accepts a `Literal` field. A live run is
  unchanged: the golden expected files did not move.
- The three runs, over the same range, all other parameters equal:

      python -m stocks_on_the_move.backtest run --from 2022-01-05 --to 2026-09-09 --label blend
      python -m stocks_on_the_move.backtest run --from 2022-01-05 --to 2026-09-09 --label slope --set score=slope
      python -m stocks_on_the_move.backtest run --from 2022-01-05 --to 2026-09-09 --label book-lookbacks \
          --set lookback_short=21 --set lookback_mid=63 --set lookback_long=126
      python -m stocks_on_the_move.backtest compare blend slope book-lookbacks

  Their summaries go in Notes whatever the outcome; the owner then sets
  Accepted or Rejected against the gate above.

## Notes

**The comparison**, run on 2026-09-13 over the owner's warm cache: 218
Wednesdays from 2022-07-13 to 2026-09-09, today's NIFTY 500 as the
universe throughout, every intent filled at the run date's close with the
configured fees and slippage, all other parameters at their live values
(the simple averages of ADR-024, the gap filter of ADR-025, the position
fraction of ADR-026, the cadence of ADR-027).

| Measure | A, the blend | B, the book's slope | C, the blend with 21/63/126 |
| --- | --- | --- | --- |
| CAGR | 18.14 % | 18.03 % | 18.88 % |
| Annualised volatility | 10.07 % | 10.46 % | 10.12 % |
| Return over volatility | 1.800 | 1.724 | 1.866 |
| Maximum drawdown | -6.49 % | -10.15 % | -7.94 % |
| Average positions | 19.2 | 19.8 | 19.9 |
| Average exposure | 50.3 % | 48.9 % | 49.2 % |
| Trades per year | 963 | 519 | 578 |
| Turnover per year | 21.1× | 7.2× | 8.9× |

**The gate, applied.** B is 4.2 percent below A on return over volatility,
outside the two percent the gate allows, and its maximum drawdown is
deeper by 3.7 points. B does have the lower turnover, a third of A's, but
the gate needs all three, so this ADR is Rejected and the blend stays.

**What the numbers also say.** The blend trades 963 times a year for about
nineteen positions: the whole portfolio turns over twenty-one times a year,
which is what sixty percent weight on a five-day return does. Variant C, the
same blend over the book's lookbacks, cuts trades by forty percent and
turnover by fifty-eight percent while its return over volatility is 3.7
percent higher and its CAGR three quarters of a point higher; its drawdown
is 1.45 points deeper than A's. C does not beat A on every measure, so the
gate's follow-up clause does not fire by itself. Whether lower turnover,
which the fixed slippage here understates the value of, is worth 1.45
points of drawdown is a judgment, and the natural next ADR: propose C with
a gate of its own, written before any further runs.

**Caveats** are the harness's (ADR-023): survivorship bias flatters all
three alike; fills at the close with fixed slippage flatter the highest
turnover most, so the blend's edge here is, if anything, overstated
relative to B and C; one name whose candles ended inside the range sat as
a zero-valued holding in all three runs.

The three result directories are `runs/backtests/2022-07-13_2026-09-09-blend`,
`-slope` and `-book-lookbacks`; `compare blend slope book-lookbacks` prints
the table above.
