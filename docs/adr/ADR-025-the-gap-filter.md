# ADR-025: Exclude names with a daily move above 15 percent in the last 90 days

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-13
- **Author**: Aditya Zagade

## Context

The book has four rules for whether a stock may be held: it is in the top
of the ranking, it is above its 100-day average, it is still in the index,
and it has had no single-day move of 15 percent or more in the last 90
days. The port has the first three. The gap rule is the one that says a
stock whose price jumped on news is not a trend.

The omission matters more here than in the book, because of how this
port scores. Sixty percent of the score is the five-day return. A stock
that gaps up 20 percent on Monday has a five-day return of roughly 20
percent on Wednesday and ranks near the top of the universe, above every
name that has climbed steadily. The R² of the 90-day fit dampens this,
but a single jump inside a flat series still leaves an R² in the range
that passes. The book's regression score is less exposed to one day; the
blend is very exposed, and nothing filters the day out.

## Decision

A stock is excluded from the ranking, and therefore exits if held, when
the largest absolute close-to-close daily return in its last 90 trading
days is at or above `MAX_GAP_PCT`.

- `MAX_GAP_PCT` is a setting (ADR-007), default `0.15`, range 0 to 1, in the
  "Sizing and risk" section next to `MAX_ATR_PCT`. A value of `1` disables
  the rule.
- The lookback is the strategy constant `gap_lookback = 90` in
  `StrategyParams` (ADR-021), the same window as the regression.
- The snapshot (ADR-021) gains `max_gap`: the largest absolute value of
  `close[t] / close[t - 1] - 1` over the last 90 closes.
- The filter chain (`evaluate`) checks it after the ATR rule, with reason
  `gap`. A held name that fails it becomes unranked and exits through the
  existing path; `exits.csv` names the cause as `unranked:gap`, and the
  same form applies to every other exclusion reason a held name can fail
  (`unranked:volume`, `unranked:below_ma100`), which today all read
  `unranked`.
- `universe.csv` gains a `max_gap` column.

The golden fixtures contain a collapse (the `CLIFF` instrument) that is
likely a single-day move above the threshold; the expected files are
regenerated once with the diff reviewed and described.

## Consequences

### Positive

- A news jump no longer buys its way to the top of the ranking through the
  five-day return.
- The rule is the book's, with the book's numbers, and is one field and
  one comparison.
- `exits.csv` says why an unranked holding was unranked, which it never did.

### Negative

- Names that gapped up on good news and kept going are excluded for 90
  days. That is the rule's intent and its cost; the book accepts it.
- A stock that gapped down 15 percent is sold on the next run even if it
  is still above its average and still ranked. Also the intent.
- One more knob in `.env.example`.

### Neutral

- Close-to-close, not open against the previous close, so an intraday
  spike that closed back is not a gap. Simpler, and the candles' opens
  are not needed for anything else.

## Alternatives Considered

### Option 1: Status quo, no gap rule

**Cons:**

- The five-day return rewards exactly the move the rule exists to exclude.

### Option 2: Measure the gap open against the previous close

**Pros:**

- Closer to the word "gap".

**Cons:**

- The book's concern is a violent move, not the overnight part of it. A
  stock that opened flat and ran 20 percent intraday is the same problem.
  Close to close catches both.

### Option 3: Penalise the score instead of excluding

Divide the score by one plus the largest gap, or similar.

**Cons:**

- A tuning knob with no basis in the book and a smooth effect that is
  harder to reason about than a rule. Exclusion is the book's answer.

### Option 4: Apply the rule at entry only, not to holdings

**Cons:**

- The book applies it as an exit too. A held name that gapped down 20
  percent is the trend having ended.

## Implementation Plan

1. **Prerequisite**: ADR-021 at Implemented.
2. **Change**, one commit: the setting, the snapshot field, the filter
   rule, the `unranked:<reason>` form, the `universe.csv` column,
   `.env.example`, unit tests on a series with and without a gap, the
   golden update with its reviewed diff.
3. **Evidence**, when ADR-023 is Implemented: with and without the rule
   over the cached history, recorded in Notes. Not a gate; the rule is the
   book's.
4. **Docs**: the onboarding guide's filter and exit lists.
5. **Validation before Implemented.** Suite green; the first paper run's
   `universe.csv` filtered on `reason = gap`, read by hand.

## References

- ADR-021 (snapshot and params), ADR-007 (the setting), ADR-009 (golden
  update), ADR-028 (the score that makes this rule urgent)
- `ONBOARDING.md` section 3, filters and exit rules

## Implementation Status

Implemented on 2026-09-13, one pull request, golden expected files
regenerated and reviewed.

- `MAX_GAP_PCT` (default 0.15, 0 to 1) beside `MAX_ATR_PCT` in the sizing
  and risk settings, on `StrategyParams` as `max_gap_pct` with
  `gap_lookback = 90`. `.env.example` regenerated.
- `Snapshot.max_gap` is the largest absolute close-to-close return over the
  last 90 returns, `nan` with fewer than two closes. `evaluate` checks it
  after the ATR rule with reason `gap`; `universe.csv` gains the `max_gap`
  column; `Evaluation` carries it.
- `exit_check` takes an `unranked_cause`; `decide_exits` supplies it by
  evaluating the held name's snapshot, so `exits.csv` reads
  `unranked:gap`, `unranked:below_ma100`, `unranked:volume`,
  `unranked:not_in_universe` for a name that passes every filter but left
  the index, or `unranked:error:<type>` for a name with no instrument. A
  holding with no snapshot at all still reads `unranked`.
- Tests: the gap measurement on a steady series, a 20 percent jump inside
  and outside the window, the `gap` verdict and its place after the ATR
  rule, the rule disabled at 1, and the `unranked:<cause>` form.
- The golden diff is described in the commit body.
- **Plan step 3**, with and without the rule over the cached history, waits
  for the owner's warm cache (ADR-023 step 6).
