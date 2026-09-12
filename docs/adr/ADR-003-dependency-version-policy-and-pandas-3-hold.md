# ADR-003: Dependency version policy and the pandas 3 hold

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

The strategy's numbers come out of pandas: `ewm` for the EMAs, `rolling` for
the trailing stop, `concat`, `merge` and `drop_duplicates` in the candle
cache, `read_csv(parse_dates=...)` and positional `.at[]` access. Any change
in pandas semantics can move a filter boundary or a position size without
raising an error.

At migration time (ADR-002) the latest releases were pandas 3.0.5, numpy
2.5.3 and kiteconnect 5.2.1, against installed 2.2.3, 2.0.2 and 5.0.1.
pandas 3.0 changes defaults that matter here: copy-on-write becomes
mandatory, and the default string dtype changes from `object` to a dedicated
`str` type. Neither is expected to break this code, but "expected" is not
"verified", and there is no harness that compares one run's ranking against
another.

The old `requirements.txt` used `~=` at patch level, which is stricter than
the code needs and would have blocked the numpy 2.5 wheels required for
Python 3.13.

## Decision

- `pyproject.toml` bounds each dependency to its current major:
  `kiteconnect>=5.0.1,<6`, `numpy>=2.0,<3`, `pandas>=2.2,<3`.
- `uv.lock` pins exact versions. Upgrading is a deliberate act:
  `uv lock --upgrade && uv sync`, then tests, committed on its own with the
  resolved versions in the commit body.
- A major-version bump of any runtime dependency requires its own ADR with a
  validation plan.
- For pandas 3 specifically, the validation plan must at minimum: run the
  test suite; run a paper run on pandas 2.x and 3.x against the same candle
  cache on the same day; diff the ranking, the exit decisions and the target
  sizes. Only an empty diff, or an explained one, unblocks the bound.

## Consequences

### Positive

- Behaviour of a money-handling script stays fixed until someone chooses to
  change it and can show what changed.
- Minor and patch upgrades still flow, so security and bug fixes inside a
  major arrive with a normal `uv lock --upgrade`.
- The lockfile gives bit-for-bit reproducibility regardless of the bounds.

### Negative

- Fixes that land only on pandas 3.x are not received.
- The 2.x line will eventually stop getting releases; the hold has a shelf
  life and the follow-up ADR should not wait for that to happen.
- kiteconnect moved 5.0.1 to 5.2.1 in the same step with no dedicated
  verification beyond the REST surface being unchanged. This is accepted as
  low risk because the client is a thin HTTP wrapper, and is noted here so
  the next reader knows it was not tested separately.

### Neutral

- Automated dependency bots, if ever added, must be configured to respect
  the upper bounds.

## Alternatives Considered

### Option 1: Exact `==` pins in `pyproject.toml`

**Pros:**

- Maximum stability without reading the lockfile.

**Cons:**

- Duplicates what `uv.lock` already does and blocks patch fixes.

### Option 2: No upper bounds

**Pros:**

- Always current after `uv lock --upgrade`.

**Cons:**

- pandas 3 would arrive silently on the next upgrade, with no test able to
  tell whether the ranking changed.

### Option 3: Upgrade to pandas 3 immediately

**Pros:**

- Best long-term position; avoids a second migration.

**Cons:**

- No comparison harness exists yet. Doing it in the same commit as a Python
  and packaging migration would make any behaviour change impossible to
  attribute.

## References

- `pyproject.toml`, comment above the pandas line
- ADR-002
- pandas 3.0 release notes (copy-on-write, string dtype)

## Implementation Status

Implemented on 2026-09-12. The pandas 3 validation is an open follow-up
awaiting its own ADR.
