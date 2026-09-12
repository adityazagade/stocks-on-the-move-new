# ADR-022: Trade intents, one executor, bookkeeping in one place, and a plan mode

- **Status**: Proposed
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

The prune, raise-cash, resize, kill-switch and buy steps each run the same
motion in their own loop: decide, call `safe_sell` or `safe_buy`, read the
`Fill`, adjust the position, mark the symbol sold, write the row. ADR-017
changed that bookkeeping in four places and ADR-019 in five; both patches
were the same six lines each time.

There is no value that says what a run intends to do. The decision exists
only as the order it becomes. Paper mode answers "what would this run do"
by simulating fills through `PaperBroker`, and it still appends to the
trades ledger and rewrites `next_portfolio.csv` (rule 5), so a look at
Wednesday's plan on Tuesday evening means pointing four file paths at
scratch copies first. The onboarding guide has a recipe for that; the
recipe is the symptom.

Two things must not change. Cash flows through the run in order: exits
raise it, the raise-cash step covers a withdrawal, resize spends some, the
buys spend the rest, and each buy resizes on the equity after the previous
fill. And a position changes only by what the broker confirmed
(ADR-017, ADR-019).

## Decision

**`TradeIntent`**, a frozen dataclass: symbol, side, quantity, a reason
string (the same values the artifacts already print: `rank_cutoff`,
`resize`, `raise_cash`, `new_position`, `kill_switch`), and the reference
price the decision was made at.

**Each step decides, then executes.** A step is a pure function from the
current portfolio, snapshots, prices and params to a list of intents, and
the pipeline runs every intent through the executor and applies the fill
before the next step decides. The buy step decides one intent at a time,
because each fill changes the equity the next size is computed from. The
order of cash through the run is therefore exactly today's.

**`Executor`**, a protocol with one method, `execute(intent) -> Fill | None`,
and two implementations:

- `BrokerExecutor`: today's `_price_for`, `place_order`, `await_fill` and
  the `orders.csv` row, over any `Broker`. Paper mode is this executor
  over `PaperBroker`, unchanged.
- `PlanExecutor`: returns a `Fill` for the full quantity at the intent's
  reference price, sends nothing, and writes the `orders.csv` row with
  status `PLANNED`.

**`Portfolio.apply(fill, *, exit: bool)`** is the only code that changes a
position: subtract or add the filled quantity, drop the position at zero,
mark the symbol sold when `exit` is set even for a partial fill, and take
the cash delta the ledger returns. The five loops lose their copies of
this. `SELL:partial`, `BUY:partial`, `SKIP:no_fill` and `SKIP:no_price`
keep their meanings; they are derived from the intent and the fill by one
helper.

**Plan mode.** A new setting, `PLAN_ONLY` (default false). When set:

- the weekday guard is skipped, as it is for the kill switch, so the plan
  can be read on any evening;
- the login happens and market data is gathered as in a real run;
- `PlanExecutor` is the executor, and the ledgers and `next_portfolio.csv`
  are not written: the `Ledgers` object is constructed in a read-only form
  that reconstructs cash and refuses to append;
- the run directory is `runs/<date>/<time>-plan/` with every table, and
  the console prints one line per intent at INFO and a closing summary of
  intents by reason;
- `KILL_SWITCH=1` with `PLAN_ONLY=1` plans the liquidation without selling.

`PLAN_ONLY` wins over `ALLOW_KITE_EXECUTION`. `run_mode` gains `plan`.

`CLAUDE.md` rule 5 gains one sentence: `PLAN_ONLY=1` is the mode that
writes nothing. `ONBOARDING.md` section 2 replaces the scratch-ledger
recipe's motivation with plan mode, keeping the recipe for paper runs that
should book.

## Consequences

### Positive

- One bookkeeping path. The next ADR that touches how a position follows a
  fill edits one method and one test.
- A run's decisions exist as values before they are orders, which is what a
  backtest (ADR-023) consumes and what a plan prints.
- "What would Wednesday do" is one environment variable, writes no state,
  and leaves a run directory to read.
- The step functions are testable with a list of intents as the expected
  value, no broker fake needed for the decision half.

### Negative

- Plan mode assumes every intent fills at its reference price. A plan is
  an upper bound on what a run would do when limit orders on `BE` names do
  not fill. Said in the console summary.
- Plan mode still needs the Kite login and the data fetch; it is not
  offline. Offline what-if is the backtest's job.
- One more executor implementation and one more mode to keep in the tests.

### Neutral

- `Fill` and the `orders.csv` schema are unchanged; `PLANNED` joins the
  status values.
- The golden test runs paper mode and does not move. A second golden
  configuration for plan mode is not needed: the decision half is the same
  code.

## Alternatives Considered

### Option 1: Status quo, five loops that decide and trade

**Cons:**

- The same bookkeeping in five places, twice patched already; no plan
  without booking.

### Option 2: Plan the whole run first, then execute the plan

Every step decides against assumed fills; the executor runs the full plan;
a reconciliation step re-plans if fills differed.

**Pros:**

- A complete plan before the first order; parallel waits for fills.

**Cons:**

- Changes the cash semantics ADR-017 and ADR-019 pinned down: a buy sized
  before the exits filled is sized on money that may not arrive. The
  per-step form keeps today's order and still yields a plan. Revisit if the
  run's wall time ever matters.

### Option 3: A command-line flag instead of a setting

**Cons:**

- The console script takes no arguments today; `KILL_SWITCH` and
  `FORCE_RESIZE` are settings, and `.env.example` documents them. Same
  shape for the same kind of switch.

### Option 4: Point the ledgers at scratch paths, as the recipe does

**Cons:**

- Works, and is why nobody runs it on a Tuesday. Four paths to remember and
  a `runs/` directory labelled `paper` that looks like a real run.

## Implementation Plan

1. **Prerequisites**: ADR-020 and ADR-021 at Implemented.
2. **Intents and apply**, one commit: `TradeIntent`, `Portfolio.apply`, the
   five steps returning intents, the pipeline loop, `BrokerExecutor` from
   today's `_place`. `tests/test_fills.py` adapts; the decision half of
   each step gains a test that asserts the intents.
3. **Plan mode**, one commit: `PLAN_ONLY`, `PlanExecutor`, the read-only
   ledgers, the `plan` mode directory, the console summary; tests that a
   plan run appends no ledger row, rewrites no snapshot, and leaves an
   `orders.csv` of `PLANNED` rows. `.env.example`, `CLAUDE.md` rule 5,
   `ONBOARDING.md` section 2.
4. **Validation before Implemented.** Golden files untouched; the suite
   green; `ty` clean; one `PLAN_ONLY=1` run on a weekday evening whose four
   ledger files have the same hash before and after.

## References

- ADR-017, ADR-019 (the bookkeeping this ADR centralises), ADR-020,
  ADR-021 (prerequisites), ADR-023 (the consumer of intents), ADR-014
  (`CLAUDE.md` rule 5 changes with this ADR)
- `src/stocks_on_the_move/momentum.py`: `prune_portfolio`,
  `resize_positions`, `raise_cash_if_needed`, `liquidate_all`, the step-11
  loop, `_place`

## Implementation Status

Proposed; nothing implemented.

## Notes

The intents' reason strings are the decision values the artifacts already
carry, so `exits.csv`, `sizing.csv` and `candidates.csv` do not change
shape; they are built from intents and fills instead of inline.
