# ADR-027: Resize on elapsed time since the last resize, not ISO-week parity

- **Status**: Proposed
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

Position sizes are rebalanced toward their ATR targets every second week,
as the book prescribes. The code decides "second week" by the parity of
the ISO week number: even weeks resize, odd weeks skip, `FORCE_RESIZE=1`
overrides.

Parity is a property of the calendar, not of the portfolio. Two cases
break it:

- **A 53-week ISO year.** 2026 is one: 31 December 2026 is in week 53 and
  the next Wednesday, 6 January 2027, is in week 1. Both odd. The resize
  due in the first week of January happens in the second, three weeks
  after the last one.
- **A missed run.** A Wednesday holiday, an outage or the operator away
  shifts nothing in the calendar, so a resize can land one week after the
  previous one or three weeks after, depending on which week was missed.

The cadence the book means is "every other run", or "about every
fourteen days since the last time". Neither is derivable from the
ledgers: a resize that produced no trades leaves no trace anywhere.

## Decision

The run keeps a small state file and resizes when enough time has passed
since the last resize.

- `strategy_state.json`, a fifth versioned state file next to the four
  ledgers, path in the `STATE_FILE` setting (default
  `strategy_state.json`), holding `last_resize_date` and nothing else for
  now. It is state the code reads to run, so it is committed like the
  ledgers (ADR-004), never hand-edited, and written by the run only.
- Resize when the file is absent, or when the run date is at least
  **12 days** after `last_resize_date`, or when `FORCE_RESIZE=1`. Twelve
  tolerates a run shifted by up to two days either way while never
  resizing on consecutive weekly runs.
- The date is written when a resize was performed, whether or not it
  produced a trade. Plan mode (ADR-022) reads it and does not write it.
- `run.json` records `resize_performed` as today and gains
  `last_resize_date` before and after.
- The golden test's two configurations keep their names and gain a
  `strategy_state.json` fixture each: `even_week` with a date fourteen days
  before its run date, `odd_week` with seven. Their expected files do not
  move.
- `CLAUDE.md` rule 4 says "the five state files"; `ONBOARDING.md`'s file
  lifecycle table gains the row; ADR-004 is not superseded, this ADR
  extends its list.

## Consequences

### Positive

- The cadence survives the 2026 year end and any missed run.
- "When did we last resize" has an answer without reading the trades
  ledger.
- The file has room for the next piece of state that needs a home, with
  the same rules.

### Negative

- A fifth file to commit after a run, and one more thing paper mode writes
  (rule 5).
- Twelve days is a judgment. Seven would resize every run; fourteen would
  miss a run shifted one day earlier.

### Neutral

- The first run after this lands finds no file and resizes; the operator
  can pre-seed the file with the last even-week date to keep the cadence.
- `FORCE_RESIZE` is unchanged in meaning.

## Alternatives Considered

### Option 1: Status quo, ISO-week parity

**Cons:**

- Three-week gap at the 2026 year end; one- or three-week gaps after any
  missed run.

### Option 2: Parity relative to a fixed epoch date

Weeks since a chosen Wednesday, modulo two.

**Pros:**

- No state file.

**Cons:**

- Fixes the year-end case, not the missed-run case; still the calendar
  deciding.

### Option 3: Mark resize trades in the trades ledger and derive the date

**Cons:**

- A resize with no trades leaves no mark; the ledger's columns are the
  ledger's (ADR-004) and a marker column changes every reader.

### Option 4: Count runs, resize every second one

**Cons:**

- Needs state anyway, and "every second run" after a three-week gap is
  worse than "at least twelve days".

## Implementation Plan

1. **Prerequisite**: ADR-020 at Implemented, so the state file joins
   `ledger.py`.
2. **Change**, one commit: the setting, the read and write, the rule in the
   resize step, `run.json` fields, the golden fixtures, `.env.example`,
   tests for absent file, 7 days, 12 days, 14 days, force, and plan mode not
   writing.
3. **Docs**: `CLAUDE.md` rule 4, `ONBOARDING.md` lifecycle table and
   section 3 step 9, `README.md` file list.
4. **Validation before Implemented.** Suite green; golden expected files
   untouched; one paper run writes the file with the run date.

## References

- ADR-004 (versioned state), ADR-014 (`CLAUDE.md` rule 4 changes here),
  ADR-022 (plan mode reads, does not write), ADR-009 (fixtures)
- `src/stocks_on_the_move/momentum.py`: `resize_positions`, the parity
  check
- ISO week 53 in 2026: `date(2026, 12, 31).isocalendar()` is week 53

## Implementation Status

Proposed; nothing implemented.
