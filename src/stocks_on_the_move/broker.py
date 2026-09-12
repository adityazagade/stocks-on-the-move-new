"""The broker boundary (ADR-008).

``Broker`` is the whole of what the strategy asks of the outside world:
instruments, last prices, quotes with depth, daily candles, orders, and the
profile check the login uses. Results are our own small types, never raw Kite
dicts. ``KiteBroker`` adapts ``KiteConnect`` to the protocol and owns the
request spacing, the jittered backoff on "too many requests" and the retry
limit that ``kite_call`` used to have; after the last retry it raises
``BrokerError`` instead of returning ``None``. ``PaperBroker`` wraps any broker,
delegates every read and turns ``place_order`` into a log line. Tests use
``tests/fakes.FakeBroker``.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Protocol, TypedDict

from kiteconnect.exceptions import NetworkException

logger = logging.getLogger(__name__)

Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]


class BrokerError(RuntimeError):
    """The broker kept refusing after every configured retry."""


@dataclass(frozen=True)
class Instrument:
    token: int
    tradingsymbol: str
    exchange: str
    segment: str
    instrument_type: str


@dataclass(frozen=True)
class Quote:
    last_price: float
    best_bid: float | None  # top of the buy side of the depth, None when the book is empty
    best_ask: float | None


class Candle(TypedDict):
    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Order:
    """What the strategy wants done. Only the adapter knows the broker's own vocabulary."""

    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: float | None = None
    exchange: str = "NSE"

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, not {self.side!r}")
        if self.quantity < 1:
            raise ValueError(f"quantity must be at least 1, not {self.quantity}")
        if self.order_type == "LIMIT" and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("a LIMIT order needs a positive limit_price")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("a MARKET order takes no limit_price")


class Broker(Protocol):
    def instruments(self, exchange: str) -> list[Instrument]: ...

    def ltp(self, keys: Iterable[str]) -> dict[str, float]:
        """``{"NSE:TCS": last_price}`` for every key the broker answered."""
        ...

    def quote(self, keys: Iterable[str]) -> dict[str, Quote]: ...

    def historical_data(self, token: int, from_date: date, to_date: date, interval: str = "day") -> list[Candle]: ...

    def place_order(self, order: Order) -> str:
        """Returns the broker's order id."""
        ...

    def profile(self) -> dict[str, Any]: ...


# ═════════════════════════════════════════════════════════════════════════
# Kite adapter
# ═════════════════════════════════════════════════════════════════════════
def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "too many requests" in msg or "429" in msg


class KiteBroker:
    """``KiteConnect`` behind the ``Broker`` protocol, with spacing, backoff and a retry limit.

    ``min_interval`` seconds separate consecutive requests (plus up to 25 % jitter).
    A "too many requests" answer is retried up to ``max_retries`` times with a
    jittered exponential backoff starting at 0.25 s and capped at 8 s; the last
    failure raises ``BrokerError``. Any other error propagates untouched.
    ``instruments()`` is fetched once per exchange per run.
    """

    def __init__(
        self,
        kite: Any,
        *,
        min_interval: float,
        max_retries: int,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._kite = kite
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._sleep = sleep
        self._monotonic = monotonic
        self._uniform = uniform
        self._last_call: float | None = None
        self._instruments: dict[str, list[Instrument]] = {}

    @property
    def kite(self) -> Any:
        return self._kite

    # -- the wrapper every request goes through ------------------------------
    def _throttle(self) -> None:
        now = self._monotonic()
        if self._last_call is not None:
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                self._sleep(wait + self._uniform(0, self._min_interval * 0.25))
        self._last_call = self._monotonic()

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run one Kite request with spacing and rate-limit retries; raise BrokerError when they run out."""
        delay = 0.25
        last: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            self._throttle()
            try:
                return fn(*args, **kwargs)
            except NetworkException as exc:
                if not _is_rate_limit(exc):
                    raise
                last = exc
                logger.debug("Kite rate limited on attempt %d/%d; backing off %.2fs", attempt, self._max_retries, delay)
                self._sleep(delay + self._uniform(0, delay * 0.3))
                delay = min(delay * 2, 8.0)
        raise BrokerError(
            f"Kite kept answering 'too many requests' through {self._max_retries} attempts: {last}"
        ) from last

    # -- Broker protocol ----------------------------------------------------
    def instruments(self, exchange: str) -> list[Instrument]:
        if exchange not in self._instruments:
            raw = self.call(self._kite.instruments, exchange)
            self._instruments[exchange] = [
                Instrument(
                    token=int(row["instrument_token"]),
                    tradingsymbol=str(row["tradingsymbol"]),
                    exchange=str(row.get("exchange") or exchange),
                    segment=str(row.get("segment") or ""),
                    instrument_type=str(row.get("instrument_type") or ""),
                )
                for row in raw
                if row.get("instrument_token") and row.get("tradingsymbol")
            ]
        return self._instruments[exchange]

    def ltp(self, keys: Iterable[str]) -> dict[str, float]:
        keys = list(keys)
        if not keys:
            return {}
        out: dict[str, float] = {}
        for key, val in self.call(self._kite.ltp, keys).items():
            try:
                out[key] = float(val.get("last_price", 0.0))
            except (AttributeError, TypeError, ValueError):
                continue
        return out

    def quote(self, keys: Iterable[str]) -> dict[str, Quote]:
        keys = list(keys)
        if not keys:
            return {}
        out: dict[str, Quote] = {}
        for key, val in self.call(self._kite.quote, keys).items():
            depth = val.get("depth") or {}
            bids, asks = depth.get("buy") or [], depth.get("sell") or []
            out[key] = Quote(
                last_price=float(val.get("last_price", 0.0)),
                best_bid=float(bids[0]["price"]) if bids else None,
                best_ask=float(asks[0]["price"]) if asks else None,
            )
        return out

    def historical_data(self, token: int, from_date: date, to_date: date, interval: str = "day") -> list[Candle]:
        raw = self.call(self._kite.historical_data, token, from_date, to_date, interval, continuous=False, oi=False)
        return [
            Candle(
                date=row["date"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row.get("volume", 0)),
            )
            for row in raw
        ]

    def place_order(self, order: Order) -> str:
        k = self._kite
        kwargs: dict[str, Any] = {
            "variety": k.VARIETY_REGULAR,
            "exchange": order.exchange,
            "tradingsymbol": order.symbol,
            "transaction_type": k.TRANSACTION_TYPE_BUY if order.side == "BUY" else k.TRANSACTION_TYPE_SELL,
            "quantity": order.quantity,
            "product": k.PRODUCT_CNC,
            "order_type": k.ORDER_TYPE_LIMIT if order.order_type == "LIMIT" else k.ORDER_TYPE_MARKET,
        }
        if order.order_type == "LIMIT":
            kwargs["price"] = order.limit_price
            kwargs["validity"] = k.VALIDITY_DAY
        return str(self.call(k.place_order, **kwargs))

    def profile(self) -> dict[str, Any]:
        return dict(self.call(self._kite.profile))


# ═════════════════════════════════════════════════════════════════════════
# Paper trading
# ═════════════════════════════════════════════════════════════════════════
class PaperBroker:
    """Every read goes to the wrapped broker; ``place_order`` is recorded and logged, never sent."""

    def __init__(self, inner: Broker) -> None:
        self._inner = inner
        self.orders: list[Order] = []

    def instruments(self, exchange: str) -> list[Instrument]:
        return self._inner.instruments(exchange)

    def ltp(self, keys: Iterable[str]) -> dict[str, float]:
        return self._inner.ltp(keys)

    def quote(self, keys: Iterable[str]) -> dict[str, Quote]:
        return self._inner.quote(keys)

    def historical_data(self, token: int, from_date: date, to_date: date, interval: str = "day") -> list[Candle]:
        return self._inner.historical_data(token, from_date, to_date, interval)

    def place_order(self, order: Order) -> str:
        self.orders.append(order)
        order_id = f"PAPER-{len(self.orders):04d}"
        logger.debug("Paper order %s not sent: %s", order_id, order)
        return order_id

    def profile(self) -> dict[str, Any]:
        return self._inner.profile()
