# ADR-009: A golden-file regression test on a frozen candle cache

- **Status**: Accepted
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

The test suite covers pure helpers: parsing, CSV round-trips, ATR, the
momentum formula on synthetic series. Nothing checks that the pipeline, given
the same market data, produces the same ranking, the same exits and the same
sizes as it did last month. Any change to `rank_universe`, `should_exit`,
`target_shares`, or to pandas itself, is verified today by a human reading
log lines from two runs.

ADR-003 makes this concrete: the pandas upper bound cannot move until
ranking, exits and sizes have been compared across versions, and no artefact
exists to compare. ADR-006 defines the CSV schemas such a comparison would
use. ADR-008 provides the fake broker and the injectable clock that make a
deterministic run possible.

The real candle cache holds 2,846 instruments, about 30 MB, median 179 daily
rows per instrument, and includes the NIFTY 50 index token. It is the
natural source of realistic fixtures.

## Decision

Add a single end-to-end regression test that runs the decision pipeline
against frozen inputs and compares its outputs to committed expected files.

- **Fixtures** in `tests/fixtures/golden/`:
  - `candles/<token>.csv` for about forty instruments copied from
    `.cache_candles/`, chosen to exercise every branch: names that rank,
    names that fail each filter (history, EMA-100, volume, ATR percentage),
    at least two `-BE` series names, the index token, and every name in the
    fixture portfolio.
  - `instruments.csv`: the matching rows of Kite's NSE instrument dump.
  - `nifty500.txt`: the fixture universe.
  - `ltp.csv`: last prices for the held names and buy candidates.
  - `portfolio_before.csv`, a cash ledger and a trades ledger for the fixture
    account.
  - `meta.json`: the frozen `as_of` date, the settings used (ADR-007 model
    dump), and the date the fixtures were taken.
- **Expected outputs** in `tests/fixtures/golden/expected/`: `ranking.csv`,
  `universe.csv`, `exits.csv`, `sizing.csv`, `candidates.csv`,
  `portfolio_after.csv`, using the ADR-006 schemas verbatim.
- **The test** builds a `Settings` from `meta.json`, a `FakeBroker` from the
  fixtures, a clock frozen at `as_of`, runs steps 5 to 12 of the pipeline
  through `PaperBroker`, and compares each output with
  `pandas.testing.assert_frame_equal(check_exact=False, rtol=1e-9)`.
- **Regeneration** is explicit: `uv run pytest --update-golden` rewrites the
  expected files. The resulting diff is reviewed in the commit like any code
  change, and the commit body explains it. A silently regenerated golden is
  no test at all.
- **Two configurations.** The test runs twice, once with `as_of` on an even
  ISO week and once on an odd one, so both the resize and the skip path are
  covered.
- Fixture selection and refresh is a script,
  `tests/fixtures/golden/make_fixtures.py`, that reads the live cache and a
  list of tokens, so the snapshot can be re-taken deliberately.

## Consequences

### Positive

- Any change to the strategy, its dependencies, or the Python version shows
  up as a concrete diff in ranking, exits or sizes before it reaches a live
  run.
- ADR-003 gets its exit condition: run the test on pandas 3, read the diff.
- Refactors like ADR-008 can be verified mechanically instead of by comparing
  log lines.

### Negative

- Real market data enters the repository, on the order of half a megabyte.
  Zerodha's terms restrict redistribution of historical data; a private
  repository is not redistribution, but if the repository is ever made
  public the fixtures must be replaced with synthetic series first. This is
  recorded as a condition in `.gitignore`'s companion note and in Notes below.
- The test is only as honest as the expected files. The `--update-golden`
  discipline is a process rule, not something the code can enforce.
- Fixtures age. As lookbacks or filters change, forty instruments may stop
  exercising every branch; the selection script has to be re-run and the
  coverage claim re-checked.

### Neutral

- The test depends on ADR-006 (schemas), ADR-007 (settings), ADR-008 (fake
  broker, clock). It cannot be implemented before them.
- Runtime is a few seconds; it stays in the default `pytest` run.

## Alternatives Considered

### Option 1: Status quo, unit tests on pure helpers only

**Pros:**

- Fast, simple, no fixtures.

**Cons:**

- Cannot detect a behaviour change in the pipeline or in pandas. ADR-003
  has no exit condition.

### Option 2: Synthetic candles generated with a seeded random walk

**Pros:**

- No licence question; arbitrarily many instruments.

**Cons:**

- Does not exercise the filters the way real data does: no gaps, no
  splits, no `-BE` names, no realistic volume profile. Kept as the fallback
  if the repository goes public.

### Option 3: Compare against a live run each time

**Pros:**

- No fixtures to maintain.

**Cons:**

- Not reproducible, needs a broker session, and the reference changes daily.

### Option 4: Property-based tests over the pipeline

**Pros:**

- Finds edge cases a fixture never would.

**Cons:**

- Cannot answer "did the ranking change" for a given input, which is the
  question ADR-003 asks. Complementary, not a substitute.

## Implementation Plan

1. **Prerequisites** landed: ADR-006, ADR-007, ADR-008 at Implemented.
2. **Fixture script and fixtures**, one commit: `make_fixtures.py`, the
   snapshot, `meta.json`. Commit body lists the tokens and why each was
   chosen.
3. **Test and expected files**, one commit: the test, the `--update-golden`
   option in `conftest.py`, the first expected files generated from the
   current build. The body states that the expected files encode current
   behaviour, not verified-correct behaviour.
4. **Validation before Implemented.** Change one constant deliberately, for
   example `CUT_OFF_PCT`, and confirm the test fails with a readable diff.
   Revert.

## References

- ADR-003 (why this must exist), ADR-006 (schemas), ADR-007, ADR-008
- `.cache_candles/` (fixture source), `.cache_candles/256265.csv` (index)
- `tests/test_momentum.py` (existing unit tests)

## Implementation Status

Not started.

## Notes

Licence condition: the fixtures are real Kite historical data and stay in
the repository only while it is private. Making the repository public
requires replacing them with synthetic data first.
