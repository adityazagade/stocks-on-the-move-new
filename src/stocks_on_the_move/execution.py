"""From a decision to a booked trade (ADR-008, ADR-019, ADR-020, ADR-022).

A step decides a ``TradeIntent``. ``trade`` hands it to the context's executor,
which answers with what the broker did as a ``Fill`` (or ``None`` when nothing
was sent), and ``book`` writes the ledger row and applies the fill to the
portfolio, the one place a position changes. ``BrokerExecutor`` sends the order
and waits for its verdict; ``PlanExecutor`` sends nothing and reports every
sendable intent filled in full at the price it would have gone out at, which
is plan mode.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from typing import Any, Literal, Protocol

from stocks_on_the_move.broker import Broker, Order, OrderStatus, OrderType, Side
from stocks_on_the_move.context import Fill, RunContext, TradeIntent, strategy_params
from stocks_on_the_move.ledger import record_trade
from stocks_on_the_move.settings import Settings
from stocks_on_the_move.universe import NO_MARKET_SERIES, series_of

logger = logging.getLogger(__name__)


ORDER_COLUMNS = [  # every order this run sent and what the broker did with it (ADR-019)
    "order_id",
    "symbol",
    "side",
    "order_type",
    "limit_price",
    "requested_qty",
    "status",
    "filled_qty",
    "average_price",
    "status_message",
    "polls",
    "waited_s",
]


def ltp_map(broker: Broker, syms: Iterable[str]) -> dict[str, float]:
    """Batch-fetch LTP for NSE symbols. Returns {sym: last_price}."""
    syms = list(syms)
    if not syms:
        return {}
    data = broker.ltp([f"NSE:{s}" for s in syms])
    return {key.split(":", 1)[1]: price for key, price in data.items() if ":" in key}


def live_value(ctx: RunContext) -> float:
    """Mark-to-market portfolio value using LTP (batched)."""
    pf = ctx.portfolio.positions
    if not pf:
        return 0.0
    prices = ltp_map(ctx.broker, pf.keys())
    return float(sum(pf[s] * prices.get(s, 0.0) for s in pf))


def gross_cost_for_buy(settings: Settings, price: float, qty: int) -> float:
    return qty * price * (1.0 + settings.fees_pct + settings.slippage_pct)


def net_proceeds_for_sell(settings: Settings, price: float, qty: int) -> float:
    return qty * price * (1.0 - settings.fees_pct - settings.slippage_pct)


# ── the executor: from an intent to what the broker did ─────────────────
class Executor(Protocol):
    def execute(self, intent: TradeIntent) -> Fill | None:
        """What became of the intent; ``None`` when nothing was sent. Books nothing."""
        ...


def _price_for(ctx: RunContext, sym: str, side: Side) -> tuple[float, OrderType]:
    """The price a trade is booked at, and the order type that implies.

    Series without market orders (BE, BZ, ...) get a LIMIT at the top of the
    opposite side of the book, falling back to the last price when the book is
    empty; everything else is a MARKET order booked at the last traded price.
    """
    if series_of(sym) in NO_MARKET_SERIES:
        key = f"NSE:{sym}"
        q = ctx.broker.quote([key])[key]
        top = q.best_ask if side == "BUY" else q.best_bid
        return (top if top is not None else q.last_price), "LIMIT"
    return ltp_map(ctx.broker, [sym]).get(sym, 0.0), "MARKET"


def _sendable(ctx: RunContext, intent: TradeIntent) -> tuple[float, OrderType] | None:
    """The price and order type the intent would go out at, or ``None`` with the reason logged."""
    sym, side, qty = intent.symbol, intent.side, intent.quantity
    if qty < strategy_params(ctx).min_shares:
        return None
    price_used, order_type = _price_for(ctx, sym, side)
    if price_used <= 0:
        logger.warning("No price for %s; %s x%d skipped", sym, side, qty)
        return None
    if side == "BUY":
        need = gross_cost_for_buy(ctx.settings, price_used, qty)
        if need > ctx.portfolio.cash + 1e-6:
            logger.info("Not enough cash for BUY %s x%d (need %.2f, have %.2f)", sym, qty, need, ctx.portfolio.cash)
            return None
    return price_used, order_type


def await_fill(ctx: RunContext, order_id: str) -> tuple[OrderStatus, int, float]:
    """Poll the broker until the order is terminal, cancelling what has not filled by the timeout (ADR-019).

    Polls every ``FILL_POLL_SECONDS`` for up to ``FILL_TIMEOUT_SECONDS``, counted
    in polls so a test with a no-op ``sleep`` walks the same path. Past the
    timeout the order is cancelled and polled again for as long; a cancel that
    races a fill comes back COMPLETE and is booked as such. Returns the last
    status, the polls made and the seconds asked of ``ctx.sleep``.
    """
    s = ctx.settings
    interval = s.fill_poll_seconds
    budget = max(1, math.ceil(s.fill_timeout_seconds / interval))
    polls = 0
    waited = 0.0

    def poll(*, sleep_first: bool) -> OrderStatus:
        nonlocal polls, waited
        for i in range(budget):
            if i or sleep_first:
                ctx.sleep(interval)
                waited += interval
            status = ctx.broker.order_status(order_id)
            polls += 1
            if status.terminal or i == budget - 1:
                return status
        raise AssertionError("unreachable: the budget is at least one poll")

    status = poll(sleep_first=False)
    if status.terminal:
        return status, polls, waited
    logger.warning(
        "Order %s still %s after %d poll(s) over %.0fs; cancelling what has not filled",
        order_id,
        status.status,
        polls,
        waited,
    )
    ctx.broker.cancel_order(order_id)
    status = poll(sleep_first=True)
    if not status.terminal:
        logger.warning(
            "Order %s neither filled nor cancelled after %d polls; the broker's order book is the truth. "
            "Booking the %d filled so far",
            order_id,
            polls,
            status.filled_quantity,
        )
    return status, polls, waited


def _place(ctx: RunContext, sym: str, side: Side, qty: int, price: float, order_type: OrderType) -> Fill:
    """Send the order, wait for the broker's verdict, return what filled (ADR-019). Books nothing."""
    pf = ctx.portfolio
    limit = price if order_type == "LIMIT" else None
    order_id = ctx.broker.place_order(Order(sym, side, qty, order_type, limit_price=limit))
    row: dict[str, Any] = {
        "order_id": order_id,
        "symbol": sym,
        "side": side,
        "order_type": order_type,
        "limit_price": limit,
        "requested_qty": qty,
        "status": "PLACED",
        "filled_qty": 0,
        "average_price": None,
        "status_message": None,
        "polls": 0,
        "waited_s": 0.0,
    }
    pf.orders.append(row)
    ctx.artifacts.write_table("orders", ORDER_COLUMNS, pf.orders)  # a crash mid-wait still leaves the order id

    status, polls, waited = await_fill(ctx, order_id)
    row.update(
        status=status.status,
        filled_qty=status.filled_quantity,
        average_price=status.average_price,
        status_message=status.status_message,
        polls=polls,
        waited_s=round(waited, 1),
    )
    ctx.artifacts.write_table("orders", ORDER_COLUMNS, pf.orders)

    filled = status.filled_quantity
    if filled <= 0:
        why = f"{status.status}: {status.status_message}" if status.status_message else status.status
        logger.warning("%s %s x%d: nothing filled (%s); nothing booked", side, sym, qty, why)
        return Fill(sym, side, order_id, status.status, qty, 0, 0.0)
    book_price = status.average_price
    if book_price <= 0:  # filled, but no average came back; the price seen is the best record there is
        logger.warning("%s %s: %d filled but no average price came back; booking at %.2f", side, sym, filled, price)
        book_price = price
    if filled < qty:
        logger.warning("%s %s: %d of %d filled (%s); booking the part that did", side, sym, filled, qty, status.status)
    return Fill(sym, side, order_id, status.status, qty, filled, book_price)


class BrokerExecutor:
    """Sends the intent through the context's broker and waits for the verdict (ADR-019).

    Paper mode is this executor over ``PaperBroker``.
    """

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx

    def execute(self, intent: TradeIntent) -> Fill | None:
        sendable = _sendable(self._ctx, intent)
        if sendable is None:
            return None
        price, order_type = sendable
        return _place(self._ctx, intent.symbol, intent.side, intent.quantity, price, order_type)


class PlanExecutor:
    """Sends nothing: every sendable intent is reported filled in full at the price it would go out at (ADR-022)."""

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx

    def execute(self, intent: TradeIntent) -> Fill | None:
        ctx = self._ctx
        sendable = _sendable(ctx, intent)
        if sendable is None:
            return None
        price, order_type = sendable
        pf = ctx.portfolio
        order_id = f"PLAN-{len(pf.orders) + 1:04d}"
        pf.orders.append(
            {
                "order_id": order_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "order_type": order_type,
                "limit_price": price if order_type == "LIMIT" else None,
                "requested_qty": intent.quantity,
                "status": "PLANNED",
                "filled_qty": intent.quantity,
                "average_price": price,
                "status_message": None,
                "polls": 0,
                "waited_s": 0.0,
            }
        )
        ctx.artifacts.write_table("orders", ORDER_COLUMNS, pf.orders)
        return Fill(intent.symbol, intent.side, order_id, "PLANNED", intent.quantity, intent.quantity, price)


def executor_for(ctx: RunContext) -> Executor:
    """The context's executor, else the one its settings imply: the plan executor or the broker."""
    if ctx.executor is not None:
        return ctx.executor
    return PlanExecutor(ctx) if ctx.settings.plan_only else BrokerExecutor(ctx)


# ── booking: the one path from a fill to the ledger and the portfolio ───
Mode = Literal["live", "paper", "plan"]

# Trade lines keep the wording earlier runs logged, so runs can be compared line by line.
_TRADE_LINE: dict[tuple[Mode, Side], str] = {
    ("live", "BUY"): "BUY  %-10s x%4d @ %.2f   (cash → %.2f)",
    ("live", "SELL"): "SELL %-10s x%4d @ %.2f   (cash → %.2f)",
    ("paper", "BUY"): "PAPER BUY %-10s x%4d @ %.2f   (cash → %.2f)",
    ("paper", "SELL"): "PAPER SELL %-9s x%4d @ %.2f   (cash → %.2f)",
    ("plan", "BUY"): "PLAN  BUY %-10s x%4d @ %.2f   (cash → %.2f)",
    ("plan", "SELL"): "PLAN  SELL %-9s x%4d @ %.2f   (cash → %.2f)",
}


def trade_mode(ctx: RunContext) -> Mode:
    if ctx.settings.plan_only:
        return "plan"
    return "paper" if ctx.paper else "live"


def book(ctx: RunContext, fill: Fill, *, exit: bool = False) -> None:
    """Write the ledger row for a fill and apply it to the portfolio (ADR-019, ADR-022)."""
    cash_delta = record_trade(ctx, fill.side, fill.symbol, fill.filled, fill.price)
    ctx.portfolio.apply(fill, cash_delta, exit=exit)
    logger.info(_TRADE_LINE[(trade_mode(ctx), fill.side)], fill.symbol, fill.filled, fill.price, ctx.portfolio.cash)


def trade(ctx: RunContext, intent: TradeIntent, *, exit: bool = False) -> Fill | None:
    """Run one intent through the executor and book what filled: the pipeline's only way to trade.

    ``exit`` marks the symbol sold this run even on a partial fill, so the buy
    step does not buy back into a name the strategy wanted out of.
    """
    fill = executor_for(ctx).execute(intent)
    ctx.portfolio.intents.append((intent, fill))
    if fill is not None and fill.filled > 0:
        book(ctx, fill, exit=exit)
    return fill


def outcome(intent: TradeIntent, fill: Fill | None, *, not_sent: str = "SKIP:not_placed") -> str:
    """The decision value the tables print for what became of an intent."""
    if fill is None:
        return not_sent
    if fill.filled <= 0:
        return "SKIP:no_fill"
    return intent.side if fill.filled == intent.quantity else f"{intent.side}:partial"


def safe_buy(ctx: RunContext, sym: str, qty: int) -> Fill | None:
    """Buy ``qty`` now, outside any step: one intent, executed and booked. ``None`` when nothing was sent."""
    return trade(ctx, TradeIntent(sym, "BUY", qty, "direct", math.nan))


def safe_sell(ctx: RunContext, sym: str, qty: int) -> Fill | None:
    """Sell ``qty`` now, outside any step: one intent, executed and booked. ``None`` when nothing was sent."""
    return trade(ctx, TradeIntent(sym, "SELL", qty, "direct", math.nan))
