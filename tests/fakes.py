"""Test doubles for the broker boundary (ADR-008). No network anywhere."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from stocks_on_the_move.broker import Candle, Instrument, Order, OrderStatus, Quote
from stocks_on_the_move.params import StrategyParams

# Kite hands back datetimes with a fixed +05:30 offset; the CSV cache round-trips the same.
IST_OFFSET = timezone(timedelta(hours=5, minutes=30))


def business_days(end: date, count: int) -> list[date]:
    """The last ``count`` weekdays up to and including ``end`` (or the weekday before it)."""
    days: list[date] = []
    d = end
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def make_candles(
    closes: Sequence[float],
    *,
    end: date,
    volume: int = 1_000_000,
    spread: float = 0.01,
) -> list[Candle]:
    """One daily candle per close, ending on ``end``, high/low a ``spread`` fraction around the close."""
    days = business_days(end, len(closes))
    out: list[Candle] = []
    for d, close in zip(days, closes, strict=True):
        close = float(close)
        out.append(
            Candle(
                date=datetime.combine(d, time.min, tzinfo=IST_OFFSET),
                open=close,
                high=close * (1 + spread),
                low=close * (1 - spread),
                close=close,
                volume=volume,
            )
        )
    return out


# Candles a name needs before every rule will look at it: StrategyParams.history_days, with room to spare.
# Tie it to the parameters rather than a literal, so a change to the lookbacks moves the fixtures with it.
FULL_HISTORY = StrategyParams().history_days + 25


def trending_closes(n: int, *, start: float = 100.0, daily: float = 0.002, seed: int = 0, noise: float = 0.0):
    """Log-linear closes with optional multiplicative noise; positive ``daily`` is an uptrend."""
    rng = np.random.default_rng(seed)
    steps = np.full(n, float(daily))
    if noise:
        steps = steps + rng.normal(0, noise, n)
    return [float(v) for v in start * np.exp(np.cumsum(steps))]


# One scripted answer to ``order_status``: (status, filled quantity, average price[, status message]).
FillStep = tuple[str, int, float] | tuple[str, int, float, str | None]


class FakeBroker:
    """Canned instruments, candles per token, an LTP map and a quote map; records orders.

    Every order fills in full on the first poll, at its limit price or the LTP,
    unless ``script_fills`` scripted the symbol: then each poll consumes one step
    and the last step repeats. A step that is terminal wins over a cancellation,
    which is how a cancel that races a fill is modelled (ADR-019).
    """

    def __init__(
        self,
        *,
        instruments: Iterable[Instrument] = (),
        candles: dict[int, list[Candle]] | None = None,
        ltp: dict[str, float] | None = None,
        quotes: dict[str, Quote] | None = None,
    ) -> None:
        self._instruments = list(instruments)
        self.candles: dict[int, list[Candle]] = dict(candles or {})
        self.ltps: dict[str, float] = dict(ltp or {})
        self.quotes: dict[str, Quote] = dict(quotes or {})
        self.orders: list[Order] = []
        self.calls: list[tuple[str, Any]] = []
        self.fills: dict[str, list[FillStep]] = {}  # symbol -> scripted status per poll; the last repeats
        self._placed: dict[str, Order] = {}
        self._polls: dict[str, int] = {}
        self._cancelled: set[str] = set()
        self._last: dict[str, OrderStatus] = {}

    # -- helpers for tests --------------------------------------------------
    def script_fills(self, symbol: str, *steps: FillStep):
        """What ``order_status`` answers, poll by poll, for the next orders on ``symbol``."""
        self.fills[symbol] = list(steps)
        return self

    def add_equity(self, symbol: str, token: int, closes: Sequence[float], *, end: date, ltp: float | None = None):
        self._instruments.append(Instrument(token, symbol, "NSE", "NSE", "EQ"))
        self.candles[token] = make_candles(closes, end=end)
        self.ltps[f"NSE:{symbol}"] = float(closes[-1]) if ltp is None else ltp
        return self

    # -- Broker protocol ----------------------------------------------------
    def instruments(self, exchange: str) -> list[Instrument]:
        self.calls.append(("instruments", exchange))
        return [i for i in self._instruments if i.exchange == exchange]

    def ltp(self, keys: Iterable[str]) -> dict[str, float]:
        keys = list(keys)
        self.calls.append(("ltp", keys))
        return {k: self.ltps[k] for k in keys if k in self.ltps}

    def quote(self, keys: Iterable[str]) -> dict[str, Quote]:
        keys = list(keys)
        self.calls.append(("quote", keys))
        return {k: self.quotes[k] for k in keys if k in self.quotes}

    def historical_data(self, token: int, from_date: date, to_date: date, interval: str = "day") -> list[Candle]:
        self.calls.append(("historical_data", (token, from_date, to_date, interval)))
        return [c for c in self.candles.get(token, []) if from_date <= c["date"].date() <= to_date]

    def place_order(self, order: Order) -> str:
        self.calls.append(("place_order", order))
        self.orders.append(order)
        order_id = f"FAKE-{len(self.orders):04d}"
        self._placed[order_id] = order
        return order_id

    def order_status(self, order_id: str) -> OrderStatus:
        self.calls.append(("order_status", order_id))
        order = self._placed[order_id]
        poll = self._polls.get(order_id, 0)
        self._polls[order_id] = poll + 1
        script = self.fills.get(order.symbol)
        if script:
            step = script[min(poll, len(script) - 1)]
            message = step[3] if len(step) > 3 else None
            status = OrderStatus(order_id, step[0], step[1], order.quantity - step[1], step[2], message)
        else:
            key = f"{order.exchange}:{order.symbol}"
            price = order.limit_price if order.limit_price is not None else self.ltps.get(key, 0.0)
            status = OrderStatus(order_id, "COMPLETE", order.quantity, 0, price)
        if order_id in self._cancelled and not status.terminal:
            filled = status.filled_quantity
            status = OrderStatus(order_id, "CANCELLED", filled, order.quantity - filled, status.average_price)
        self._last[order_id] = status
        return status

    def cancel_order(self, order_id: str) -> None:
        self.calls.append(("cancel_order", order_id))
        self._cancelled.add(order_id)

    def profile(self) -> dict[str, Any]:
        self.calls.append(("profile", None))
        return {"user_id": "FAKE01"}


# ── a frozen clock ───────────────────────────────────────────────────────
IST = ZoneInfo("Asia/Kolkata")


def _wednesday(even_week: bool) -> datetime:
    d = datetime(2026, 9, 16, 10, 0, tzinfo=IST)  # a Wednesday
    while (d.isocalendar().week % 2 == 0) != even_week:
        d += timedelta(days=7)
    return d


EVEN_WEEK_WEDNESDAY = _wednesday(even_week=True)  # resize runs
ODD_WEEK_WEDNESDAY = _wednesday(even_week=False)  # resize skipped
