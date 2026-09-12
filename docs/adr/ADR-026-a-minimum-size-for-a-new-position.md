# ADR-026: A minimum size for a new position

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-13
- **Author**: Aditya Zagade

## Context

Step 11 walks the ranking and, for each candidate, computes the ATR-based
target quantity. When the cash left does not cover it, the code buys as
many shares as the cash covers, down to `MIN_SHARES = 1`. The onboarding
guide states this as a design choice.

The strategy is risk parity: each position should carry about the same
risk, `RISK_FACTOR` of equity per ATR. A position bought at a tenth of its
target carries a tenth of the risk and occupies one of `MAX_POSITIONS`
slots. Two runs later it is resized up if cash allows, or stays a fragment
if the cash went to the next candidates. The book's rule is the other
way: if there is not enough cash for a full position, do not open it.

Neither extreme is obviously right for a small account. Full positions
only can leave several percent of equity idle every week; any fraction
leaves fragments.

## Decision

A new position is opened only when the affordable quantity is at least
`MIN_POSITION_FRACTION` of the target quantity.

- `MIN_POSITION_FRACTION` is a setting (ADR-007), default `0.5`, range 0 to
  1, in the "Sizing and risk" section. `1` is the book's rule; `0` is
  today's behaviour.
- The check applies in the buy step only. Resize buy-ups are already
  all-or-nothing on cash; exits, raise-cash and the kill switch are sells.
- A candidate below the fraction is recorded in `candidates.csv` as
  `SKIP:below_min_fraction` with the affordable quantity and the target in
  the row, and the loop continues to the next candidate, which may be
  cheaper per unit of risk and fit.
- `MIN_SHARES` stays at 1 as the absolute floor.

## Consequences

### Positive

- No position opens at a tenth of its intended risk. The portfolio is
  closer to the equal-risk book it is sized as.
- The reason a candidate was passed over is in the artifact.

### Negative

- Some cash sits idle until the next run when the last candidate that fit
  would have been a fragment. At the default, at most half a position's
  worth per skipped candidate.
- One more knob.

### Neutral

- Cheaper names later in the ranking may be opened in full where an
  expensive one was skipped. The ranking order still decides who is asked
  first.
- The golden fixtures may or may not include an under-cash candidate at
  the default; the update, if any, is reviewed.

## Alternatives Considered

### Option 1: Status quo, buy whatever the cash covers

**Cons:**

- Fragments with a fraction of the intended risk, holding a slot.

### Option 2: The book's rule, full positions only

**Pros:**

- Every position carries its intended risk from day one; no knob.

**Cons:**

- Idle cash every week for a small account. Available as the setting's
  value `1`.

### Option 3: Buy the fragment and top it up at the next run regardless of
resize week

**Cons:**

- Adds a fourth kind of buy to the pipeline for a case the fraction rule
  avoids.

## Implementation Plan

1. **Prerequisite**: ADR-022 at Implemented, so the buy step is a decide
   function that can return a skip with a reason.
2. **Change**, one commit: the setting, the check, the `candidates.csv`
   decision value, `.env.example`, a test that a candidate under the
   fraction is skipped and the next is bought.
3. **Evidence**, when ADR-023 is Implemented: the fraction at 0, 0.5 and 1
   over the cached history, in Notes. Not a gate; the default is a judgment
   the setting lets the owner change.
4. **Docs**: the onboarding guide's sizing paragraph.
5. **Validation before Implemented.** Suite green; golden diff, if any,
   reviewed.

## References

- ADR-022 (the buy step as a decide function), ADR-007 (the setting),
  ADR-023 (optional evidence)
- `src/stocks_on_the_move/momentum.py`: the `affordable_qty` branch of
  step 11
- `ONBOARDING.md` section 3, sizing

## Implementation Status

Implemented on 2026-09-13, one pull request.

- `MIN_POSITION_FRACTION` (default 0.5, 0 to 1) beside `MAX_POSITIONS` in
  the sizing and risk settings, on `StrategyParams` as
  `min_position_fraction`; `.env.example` regenerated.
- The buy step compares the affordable quantity with the fraction of the
  target after the share floor and before the intent; below it the row
  reads `SKIP:below_min_fraction` and the loop moves on. `candidates.csv`
  gains `target_qty` next to `qty`, so the fraction is readable per row.
- Tests: with most of the equity held and little cash, the calm candidate
  the cash covers under half of is passed over and the volatile one, whose
  ATR target is a quarter of the cost, is bought in full; at `0` the
  fragment is bought; at `1` only the full position is.
- The golden diff is described in the commit body.
- **Plan step 3**, the fraction at 0, 0.5 and 1 over the cached history,
  waits for the owner's warm cache (ADR-023 step 6).

## Notes

**Plan step 3, run on 2026-09-13** over the owner's warm cache: 218
Wednesdays from 2022-07-13 to 2026-09-09, the fraction at 0, 0.5 and 1,
everything else at the live values. The three runs are byte-identical:
same equity curve, same trades. The rule never fired, because with
`RISK_FACTOR` 0.001 the portfolio held about nineteen positions at about
half of equity in cash, so no candidate was ever short of cash. It matters
only when cash binds: a smaller account, a higher risk factor, or a run
that has just spent its cash on exits' replacements. The default stays at
one half as the guard for those cases; there is no evidence either way
about its value, and none expected while exposure sits near fifty percent.
Result directories `runs/backtests/2022-07-13_2026-09-09-blend`,
`-fraction-0` and `-fraction-1`.
