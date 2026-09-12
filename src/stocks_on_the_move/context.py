"""The run's context: everything a run needs, assembled once by ``main()`` (ADR-008, ADR-020).

``RunContext`` carries the settings, the broker, the candle store, the clock, the
portfolio, the token cache and the artifacts sink. ``Portfolio`` is the state a
run mutates; ``Fill`` is what the broker did with one order (ADR-019).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from stocks_on_the_move.artifacts import Artifacts, NoArtifacts
from stocks_on_the_move.broker import Broker, Side
from stocks_on_the_move.candles import CandleStore
from stocks_on_the_move.settings import Settings

if TYPE_CHECKING:
    from stocks_on_the_move.universe import UniverseSource

# Timezone: run scheduling and biweekly parity in IST
IST = ZoneInfo("Asia/Kolkata")


def ist_now() -> datetime:
    """Convenience: current time in IST."""
    return datetime.now(IST)


@dataclass(frozen=True)
class Fill:
    """What the broker did with one order (ADR-019).

    ``filled`` is what the account actually gained or lost, and ``price`` the
    broker's average for it. ``filled`` below ``requested`` is a partial fill;
    zero means the order ended without a trade. ``safe_buy`` and ``safe_sell``
    return ``None`` instead when nothing was sent at all.
    """

    symbol: str
    side: Side
    order_id: str
    status: str
    requested: int
    filled: int
    price: float

    @property
    def partial(self) -> bool:
        return 0 < self.filled < self.requested


def filled_qty(fill: Fill | None) -> int:
    """The shares a trade attempt actually moved: zero for nothing sent and for nothing filled."""
    return 0 if fill is None else fill.filled


@dataclass
class Portfolio:
    """Positions, the cash reconstructed from the ledgers, and the names sold this run."""

    positions: dict[str, int] = field(default_factory=dict)
    cash: float = 0.0
    sold: set[str] = field(default_factory=set)
    trades: list[dict[str, Any]] = field(default_factory=list)  # the rows appended to the ledger this run
    orders: list[dict[str, Any]] = field(default_factory=list)  # every order sent this run (ADR-019)


@dataclass
class RunContext:
    """Everything a run needs, assembled once by ``main()`` (ADR-008).

    ``paper`` only labels the trade log lines; paper mode itself is the choice
    of a ``PaperBroker`` as ``broker``. ``universe`` supplies the base symbols
    the strategy may hold; ``None`` means the NSE archives with their last-good
    copy (ADR-020), per settings.
    ``artifacts`` receives every table the run writes (ADR-006); the default
    writes nothing. ``sleep`` is what the wait for a fill sleeps with (ADR-019);
    tests pass a no-op.
    """

    settings: Settings
    broker: Broker
    candles: CandleStore
    now: Callable[[], datetime] = ist_now
    paper: bool = False
    portfolio: Portfolio = field(default_factory=Portfolio)
    tokens: dict[str, int] = field(default_factory=dict)  # "EXCH:SYMBOL" -> instrument_token
    universe: UniverseSource | None = None
    artifacts: Artifacts = field(default_factory=NoArtifacts)
    sleep: Callable[[float], None] = time.sleep


def build_token_cache(ctx: RunContext) -> None:
    """Populate the context's token map from instruments("NSE") once per run."""
    for inst in ctx.broker.instruments("NSE"):
        ctx.tokens[f"NSE:{inst.tradingsymbol}"] = inst.token


def token_of(ctx: RunContext, sym: str | None = None, exch: str | None = None) -> int:
    """Resolve instrument_token for EXCH:SYMBOL (default: the regime index) from the token map."""
    sym = ctx.settings.index_symbol if sym is None else sym
    exch = ctx.settings.index_exchange if exch is None else exch
    key = f"{exch}:{sym}"
    if key in ctx.tokens:
        return ctx.tokens[key]
    # Fallback: scan the instruments of that exchange once if missing
    for inst in ctx.broker.instruments(exch):
        if inst.tradingsymbol == sym:
            ctx.tokens[key] = inst.token
            return inst.token
    raise KeyError(f"Cannot resolve instrument_token for {key}")
