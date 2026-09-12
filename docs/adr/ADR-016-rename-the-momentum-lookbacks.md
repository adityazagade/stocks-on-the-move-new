# ADR-016: Name the momentum lookbacks and weights for what they are

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

The momentum score is computed in `_composite_momentum` as

```python
comp = 0.6 * r21 + 0.3 * r63 + 0.1 * r126
```

where `r21`, `r63` and `r126` are returns over `LOOKBACK_R21`,
`LOOKBACK_R63` and `LOOKBACK_R126`, whose values are 5, 15 and 45 trading
days. The names come from the book's 21/63/126-day version; the values were
shortened between the v3 and v4 scripts, before version control, for a
reason nobody recorded. The docstring still reads
`score = (R21 + R63 + R126) × R²(90d)`, which names neither the lookbacks
nor the weights the code uses. ADR-001 cites this as its example of drift,
and it is rough edge 1 in the onboarding guide.

Every reader of the ranking code, human or assistant, has to reconcile the
names with the values before trusting anything. The golden test (ADR-009)
now makes a pure rename provable: if the score is unchanged, the expected
files are unchanged.

## Decision

Rename without changing any value.

- `LOOKBACK_R21`, `LOOKBACK_R63`, `LOOKBACK_R126` become `LOOKBACK_SHORT = 5`,
  `LOOKBACK_MID = 15`, `LOOKBACK_LONG = 45`.
- The weights leave the expression and become `WEIGHT_SHORT = 0.6`,
  `WEIGHT_MID = 0.3`, `WEIGHT_LONG = 0.1`, next to the lookbacks.
- The local variables `r21`, `r63`, `r126` become `r_short`, `r_mid`,
  `r_long`.
- The docstring states the formula the code computes:
  `score = (0.6·R5 + 0.3·R15 + 0.1·R45) × R²(90d)`, and says in one sentence
  that the lookbacks were shortened from the book's 21/63/126 before version
  control and that changing them again is a strategy ADR with the golden
  test as its evidence.
- Every other reference follows: `MIN_HISTORY`, the trailing-stop window,
  the tests, `ONBOARDING.md` sections 1 and 3, rough edge 1 removed.

The values 5, 15, 45 and 0.6, 0.3, 0.1 are not reconsidered here. Whether
they are right is a strategy question that deserves a backtest and its own
ADR; this one only makes the code say what it does.

## Consequences

### Positive

- The code, the docstring and the onboarding guide agree.
- The drift ADR-001 was written about is closed, on record.
- The next person to question the lookbacks starts from named constants
  and a golden test, not from archaeology.

### Negative

- A rename touches five places in `momentum.py`, one test and two documents
  for no runtime effect. Cheap, but it is a commit that adds nothing to the
  strategy.

### Neutral

- Ranking output is byte-for-byte unchanged; the golden test is the proof
  and its expected files do not move.

## Alternatives Considered

### Option 1: Status quo

**Cons:**

- The names lie, and ADR-001 says so in its Context.

### Option 2: Rename and also restore the book's 21/63/126

**Pros:**

- Names and values would match the book.

**Cons:**

- A strategy change hidden inside a rename, with no backtest. Rejected here;
  if wanted, it is its own ADR with the golden diff as evidence.

### Option 3: Keep the names, fix only the docstring

**Cons:**

- Half the lie stays: `LOOKBACK_R21 = 5` still reads as a mistake.

## Implementation Plan

1. **Rename**, one commit: constants, weights, locals, docstring, tests,
   onboarding sections 1 and 3, rough edge 1 removed.
2. **Validation before Implemented.** `uv run pytest`: the golden test passes
   with its expected files untouched, which proves the score is unchanged.

## References

- `src/stocks_on_the_move/momentum.py`: constants block, `_composite_momentum`,
  `MIN_HISTORY`, `_trailing_stop`
- `ONBOARDING.md`, section 1 (the deviation note), section 3 (filters), rough edge 1
- ADR-001 (Context), ADR-009 (the proof)

## Implementation Status

Implemented on 2026-09-12. Constants, weights, locals, docstring, `MIN_HISTORY`,
the trailing-stop window, the test and the onboarding guide renamed; no value
changed. Plan step 2 passed: 192 tests, the golden test's expected files
untouched, so the score is byte-for-byte what it was.
