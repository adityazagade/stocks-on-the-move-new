# ADR-019: Confirm fills against the broker before booking a trade

- **Status**: Proposed
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

`_place` in `momentum.py` sends an order and, in the next line, calls
`record_trade` with the price the strategy looked up a moment earlier: the
top of the opposite side of the book for a LIMIT on a `BE`/`BZ` name, the
last traded price for a MARKET order on everything else. The order id the
broker returns is discarded. `safe_buy` and `safe_sell` hand that same
price back, and since ADR-017 every caller treats it as "the trade
happened" and adjusts the position.

Kite accepting an order says only that the request was well formed. What
happens next is decided by the order management system and the exchange,
and it is reported through the order's status, which the program never
reads:

- A LIMIT at the top of the book on an illiquid name can sit `OPEN` all
  session and lapse at the close. The ledger says it filled.
- A regular order placed outside market hours, or on a name under a halt,
  is accepted and then `REJECTED` with a `status_message`. The ledger says
  it filled.
- A MARKET order fills at an `average_price` that is not the last traded
  price the program booked. The ledger is off by the difference on every
  trade, in both directions, and `SLIPPAGE_PCT` is a guess at that error.
- A large order on a thin book can fill in part; `filled_quantity` and
  `pending_quantity` say how much. The ledger and `next_portfolio.csv`
  carry the requested quantity.

Cash is reconstructed from the trades ledger on every run (ADR-004), the
ledger is append-only and never hand-edited (rule 7), and `GET /orders`
returns only the current day's orders, so a phantom trade cannot be
reconciled a week later from the API. The error in cash is permanent. The
error in holdings is caught only if the operator compares
`next_portfolio.csv` with the Kite positions page by hand, which the
onboarding guide describes as the current procedure. Sells run before buys
(steps 7, 8 and 9 before step 11), so a sell that did not fill also
overstates the cash the same run's buys are sized against.

ADR-017 fixed the half of this that needed no broker call, and left this
half as rough edge 1. On 2026-09-12 the owner raised Kite's **Postback
URL**: an HTTPS endpoint, set per app in the developer console, to which
Kite POSTs a JSON payload when an order placed with that app's API key
turns `COMPLETE`, `REJECTED` or `CANCELLED`, or is modified or partly
filled (`UPDATE`). The payload is signed with a SHA-256 checksum of the
order id, the order timestamp and the API secret. The documentation
promises no retry and gives no delivery guarantee.

Kite exposes the same information two other ways. The WebSocket ticker
delivers the identical payload as messages of type `order`, over a
connection the program opens itself, with no public URL; an API key may
hold three such connections. And the REST endpoints `GET /orders` and
`GET /orders/:order_id` return, for every order of the day, its `status`,
`filled_quantity`, `pending_quantity`, `average_price` and
`status_message`, under the general limit of ten requests a second.
`DELETE /orders/regular/:order_id` cancels an open order.

This program is a process that lives for a few minutes on a laptop, once a
week, and places on the order of ten orders.

## Decision

A trade is booked only after the broker reports what filled, at the price
and quantity the broker reports. Fills are confirmed by polling the
broker's order status, one order at a time, inside the call that placed
it.

**Broker protocol (ADR-008).** Two methods and one value type:

- `order_status(order_id) -> OrderStatus`: a frozen dataclass with
  `order_id`, `status` (Kite's spelling: `COMPLETE`, `REJECTED`,
  `CANCELLED`, `OPEN`, and the interim states), `filled_quantity`,
  `pending_quantity`, `average_price` (0 when nothing has filled) and
  `status_message` (`None` when the broker gave none). `COMPLETE`,
  `REJECTED` and `CANCELLED` are terminal.
- `cancel_order(order_id) -> None` for a regular-variety order.
- `KiteBroker` implements the first with `order_history`, taking the latest
  entry, and the second with `cancel_order`, both through `call` so
  spacing and backoff apply. Nothing beyond the named fields is read from
  the response, and nothing beyond them is logged (rule 9).
- `PaperBroker.place_order` remembers, per order, the limit price for a
  LIMIT and the wrapped broker's last price at placement for a MARKET;
  `order_status` reports every paper order `COMPLETE` for the full quantity
  at that price, on the first poll; `cancel_order` is a no-op. A paper run
  therefore books exactly what it books today and waits for nothing.
- `tests/fakes.FakeBroker` does the same by default and lets a test script
  a sequence of statuses per symbol: a fill after N polls, a partial fill,
  a rejection, an order that never fills.

**Waiting for the fill.** `_place` keeps the order id and calls
`await_fill(ctx, order_id)`, which polls `order_status` every
`FILL_POLL_SECONDS` until the status is terminal or `FILL_TIMEOUT_SECONDS`
have elapsed. On timeout it calls `cancel_order`, then polls again until
terminal, for at most the same timeout; a cancel that races a fill comes
back `COMPLETE` and is booked as such. If the order is still not terminal
after that, a WARNING says the broker's order book is the truth and the
program books what `filled_quantity` says so far. The wait is counted in
polls, `ceil(timeout / interval)`, not in wall-clock time, and the sleep is
`RunContext.sleep` (default `time.sleep`), so tests run it with a no-op.

**What comes back.** `_place`, `safe_buy` and `safe_sell` return a `Fill`
or `None`. A `Fill` carries the symbol, side, order id, terminal status,
the quantity requested, the quantity filled and the average price. `None`
means nothing filled: no price was available (as today), not enough cash
(as today), or the order ended `REJECTED` or `CANCELLED` with nothing
filled. A `Fill` whose filled quantity is below the requested one is a
partial fill and is booked as such.

**Booking.** `record_trade` is called with the filled quantity and the
average price. For a fill reported by the exchange the `slippage_pct`
column of the ledger row is `0`: slippage is the estimate of the gap
between the price seen and the price got, and the gap is now known.
`fees_pct` still applies; the exchange's average price excludes brokerage,
STT, exchange charges, GST and stamp duty. Paper fills keep the configured
`SLIPPAGE_PCT`, because a paper price is still an estimate. `ctx.paper`
decides which; ADR-008 made it a label for log lines, and this ADR gives
it that second job. The ledger's columns do not change.

**Positions follow the fill**, at the five call sites ADR-017 touched:

- `prune_portfolio`: full fill pops the holding and marks it sold; partial
  fill leaves the remainder in `positions` and still marks the symbol sold,
  so step 11 does not buy back into a name the strategy wanted out of;
  `exits.csv` reads `SELL`, `SELL:partial` or `SKIP:no_fill`, alongside
  ADR-017's `SKIP:no_price`.
- `resize_positions`: the sell-down and the buy-up move the quantity by
  what filled; `sizing.csv` reads `SELL:partial` or `BUY:partial` when it
  differs from the request, and `SKIP:no_fill` when nothing did.
- `raise_cash_if_needed`: the quantity moves by what filled; the remaining
  need is recomputed from cash, as today.
- `liquidate_all`: a partial fill leaves the remainder; the closing WARNING
  lists remaining quantities, not just names.
- Step 11: the new position is the filled quantity, never the requested
  one; `candidates.csv` reads `BUY`, `BUY:partial` or `SKIP:no_fill`.

**A record of every order.** The run directory (ADR-006) gains
`orders.csv`: order id, symbol, side, order type, limit price, quantity
requested, terminal status, quantity filled, average price, status
message, polls made and seconds waited. It is written after each order
completes, so a crash mid-run leaves the orders so far. The golden test
(ADR-009) adds it to its table list.

**Logging (ADR-015).** INFO for every fill, with requested and filled
quantity and the average price; WARNING for a partial fill, for a
rejection with its `status_message`, and for a timeout with the cancel
that followed. Never the response body.

**Settings (ADR-007).** `FILL_TIMEOUT_SECONDS`, integer, default 120,
range 1 to 900; `FILL_POLL_SECONDS`, default 2.0, range 0.5 to 30. Both in
a new "Order confirmation (ADR-019)" section of `.env.example`. The
defaults bound the worst case at about two minutes per LIMIT that never
fills, against a MARKET order on a Nifty 500 name that is terminal on the
first or second poll.

## Consequences

### Positive

- The ledger and `next_portfolio.csv` record what the account holds and
  what it paid, for every trade, not only the limit orders the rough edge
  named. The manual comparison against the Kite positions page stops being
  a correction step and becomes a check.
- Cash within a run follows real fills: an exit that did not fill no
  longer funds buys that then fail at the broker.
- An order placed outside market hours, or rejected for any other reason,
  books nothing and says why.
- `orders.csv` answers "what did the broker do with order X" three weeks
  later without logging in.

### Negative

- The run waits. Ten MARKET orders add ten to twenty seconds; each LIMIT
  that never fills adds the timeout. On a bad day for `BE` names the run
  is minutes longer. Acceptable for a weekly process; the timeout is a
  setting.
- Two more calls per order (status, sometimes cancel) against the
  ten-a-second limit. Negligible next to the ranking's data fetch.
- Live ledger rows stop carrying `SLIPPAGE_PCT`. Cash after a live trade
  is higher by that fraction than the old booking would have said, which
  is the correction, but it is a visible change in the rows.
- Five call sites, `_place`, both `safe_*` functions and both broker
  adapters change shape; the fake grows a scripting surface. The pipeline
  tests cover each path.

### Neutral

- Paper mode and the golden test book the same numbers as today; the only
  change in the expected files is the new `orders.csv`.
- A `Fill` replaces a bare price as the return of `safe_buy` and
  `safe_sell`; ADR-017's `None` contract is unchanged in meaning.
- `ctx.paper` gains a second job. If a third arrives, a `PaperBroker`
  marker on the fill is the cleaner design; not needed for two.

## Alternatives Considered

### Option 1: Status quo, book at placement

**Pros:**

- No waiting, no new broker calls.

**Cons:**

- Every kind of non-fill becomes a permanent error in cash and a wrong
  holdings snapshot, discovered by hand or not at all.

### Option 2: Kite Postback URL

Register an HTTPS endpoint per app; Kite POSTs the order's terminal
status to it.

**Pros:**

- Kite's documented way to learn a fill asynchronously; no polling.

**Cons:**

- Needs a public HTTPS receiver that is up at the moment Kite posts. For
  a process that lives a few minutes on a laptop that is a tunnel or a
  hosted service, plus a way for the run to read what the receiver got.
- The receiver verifies the checksum with the API secret, so the secret
  lives in a second place (ADR-012).
- No documented retry: a receiver that is down when the fill happens
  never learns of it, and the run needs the polling path anyway as a
  fallback and for the timeout.
- Only orders placed with the app's API key are notified, which is fine
  here, but the payload arrives out of band from the code that placed the
  order and has to be matched back by order id.

### Option 3: WebSocket order updates

Open a Kite ticker connection for the run and read messages of type
`order`, which carry the postback payload.

**Pros:**

- Same events as Option 2 with no public URL and no second copy of the
  secret.

**Cons:**

- Brings a background thread and a connection lifecycle into a program
  that has neither, for the same answer one REST call every two seconds
  gives. A dropped socket still needs the polling path for the truth, and
  the timeout and cancel logic is identical. Worth revisiting only if the
  order count grows to where the wait matters.

### Option 4: Confirm per step, not per order

Place every order of a step, then wait for all of them at once.

**Pros:**

- Waits overlap; the run's worst case is one timeout per step, not per
  order.

**Cons:**

- Inside a step, later cash checks would read cash before earlier fills;
  the five call sites would change shape more, and the ADR-017 contract
  ("adjust after the trade") would have to be rewritten as "adjust after
  the batch". For ten orders a week the saving is under a minute.

### Option 5: Book at placement, reconcile at the next run

**Cons:**

- `GET /orders` returns the day's orders only; a week later there is
  nothing to reconcile against without the trade book or the contract
  notes, and the ledger cannot be corrected by hand (rule 7). The error
  would stand for a week and then still need a human.

### Option 6: Treat a partial fill as no fill

Cancel the remainder and book nothing.

**Cons:**

- The filled shares exist in the account. Booking nothing is the same bug
  with the sign reversed.

## Implementation Plan

1. **Broker layer**, one commit: `OrderStatus`, `order_status` and
   `cancel_order` on the protocol; the `KiteBroker` adapter over
   `order_history` and `cancel_order`; `PaperBroker` fills on first poll;
   `FakeBroker` default fills and scripted statuses. Adapter tests against
   a stub Kite: field mapping, latest-entry selection, missing
   `status_message`, both calls go through `call`.
2. **Strategy layer**, one commit: `Fill`, `await_fill`, `_place` returning
   `Fill | None`, the slippage rule in `record_trade`, the five call sites
   and their new decision values, `orders.csv`, `RunContext.sleep`, the two
   settings, `.env.example` regenerated. `ONBOARDING.md`: the file
   lifecycle table gains `orders.csv`, the step-12 note stops calling the
   comparison a correction, section 6 gains a paragraph on the wait and
   the timeout, rough edge 1 is closed.
3. **Tests**, with step 2: for each of the five paths, a complete fill, a
   partial fill, a rejection and an order that never fills and is
   cancelled, asserting position, ledger row (filled quantity, average
   price, `slippage_pct` 0 live and configured paper) and cash; one test
   that a sell which does not fill reduces the buys that follow; one that
   the paper broker waits zero polls; the golden test with `orders.csv`
   added and its existing expected files unchanged.
4. **Validation before Implemented.** `uv run pytest` and `uv run ty check`
   green. Then the owner's first live run after the change: `orders.csv`
   shows every order terminal, and each ledger row's price equals the
   average price on the Kite order book for that order id.

## References

- `src/stocks_on_the_move/momentum.py`: `_price_for`, `_place`,
  `safe_buy`, `safe_sell`, `record_trade`, `prune_portfolio`,
  `resize_positions`, `raise_cash_if_needed`, `liquidate_all`, step 11
- `src/stocks_on_the_move/broker.py`: `Broker`, `KiteBroker`,
  `PaperBroker`; `tests/fakes.py`: `FakeBroker`
- Kite Connect: orders, https://kite.trade/docs/connect/v3/orders/;
  postbacks, https://kite.trade/docs/connect/v3/postbacks/; WebSocket,
  https://kite.trade/docs/connect/v3/websocket/; limits,
  https://kite.trade/docs/connect/v3/exceptions/ (all read 2026-09-12)
- ADR-004 (cash from the ledger), ADR-006 (`orders.csv` joins the run
  directory), ADR-007 (the two settings), ADR-008 (protocol methods),
  ADR-009 (golden table list), ADR-015 (level policy), ADR-017 (the
  bookkeeping half of the same rough edge)
- `ONBOARDING.md`, rough edge 1

## Implementation Status

Proposed; nothing implemented.

## Notes

The owner's prompt for this ADR was Kite's Postback URL. It is the right
instinct, confirm fills from the broker rather than assume them, in a shape
built for a server that is always listening. This program is not one, so
the decision takes the same facts from the endpoint the program can ask
directly.
