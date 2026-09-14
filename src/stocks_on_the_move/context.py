"""The run's context: everything a run needs, assembled once by ``main()`` (ADR-008, ADR-020).

``RunContext`` carries the settings, the broker, the candle store, the clock, the
portfolio, the token cache and the artifacts sink. ``Portfolio`` is the state a
run mutates, and ``Portfolio.apply`` the one place a position changes (ADR-022);
``TradeIntent`` is what a step wants done; ``Fill`` is what the broker did with
one order (ADR-019).
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
from stocks_on_the_move.candles import CandleSource
from stocks_on_the_move.indicators import Snapshot
from stocks_on_the_move.params import StrategyParams
from stocks_on_the_move.settings import Settings

if TYPE_CHECKING:
    from stocks_on_the_move.execution import Executor
    from stocks_on_the_move.universe import PriceBands, UniverseSource

# Timezone: run scheduling and the rebalance cadence in IST
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


@dataclass(frozen=True)
class TradeIntent:
    """What a step wants done, before any order exists (ADR-022).

    ``reason`` is the step's word for it: ``exit:<rules>``, ``raise_cash``,
    ``resize``, ``new_position``, ``kill_switch``, or ``direct`` for a trade
    outside any step. ``reference_price`` is the price the decision was made at,
    a candle close or a live last price; ``nan`` when the step had none. The
    executor decides the price the order actually goes out at.
    """

    symbol: str
    side: Side
    quantity: int
    reason: str
    reference_price: float


@dataclass
class Portfolio:
    """Positions, the cash reconstructed from the ledgers, and the names sold this run."""

    positions: dict[str, int] = field(default_factory=dict)
    cash: float = 0.0
    sold: set[str] = field(default_factory=set)
    trades: list[dict[str, Any]] = field(default_factory=list)  # the rows appended to the ledger this run
    orders: list[dict[str, Any]] = field(default_factory=list)  # every order sent this run (ADR-019)
    intents: list[tuple[TradeIntent, Fill | None]] = field(default_factory=list)  # every intent and its fate

    def apply(self, fill: Fill, cash_delta: float, *, exit: bool = False) -> None:
        """The one place a position changes (ADR-022): by what filled, never by what was asked.

        A buy adds the filled quantity, a sell removes it and drops the position
        at zero; a position sold to zero, or any sell made as an ``exit``, marks
        the symbol sold this run. ``cash_delta`` is what the ledger booked for
        the fill. A fill of nothing changes nothing.
        """
        if fill.filled <= 0:
            return
        self.cash += cash_delta
        held = self.positions.get(fill.symbol, 0)
        after = held + fill.filled if fill.side == "BUY" else held - fill.filled
        if after > 0:
            self.positions[fill.symbol] = after
        else:
            self.positions.pop(fill.symbol, None)
        if fill.side == "SELL" and (exit or after <= 0):
            self.sold.add(fill.symbol)


@dataclass
class RunContext:
    """Everything a run needs, assembled once by ``main()`` (ADR-008).

    ``paper`` only labels the trade log lines; paper mode itself is the choice
    of a ``PaperBroker`` as ``broker``. ``universe`` supplies the base symbols
    the strategy may hold; ``None`` means the NSE archives with their last-good
    copy (ADR-020), per settings.
    ``artifacts`` receives every table the run writes (ADR-006); the default
    writes nothing. ``sleep`` is what the wait for a fill sleeps with (ADR-019);
    tests pass a no-op. ``snapshots`` is filled once per run by the pipeline's
    gather step (ADR-021); ``params`` overrides the parameters built from the
    settings, for a backtest's variants; ``executor`` overrides the one the
    settings imply, the broker's or the plan's (ADR-022). ``bands`` is the
    price-band lookup the entry rules read (ADR-034): ``None`` means NSE's
    daily list with its last-good copy, which ``run`` resolves once; tests
    and the backtest inject a static lookup.
    """

    settings: Settings
    broker: Broker
    candles: CandleSource
    now: Callable[[], datetime] = ist_now
    paper: bool = False
    portfolio: Portfolio = field(default_factory=Portfolio)
    tokens: dict[str, int] = field(default_factory=dict)  # "EXCH:SYMBOL" -> instrument_token
    universe: UniverseSource | None = None
    artifacts: Artifacts = field(default_factory=NoArtifacts)
    sleep: Callable[[float], None] = time.sleep
    snapshots: dict[str, Snapshot] = field(default_factory=dict)  # symbol -> what the rules read (ADR-021)
    params: StrategyParams | None = None
    executor: Executor | None = None
    bands: PriceBands | None = None  # the price bands (ADR-034); None is NSE's list, resolved by the run


def strategy_params(ctx: RunContext) -> StrategyParams:
    """The run's parameters: the context's override, else the ones its settings imply."""
    return ctx.params if ctx.params is not None else StrategyParams.from_settings(ctx.settings)


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
