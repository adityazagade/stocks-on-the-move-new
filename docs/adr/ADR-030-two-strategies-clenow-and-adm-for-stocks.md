# ADR-030: Two strategies, Clenow's and an ADM adapted to stocks, and no hybrid

- **Status**: Proposed
- **Date**: 2026-09-13
- **Last Updated**: 2026-09-13
- **Author**: Aditya Zagade

## Context

The ranking score this program has carried since before version control is
a hybrid nobody designed: the 5, 15 and 45-day returns weighted 0.6, 0.3
and 0.1, multiplied by the R² of Clenow's 90-day regression. The blend is
the shape of Accelerating Dual Momentum's score, the sum of overlapping
1, 3 and 6-month returns, compressed to weeks and tilted toward the
shortest window; the R² is Clenow's smoothness penalty on a regression
slope. ADM uses no R². Clenow uses no blend. The first real backtest
(ADR-028) showed what the hybrid does on five hundred stocks weekly: the
portfolio turns over twenty-one times a year and holds a name for two and
a half weeks. ADR-028 compared the hybrid with the book's score and kept
the hybrid on a pre-registered gate; that verdict was about performance
over one history, and the owner has since decided the hybrid goes on
design grounds.

The owner wants exactly two strategies in this program, each faithful to
its source: Andreas Clenow's *Stocks on the Move* as the book describes it,
and Accelerating Dual Momentum adapted from a three-fund rotation to a
stock universe. Not a plugin framework. Two entries behind one setting,
sharing the universe, the execution path, the ledgers, plan mode and the
backtest harness. The abstraction was tested against a dozen strategies in
discussion on 2026-09-13; every long-only, single-portfolio, price-based
strategy fits six slots and a cadence, and these two come from different
corners of that family, which is the proof the seam is real without
building more than two.

The book's exit rules are: the stock left the top of the ranking, fell
below its 100-day moving average, gapped 15 percent or more in 90 days, or
left the index. It has no trailing stop; the 5×ATR trailing stop under the
40-day high is this program's addition, from before version control.

## Decision

**One setting, `STRATEGY`**, `clenow` (default) or `adm`. **A `Strategy`
bundle** in `strategies.py`, a frozen dataclass of pure slot functions and
a cadence; the pipeline asks the bundle at each step and imports no rule
directly. The slots: `eligible(snapshot, params)`, `score(snapshot, params)`,
`regime(index_snapshot, params)` returning the exposure allowed and what to
hold otherwise, `exit_check(snapshot, rank, pct_rank, params)`,
`size(snapshot, equity, n_target, params)`, and `Cadence(rotation_days,
protect_weekly, resize_days)`.

**Clenow, the book.**

- Eligible: enough history; close above the 100-day simple moving average
  (ADR-024); 20-day volume at least `MIN_VOLUME`; ATR at most `MAX_ATR_PCT`
  of price; no gap of `MAX_GAP_PCT` in 90 days (ADR-025).
- Score: the annualised slope of the 90-day exponential regression times
  its R². `ranking.csv` keeps `score`, `annual_slope`, `r2`.
- Regime: the index above its 200-day simple moving average allows buys;
  below, no buys, holdings kept subject to the exits.
- Exits, every week: not eligible or not in the universe (`unranked:<why>`),
  percentile rank beyond `CUT_OFF_PCT`, close at or below the 100-day
  average, gap. **The trailing stop is retired** with `EXIT_MULTIPLE`; it is
  not the book's rule.
- Size: `RISK_FACTOR` of equity per ATR, capped at `MAX_WEIGHT`; resize every
  12 days (ADR-027); buys down the ranking weekly while bull and cash allow,
  `MIN_POSITION_FRACTION` applying (ADR-026).

**ADM adapted to stocks.**

- Score: the sum of the 21, 63 and 126-trading-day simple returns, equal
  weights, no R². `annual_slope` and `r2` stay in the artifact for reading.
- Eligible: 127 candles of history; the volume and gap filters, kept as
  hygiene for a stock universe; and **absolute momentum: score above zero**.
  The moving-average and ATR filters are Clenow's and do not apply.
- Regime: the index's own ADM score above zero allows exposure; at or below
  zero the exposure is zero, and at the next rotation every holding is sold
  and the portfolio is cash. A safe-asset holding (a liquid or gilt ETF) is
  future work with its own ADR; cash is the adaptation's answer for now.
- Portfolio: the top `MAX_POSITIONS` eligible names, equal weight, target
  equity divided by `MAX_POSITIONS` per name. The recommended `.env` for
  this strategy sets `MAX_POSITIONS=12`, in the article's 10 to 15.
- Cadence: rotation every 28 days, `last_rotation_date` kept in
  `strategy_state.json` beside `last_resize_date` (ADR-027), first run
  rotating. At a rotation: sell what left the top N, whose score turned
  non-positive, or everything if the regime is off; resize what stays to
  equal weight; buy the entrants. Between rotations nothing trades; there
  are no protective exits, which is ADM's design. `FORCE_RESIZE=1` forces a
  rotation.

**Shared and unchanged**: the universe and its last-good copy (ADR-020),
snapshots and pure rules (ADR-021), intents, the executor and plan mode
(ADR-022), the harness (ADR-023), the state files under `runs/` (ADR-029),
`MIN_SHARES`, fees and slippage.

**Retired**: the trailing-return blend, `WEIGHT_SHORT/MID/LONG`, the `score`
switch (ADR-028's comparison prerequisite), the trailing stop and
`EXIT_MULTIPLE`. `LOOKBACK_SHORT/MID/LONG` become 21, 63 and 126 and belong
to ADM. ADR-016 is Superseded; ADR-028 stays Rejected as history, with a
note that the hybrid it defended is retired here on design grounds.

**Switching strategies on a live portfolio** is a manual event. The state
file records the strategy that last traded; a run whose `STRATEGY` differs
refuses to trade unless `ALLOW_STRATEGY_SWITCH=1` is set for that one run,
and the operator reads a `PLAN_ONLY=1` run under the new strategy first,
because its first rotation may exit most of what the old one held.

**Golden test.** Clenow's expected files regenerate once, with the diff
reviewed and described: the score changes the order, the retired trailing
stop changes exits. ADM gets two configurations of its own, a rotation due
and a week between rotations, from the same fixtures.

**Backtest.** `run --strategy clenow|adm`. One comparison over the cached
history, recorded in Notes as information, not a gate: both strategies are
in the program regardless, and the harness's caveats apply unevenly, the
slower one more flattered by survivorship bias and untaxed rotation.

## Consequences

### Positive

- Every rule in the program has an author and a reason. The onboarding
  guide's list of deviations from the book shrinks to the filters kept as
  hygiene.
- Two strategies from different corners of the family, behind one seam,
  with the seam proven by the second.
- Turnover is a choice again: Clenow's weekly exits with a 90-day signal,
  or ADM's monthly rotation with a 1-to-6-month signal, and the harness
  shows what each costs.

### Negative

- Clenow's first run after the change re-ranks every holding under the
  book's score and without the trailing stop; the exits that follow are
  the price of fidelity. The plan is read first.
- ADM on stocks has no stops between rotations. A name can fall for four
  weeks before the rotation sells it. That is the design being adopted,
  and the reason `MAX_POSITIONS=12` and equal weight are recommended with
  it.
- Cash in a bear regime earns nothing; the safe-asset ETF is deferred.
- Three pull requests of change on a strategy that trades on Wednesdays;
  each keeps the golden test green for what it does not change.

### Neutral

- ADR-028's numbers remain the only backtest of the hybrid; they are
  history, not a benchmark.
- `MAX_POSITIONS` means two things: a ceiling for Clenow, the portfolio size
  for ADM. Documented rather than split.

## Alternatives Considered

### Option 1: Status quo, the hybrid with a `score` switch

**Cons:**

- A score neither author designed, with the churn ADR-028 measured, and a
  switch that only exists for comparison.

### Option 2: One strategy, Clenow faithful

**Pros:**

- Smallest change; no seam.

**Cons:**

- The owner wants the ADM adaptation too, and a single strategy leaves the
  seam unproven for the next one.

### Option 3: A general strategy framework

**Cons:**

- Two strategies justify a seam, not a plugin system. Anything that needs
  a pipeline change, shorting, tranches, several strategies at once, gets
  its own ADR when it comes.

### Option 4: ADM on ETFs, as the article

**Cons:**

- What the owner asked for is the stock adaptation. The ETF rotation, with
  a safe asset, is a natural third entry later and the reason the regime
  slot returns an action.

### Option 5: Keep the trailing stop in Clenow

**Pros:**

- A protection the owner has lived with.

**Cons:**

- Not the book's; it rides on top of four exits that already cover a
  broken trend. Retired for fidelity; it can return by ADR with evidence.

## Implementation Plan

1. **This ADR**, Proposed; Accepted when the owner says implement, with any
   of the decisions above amended first.
2. **The seam**, one pull request: `strategies.py` with the `Strategy`
   bundle and the current behaviour wired into its slots; `ctx.strategy`;
   the pipeline asking the bundle. Golden byte-identical.
3. **Clenow faithful**, one pull request: the slope-times-R² score, the
   trailing stop and its setting retired, the blend and its weights and the
   `score` switch removed, golden regenerated with the diff described,
   `.env.example`, the onboarding deviations list.
4. **ADM for stocks**, one pull request: snapshot returns over the three
   lookbacks, the absolute-momentum filter, equal-weight sizing, the
   rotation cadence and `last_rotation_date`, the regime by index score,
   `STRATEGY` and `ALLOW_STRATEGY_SWITCH`, the two ADM golden
   configurations, `ONBOARDING.md` sections 1 and 3 per strategy.
5. **Backtest** `--strategy`, one pull request, with the comparison in
   Notes.
6. **Validation before Implemented.** Suite green with the new golden files;
   `PLAN_ONLY=1` under each strategy on the live account read by the owner;
   the state file refusing a silent switch.

## References

- Clenow, *Stocks on the Move*: ranking, exits, sizing, the 200-day regime
- Engineered Portfolio, "Accelerating Dual Momentum Investing" (2018), read
  2026-09-13: the 1/3/6-month sum, the absolute test against zero, monthly
  rotation
- ADR-016 (superseded), ADR-021, ADR-022, ADR-023, ADR-024, ADR-025,
  ADR-026, ADR-027, ADR-028 (the hybrid's one backtest), ADR-029

## Implementation Status

Proposed; nothing implemented.
