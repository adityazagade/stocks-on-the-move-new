# ADR-017: Adjust positions only after a trade was placed

- **Status**: Proposed
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

`safe_sell` and `safe_buy` return the price a trade was booked at, or
`None` when no trade happened: no quote and no last price came back
(ADR-008), or, for a buy, not enough cash. Every buy site already checks
for `None`. Three sell sites do not:

- `prune_portfolio` pops the holding after `safe_sell` whatever it returned;
- `raise_cash_if_needed` subtracts the quantity whatever it returned;
- `liquidate_all` pops the holding whatever it returned;
- `resize_positions` subtracts the sell-down whatever it returned, though
  since ADR-006 it does record the row as `SKIP:not_placed`.

The result of a missing price is therefore a holding that disappears from
`next_portfolio.csv` with no trade in the ledger and no change to cash. The
next run starts from a snapshot that understates what the account holds.
Nothing was sent to the broker, so nothing was lost, but the program's
record of the account is wrong, silently. Before ADR-008 the same path sent
a market order at price 0 and recorded nothing, which was worse; ADR-008
made the skip explicit and ADR-006 made it visible, and rough edge 3 records
the bookkeeping half that remained.

The wider part of rough edge 3, that a placed limit order on a `BE`/`BZ`
name may never fill while the ledger says it did, needs order-status
polling against the broker and is not addressed here.

## Decision

A position changes only when a trade was placed. Concretely:

- `prune_portfolio`: if `safe_sell` returns `None`, the holding stays in
  `positions`, is not added to `sold`, and its `exits.csv` row reads
  `decision = SKIP:no_price` with the reasons that would have sold it.
  A WARNING names the symbol.
- `resize_positions`: a sell-down that returns `None` leaves the quantity
  as it was; the row already says `SKIP:not_placed`.
- `raise_cash_if_needed`: a sell that returns `None` leaves the holding and
  moves on to the next candidate; the closing WARNING about cash still
  short covers the outcome.
- `liquidate_all`: a holding that could not be sold stays in `positions`;
  the kill-switch run ends with a WARNING listing the unsold names and
  writes them to `next_portfolio.csv`, so the operator sees what is still
  held.
- No change to `safe_sell` or `safe_buy` themselves, to the ledger, or to
  any path where a price was available.

## Consequences

### Positive

- `next_portfolio.csv` never loses a holding the broker still has.
- A skipped exit is a WARNING and a `SKIP:no_price` row, not a silent gap.
- A kill-switch run that could not sell everything says so instead of
  reporting an empty portfolio.

### Negative

- A holding the strategy wanted out of stays in until the next run, or
  until the operator sells it by hand. That is the honest state; the old
  behaviour only hid it.
- Four functions change shape slightly; the pipeline tests cover each.

### Neutral

- Runs where every price is available are unchanged; the golden test's
  expected files do not move.
- `exits.csv` gains one possible `decision` value.

## Alternatives Considered

### Option 1: Status quo

**Cons:**

- The snapshot understates holdings after a missing price, with no signal
  beyond a WARNING at the trade site.

### Option 2: Abort the run when any sell cannot be priced

**Pros:**

- Nothing half-done.

**Cons:**

- One illiquid or suspended name would block the week's exits and buys for
  every other holding. Too blunt for a weekly strategy.

### Option 3: Retry the sell later in the run, or as a market order

**Cons:**

- Without a price there is no market order the program is willing to book,
  and a retry within the same run hits the same quote. Retrying next week
  is what "the holding stays" already means.

## Implementation Plan

1. **Fix**, one commit: the four call sites, the `SKIP:no_price` decision,
   the kill-switch WARNING; pipeline tests for a missing price in each of
   the four paths, asserting the position survives, no ledger row is
   written and cash is unchanged.
2. **Validation before Implemented.** `uv run pytest`: the new tests pass and
   the golden test's expected files are untouched.

## References

- `src/stocks_on_the_move/momentum.py`: `prune_portfolio`, `resize_positions`,
  `raise_cash_if_needed`, `liquidate_all`, `safe_sell`
- ADR-006 (`exits.csv`, `sizing.csv`), ADR-008 (the skip on a missing price)
- `ONBOARDING.md`, rough edge 3

## Implementation Status

Not started.
