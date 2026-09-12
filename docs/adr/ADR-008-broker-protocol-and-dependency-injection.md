# ADR-008: A Broker protocol, a Kite adapter, and injected dependencies

- **Status**: Accepted
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

`KiteConnect` is passed to most functions as `k`, which looks like dependency
injection, but the functions reach past it for module state: `kite_call`
throttles with a module-level timestamp, `candles_df` reads `CACHE_DIR`,
`token_of` reads `TOKEN_CACHE`, `safe_buy` and `safe_sell` read
`allow_kite_execution` and `CASH_BAL`, and every order site spells out eight
`KiteConnect` class constants. A fake passed as `k` still hits the module
globals, so nothing above the pure helpers has a test. `ONBOARDING.md`
section 6 says as much.

Two known defects live in this layer. `kite_call` returns `None` after
exhausting its retries instead of raising, so sustained rate limiting
surfaces as an `AttributeError` downstream (rough edge 2). `candles_df` uses
`datetime.now().date()` from the machine clock while every other date in the
program is IST (rough edge 4).

Paper mode is implemented as `if not allow_kite_execution:` branches inside
the two order functions, duplicating the price lookup and cash check of the
live branches.

ADR-009 needs a fake broker and a frozen clock. ADR-005 needs a place to put
session handling that is not `momentum.py`.

## Decision

Introduce a `broker.py` module and route every external call through it.

- **`Broker` protocol** (`typing.Protocol`) with exactly the operations the
  strategy uses: `instruments(exchange)`, `ltp(keys)`, `quote(keys)`,
  `historical_data(token, from_date, to_date, interval)`,
  `place_order(order)`, `profile()`. Return types are our own small
  `TypedDict`s or dataclasses, not raw Kite dicts, so the strategy is typed at
  the boundary (ADR-010).
- **`Order` dataclass** replaces the eight `KiteConnect` constants at call
  sites: `Order(symbol, side, quantity, order_type, limit_price=None)`. Only
  the adapter knows `VARIETY_REGULAR`, `PRODUCT_CNC`, `VALIDITY_DAY`.
- **`KiteBroker`** wraps a `KiteConnect` and owns what `kite_call` did:
  the request spacing, the jittered backoff on `429`, and the retry limit.
  After the last retry it raises `BrokerError` with the underlying message.
  This replaces the `None` return and is the one intentional behaviour
  change, confined to a failure path.
- **`PaperBroker`** wraps any `Broker`, delegates every read, and turns
  `place_order` into a log line returning a synthetic order id. Paper mode
  becomes a choice of broker in `main()`; the `if not allow_kite_execution`
  branches in `safe_buy` and `safe_sell` are deleted. Prices, cash checks and
  ledger writes follow the same path in both modes, which they already do
  today by duplication.
- **`FakeBroker`** in `tests/fakes.py`: canned instruments, candles keyed by
  token, an LTP map, a quote map, and a list of orders received. No network.
- **`CandleStore(broker, cache_dir, now)`** owns the per-token CSV cache and
  the incremental fetch logic that is `candles_df` today, unchanged. The
  instrument token cache moves into `KiteBroker`.
- **The clock is injected.** `now: Callable[[], datetime]` defaults to
  `ist_now`; everything that asks the time asks it. This makes rough edge 4
  fixable by deleting the `datetime.now()` call, which is done as its own
  one-line ADR after this lands so the behaviour change is not buried here.
- **A `RunContext` dataclass** carries `settings` (ADR-007), `broker`,
  `candles`, `now`, and later `artifacts` (ADR-006). Pipeline functions take
  the context instead of `k`. `CASH_BAL` and `sold_symbols` move onto a
  `Portfolio` object in the context. No module-level mutable state remains.
- `authenticate()` (ADR-005) returns a `KiteBroker`.

## Consequences

### Positive

- `prune_portfolio`, `resize_positions`, `safe_buy`, `safe_sell`,
  `rank_universe` and `main` become testable with `FakeBroker` and a fixed
  clock. ADR-009 becomes possible.
- Rate-limit exhaustion is a `BrokerError` at the call site with a message,
  not an `AttributeError` three functions later.
- Paper mode is one wrapper class instead of branches in two functions.
- `momentum.py` stops importing `kiteconnect`; the strategy reads as
  strategy.

### Negative

- The largest refactor in the project's history: most function signatures
  change. It must land after ADR-007 and be verified by a paper-run
  comparison, not by reading the diff.
- More files and more indirection for a program one person runs weekly.
  `Broker`, `KiteBroker`, `PaperBroker`, `FakeBroker`, `CandleStore`,
  `RunContext` are six names where there was one.
- The `None` to `BrokerError` change can turn a run that used to crash
  obscurely into one that stops early with a clear error. Same outcome,
  better message, but it is a change.

### Neutral

- Throttle and backoff parameters keep their names and defaults, now read
  from settings.
- The candle cache format and location are unchanged.

## Alternatives Considered

### Option 1: Status quo, `KiteConnect` passed as `k`, globals for the rest

**Pros:**

- Works; no refactor.

**Cons:**

- Untestable above the pure helpers; the two defects stay.

### Option 2: Monkeypatch the module in tests instead of restructuring

**Pros:**

- Zero production change.

**Cons:**

- Tests would patch `kite_call`, `candles_df`, `CACHE_DIR`, `CASH_BAL`,
  `allow_kite_execution` and `datetime` per test. Brittle, and the fail-open
  and `None`-return defects remain in production.

### Option 3: Subclass `KiteConnect` and override methods for the fake

**Pros:**

- Fewer new types.

**Cons:**

- Couples tests to the client's internals and still leaves the module
  globals in place.

### Option 4: Protocol only, keep paper mode as branches

**Pros:**

- Smaller change.

**Cons:**

- Keeps the duplicated price and cash logic; paper mode remains untested by
  construction because it is a code path, not a component.

## Implementation Plan

1. **Broker layer**, one commit: `broker.py` with the protocol, `Order`,
   `KiteBroker`, `PaperBroker`, `BrokerError`; `tests/fakes.py`;
   unit tests for the backoff (sleep patched), the raise after retries, and
   `PaperBroker` recording orders.
2. **Candle store and context**, one commit: `CandleStore`, `RunContext`,
   `Portfolio`; `momentum.py` functions take the context; module globals
   removed; `conftest.py` simplified.
3. **Validation before Implemented.** Same day, same cache, same settings:
   a paper run on the last commit before step 2 and on the new build. The
   `Top`, `SELL`, `BUY` and `PAPER` log lines, and the trades written to the
   scratch ledger, must match. If ADR-006 has landed, diff the artifact files
   instead.

## References

- `src/stocks_on_the_move/momentum.py`: `kite_call`, `candles_df`,
  `token_of`, `safe_buy`, `safe_sell`
- `ONBOARDING.md`, section 6 (testing) and rough edges 2 and 4
- ADR-005 (returns a broker), ADR-006 (artifacts on the context),
  ADR-007 (settings on the context), ADR-009 (consumer of the fake),
  ADR-010 (typed boundary)

## Implementation Status

Not started.
