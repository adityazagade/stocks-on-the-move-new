# ADR-018: End the candle window on the IST date

- **Status**: Proposed
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

`CandleStore` ends every candle window at "today", and its default for
today is the machine's local date (`datetime.now().date()`). Every other
date in the program is IST: the weekday guard, the ISO-week parity, the
ledger timestamps, the run directory name. On the owner's Mac, set to IST,
the two agree. On a machine in another zone, or a CI runner on UTC between
18:30 and 24:00 UTC, the window ends a day early or late, the cache's
"current" check misjudges, and a run on the same data can differ by one
candle. This is rough edge 4. ADR-008 made the date an injected callable
(`today=`) precisely so this could be a one-line change made on its own.

## Decision

The default `today` for `CandleStore` becomes the IST date:
`datetime.now(ZoneInfo("Asia/Kolkata")).date()`. `main()` passes the run's
clock explicitly, `today=lambda: ist_now().date()`, so the store and the
rest of the run read the same clock. Tests already inject a frozen date and
are unaffected.

## Consequences

### Positive

- One clock for the whole program. Rough edge 4 is closed.

### Negative

- On a machine not set to IST, the window end moves by up to a day compared
  with the old behaviour. That is the correction, not a side effect, and it
  is the only behaviour change.

### Neutral

- On the owner's machine the dates are identical; the golden test, which
  injects its date, is untouched.

## Alternatives Considered

### Option 1: Status quo, the machine's date

**Cons:**

- Correct only by coincidence of the machine's time zone.

### Option 2: Take "today" from the newest candle Kite returns

**Cons:**

- Requires a request before the window can be computed, and turns a holiday
  into a shorter window. The calendar date is the right anchor.

## Implementation Plan

1. **Change**, one commit: the default in `candles.py`, the explicit `today`
   in `main()`, a unit test that the default is the IST date, rough edge 4
   removed from the onboarding guide.
2. **Validation before Implemented.** `uv run pytest`; the golden test's
   expected files are untouched.

## References

- `src/stocks_on_the_move/candles.py`: `_machine_today`, `CandleStore.__init__`
- `src/stocks_on_the_move/momentum.py`: `main()`, `ist_now`
- ADR-008 (made the date injectable), `ONBOARDING.md`, rough edge 4

## Implementation Status

Not started.
