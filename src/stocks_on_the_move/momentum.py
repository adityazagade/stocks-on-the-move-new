#!/usr/bin/env python3
"""
Weekly momentum portfolio for NSE equities, after Andreas Clenow's "Stocks on the Move".

The strategy reads as strategy. Every external call goes through the ``Broker``
protocol in ``broker.py`` (ADR-008); candles come from ``CandleStore`` in
``candles.py``; configuration is the ``Settings`` object from ``settings.py``
(ADR-007); the Kite login lives in ``kite_auth.py`` (ADR-005). ``main()``
assembles those into a ``RunContext`` and hands it to ``run()``, the weekly
routine, numbered step by step in comments. Nothing here holds module-level
mutable state, so the whole pipeline runs against a fake broker in the tests.

Portfolio CSV format: SYMBOL,QUANTITY (no header). Files, all overridable
through the environment:
- current_portfolio.csv   positions going into the run
- next_portfolio.csv      positions after the run
- cash_ledger.csv         date, amount, note   (+ deposit, - withdrawal)
- trades_ledger.csv       timestamp, side, symbol, qty, price, fees_pct, slippage_pct, cash_delta

NOTE: Research code. Real-money deployment should add robust order-state handling
and broker-confirmed fills before writing the trade ledger.
"""

from __future__ import annotations

import csv
import logging
import math
import os
import time
from collections import namedtuple
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from stocks_on_the_move import kite_auth
from stocks_on_the_move.artifacts import Artifacts, NoArtifacts, RunArtifacts
from stocks_on_the_move.broker import Broker, Instrument, KiteBroker, Order, OrderType, PaperBroker, Side
from stocks_on_the_move.candles import CandleStore
from stocks_on_the_move.logging_setup import configure_logging
from stocks_on_the_move.settings import Settings, SettingsError

# ── strategy constants ───────────────────────────────────────────────────
# Fixed by the strategy, not configurable. Every environment knob is a field of
# settings.Settings (ADR-007), reached through the RunContext.
MIN_SHARES = 1

# scoring & filters
MA_PERIOD_200 = 200
MA_FILTER_100: int = 100
LOOKBACK_R21 = 5
LOOKBACK_R63 = 15
LOOKBACK_R126 = 45
REG_LOOKBACK = 90

TRADING_DAYS_YR = 250

# recognised NSE equity series codes
SERIES_CODES: set[str] = {
    "EQ",
    "BE",
    "BL",
    "BZ",
    "BT",
    "IL",
    "IQ",
    "SM",
    "ST",
    "GC",
    "GS",
    # debt / partly-paid / rights etc.
    "PP",
    "RE",
    "WD",
    "N1",
    "N2",
    "N3",
}
NO_MARKET_SERIES = {"BE", "BZ", "BT", "IL", "IQ", "SM", "ST"}

# Per-run artifact tables (ADR-006); the trade columns are also the trades ledger's header
UNIVERSE_COLUMNS = ["symbol", "token", "status", "reason", "last", "ema100", "avg_vol_20", "atr", "atr_pct"]
RANKING_COLUMNS = ["rank", "symbol", "pct_rank", "score", "annual_slope", "r2", "close", "ema100", "held"]
EXIT_COLUMNS = ["symbol", "qty", "rank", "pct_rank", "close", "ema100", "stop_level", "reasons", "decision", "price"]
SIZING_COLUMNS = ["symbol", "qty", "price", "atr", "risk_qty", "cap_qty", "target_qty", "delta", "action"]
CANDIDATE_COLUMNS = ["rank", "symbol", "pct_rank", "decision", "qty", "est_cost", "cash_after"]
TRADE_COLUMNS = ["timestamp", "side", "symbol", "qty", "price", "fees_pct", "slippage_pct", "cash_delta"]

# Timezone: run scheduling and biweekly parity in IST
IST = ZoneInfo("Asia/Kolkata")


def ist_now() -> datetime:
    """Convenience: current time in IST."""
    return datetime.now(IST)


logger = logging.getLogger(__name__)

# ── data structures ──────────────────────────────────────────────────────
RankItem = namedtuple(
    "RankItem",
    [
        "symbol",
        "score",
        "annual_slope",
        "r2",
        "close",
        "ema100",
    ],
)


@dataclass
class Portfolio:
    """Positions, the cash reconstructed from the ledgers, and the names sold this run."""

    positions: dict[str, int] = field(default_factory=dict)
    cash: float = 0.0
    sold: set[str] = field(default_factory=set)
    trades: list[dict[str, Any]] = field(default_factory=list)  # the rows appended to the ledger this run


@dataclass
class RunContext:
    """Everything a run needs, assembled once by ``main()`` (ADR-008).

    ``paper`` only labels the trade log lines; paper mode itself is the choice
    of a ``PaperBroker`` as ``broker``. ``universe`` supplies the base symbols
    the strategy may hold; ``None`` means the NSE archives, per settings.
    ``artifacts`` receives every table the run writes (ADR-006); the default
    writes nothing.
    """

    settings: Settings
    broker: Broker
    candles: CandleStore
    now: Callable[[], datetime] = ist_now
    paper: bool = False
    portfolio: Portfolio = field(default_factory=Portfolio)
    tokens: dict[str, int] = field(default_factory=dict)  # "EXCH:SYMBOL" -> instrument_token
    universe: Callable[[], set[str]] | None = None
    artifacts: Artifacts = field(default_factory=NoArtifacts)


# ═════════════════════════════════════════════════════════════════════════
# I/O helpers
# ═════════════════════════════════════════════════════════════════════════
def read_portfolio(path: str) -> dict[str, int]:
    """Read SYMBOL,QUANTITY rows from CSV into a dict."""
    pf: dict[str, int] = {}
    if os.path.isfile(path):
        with open(path, newline="") as f:
            for sym, qty in csv.reader(f):
                try:
                    pf[sym.strip().upper()] = int(qty)
                except ValueError:
                    logger.warning("Invalid line in %s: %s,%s", path, sym, qty)
    logger.info("Portfolio loaded – %d positions", len(pf))
    return pf


def write_portfolio(path: str, pf: dict[str, int]) -> None:
    """Write the portfolio dict back to CSV (sorted for determinism)."""
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(sorted(pf.items()))
    logger.info("Portfolio written → %s (%d lines)", path, len(pf))


# Ledgers ------------------------------------------------------------------
def _ensure_csv(path: str, header: list[str]) -> None:
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


def _append_row(path: str, row: Sequence[Any]) -> None:
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow(row)


def append_env_cashflow_if_any(ctx: RunContext) -> None:
    """If ENV_CASHFLOW!=0, append a dated row to cash ledger for today."""
    s = ctx.settings
    if abs(s.env_cashflow) < 1e-9:
        return
    _ensure_csv(s.cash_ledger_file, ["date", "amount", "note"])
    _append_row(s.cash_ledger_file, [ctx.now().date().isoformat(), f"{s.env_cashflow:.2f}", s.cashflow_note])
    logger.info("Applied ENV_CASHFLOW: %+,.2f (%s)", s.env_cashflow, s.cashflow_note)


def cash_from_cash_ledger(path: str) -> float:
    """Sum deposits/withdrawals from cash ledger."""
    if not os.path.isfile(path):
        return 0.0
    total = 0.0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                total += float(row.get("amount", "0").strip())
            except Exception:
                continue
    return total


def trades_cash_delta(path: str) -> float:
    """Sum cash impact from the trades ledger (already net of fees/slippage)."""
    if not os.path.isfile(path):
        return 0.0
    total = 0.0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                total += float(row.get("cash_delta", "0").strip())
            except Exception:
                continue
    return total


def init_cash_balance(ctx: RunContext) -> float:
    """Reconstruct the cash for this run from the ledgers and store it on the portfolio."""
    s = ctx.settings
    _ensure_csv(s.cash_ledger_file, ["date", "amount", "note"])
    _ensure_csv(s.trades_ledger_file, TRADE_COLUMNS)
    append_env_cashflow_if_any(ctx)
    ledger = cash_from_cash_ledger(s.cash_ledger_file)
    trades = trades_cash_delta(s.trades_ledger_file)
    ctx.portfolio.cash = s.starting_cash + ledger + trades
    logger.info(
        "Cash reconstructed: START=%.2f, ledger=%.2f, trades=%.2f → CASH=%.2f",
        s.starting_cash,
        ledger,
        trades,
        ctx.portfolio.cash,
    )
    return ctx.portfolio.cash


def record_trade(ctx: RunContext, side: str, symbol: str, qty: int, price: float) -> float:
    """Write a trade to the trades ledger and update the portfolio's cash.

    Returns the cash_delta applied (positive if cash increases).
    """
    s = ctx.settings
    if qty <= 0 or price <= 0:
        return 0.0
    side = side.upper()
    # Approximate all-in price impact
    if side == "BUY":
        cash_delta = -qty * price * (1.0 + s.fees_pct + s.slippage_pct)
    elif side == "SELL":
        cash_delta = +qty * price * (1.0 - s.fees_pct - s.slippage_pct)
    else:
        raise ValueError("side must be BUY or SELL")
    values = [
        ctx.now().isoformat(timespec="seconds"),
        side,
        symbol.upper(),
        qty,
        f"{price:.4f}",
        f"{s.fees_pct:.6f}",
        f"{s.slippage_pct:.6f}",
        f"{cash_delta:.2f}",
    ]
    _append_row(s.trades_ledger_file, values)
    ctx.portfolio.trades.append(dict(zip(TRADE_COLUMNS, values, strict=True)))
    ctx.artifacts.write_table("trades", TRADE_COLUMNS, ctx.portfolio.trades)
    ctx.portfolio.cash += cash_delta
    return cash_delta


# ═════════════════════════════════════════════════════════════════════════
# NSE helpers
# ═════════════════════════════════════════════════════════════════════════
def fetch_index_constituents(index: str, retries: int = 3) -> list[str]:
    """Fetch current constituents for a given NSE index from NSE archives.

    Returns upper-cased list of symbols; empty list if all retries fail.
    """
    url = f"https://archives.nseindia.com/content/indices/ind_{index.lower().replace(' ', '')}list.csv"
    for attempt in range(1, retries + 1):
        try:
            df = pd.read_csv(url)
            return df["Symbol"].str.upper().tolist()
        except Exception as exc:
            logger.warning("%s fetch failed (%s) – attempt %d/%d", index, exc, attempt, retries)
            time.sleep(1)
    logger.error("Giving up – empty universe filter")
    return []


def fetch_nifty_constituents(retries: int = 3) -> list[str]:
    """Fetch full NIFTY constituents list from NSE archives."""
    # https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
    url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    for _attempt in range(1, retries + 1):
        try:
            df = pd.read_csv(url)
            return df["SYMBOL"].str.upper().tolist()
        except Exception as exc:
            logger.error("NIFTY full constituents fetch failed (%s) – empty universe", exc)
            time.sleep(1)
    logger.error("Giving up – empty universe")
    return []


def nse_universe_symbols(settings: Settings) -> set[str]:
    """Base symbols the strategy may hold: the NIFTY 500 constituents, or every NSE equity."""
    if settings.use_full_nifty_universe:
        symbols = set(fetch_nifty_constituents())
        logger.info("Using full NIFTY universe: %d symbols", len(symbols))
        return symbols
    return set(fetch_index_constituents("NIFTY 500"))


# ═════════════════════════════════════════════════════════════════════════
# Kite helpers
# ═════════════════════════════════════════════════════════════════════════
def authenticate(settings: Settings) -> KiteBroker:
    """A Kite session (cached, captured from the redirect, or pasted; ADR-005) behind the Broker protocol."""
    auth = kite_auth.AuthSettings(
        session_file=settings.kite_session_file,
        redirect_port=settings.kite_redirect_port,
        open_browser=settings.kite_open_browser,
        forget_session=settings.kite_forget_session,
    )
    kite = kite_auth.authenticate(
        settings.kite_api_key.get_secret_value(),
        settings.kite_api_secret.get_secret_value(),
        settings=auth,
    )
    return KiteBroker(kite, min_interval=settings.kite_min_interval, max_retries=settings.kite_max_retries)


# ═════════════════════════════════════════════════════════════════════════
# Market-data utilities
# ═════════════════════════════════════════════════════════════════════════
def atr(df: pd.DataFrame, period: int) -> float:
    """Average True Range over *period* (simple mean), guarded for tiny frames."""
    if df.empty:
        return float("nan")
    h, lo, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - lo).abs(), (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
    tr = tr.iloc[1:]  # drop first NaN due to shift
    if tr.empty:
        return float("nan")
    n = min(period, len(tr))
    return float(tr.tail(n).mean())


def ltp_map(broker: Broker, syms: Iterable[str]) -> dict[str, float]:
    """Batch-fetch LTP for NSE symbols. Returns {sym: last_price}."""
    syms = list(syms)
    if not syms:
        return {}
    data = broker.ltp([f"NSE:{s}" for s in syms])
    return {key.split(":", 1)[1]: price for key, price in data.items() if ":" in key}


# ═════════════════════════════════════════════════════════════════════════
# Strategy building blocks
# ═════════════════════════════════════════════════════════════════════════
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


# ── 1 ▸ index regime ─────────────────────────────────────────────────────
def index_trend(ctx: RunContext) -> tuple[bool, float, float]:
    """Return (is_bull, last_close, ema200) for the chosen index."""
    tok = token_of(ctx)
    closes = ctx.candles.get(tok, MA_PERIOD_200)["close"]
    if len(closes) < MA_PERIOD_200:
        raise ValueError("not enough index candles for EMA-200")
    ema200 = pd.Series(closes).ewm(span=MA_PERIOD_200, adjust=False).mean().iloc[-1]
    last = float(closes.iloc[-1])
    return last > float(ema200), float(last), float(ema200)


# ── 2 ▸ ranking  ─────────────────────────────────────────────────────────
def annualise(slope_day: float) -> float:
    """Convert daily log-price slope to annualised simple return."""
    return math.exp(slope_day * TRADING_DAYS_YR) - 1.0


def _composite_momentum(closes: pd.Series):
    """Return (score, annual_slope, r2) or (nan, nan, nan) if insufficient data.

    score = (R21 + R63 + R126) × R²(90d)
    """
    need = max(LOOKBACK_R126, REG_LOOKBACK) + 1
    if len(closes) < need:
        return math.nan, math.nan, math.nan

    last = float(closes.iloc[-1])
    r21 = (last / float(closes.iloc[-(LOOKBACK_R21 + 1)])) - 1.0
    r63 = (last / float(closes.iloc[-(LOOKBACK_R63 + 1)])) - 1.0
    r126 = (last / float(closes.iloc[-(LOOKBACK_R126 + 1)])) - 1.0
    comp = 0.6 * r21 + 0.3 * r63 + 0.1 * r126

    y = np.log(closes.iloc[-REG_LOOKBACK:])
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = intercept + slope * x
    ss_res = float(((y - y_hat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0

    return comp * r2, annualise(float(slope)), float(r2)


MIN_HISTORY = max(MA_FILTER_100, LOOKBACK_R126 + 1, REG_LOOKBACK + 1)


@dataclass(frozen=True)
class Evaluation:
    """Why an instrument was ranked or excluded, with the metrics known at that point (universe.csv)."""

    symbol: str
    token: int
    rank: RankItem | None = None
    reason: str | None = None
    last: float | None = None
    ema100: float | None = None
    avg_vol_20: float | None = None
    atr: float | None = None
    atr_pct: float | None = None

    def row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "token": self.token,
            "status": "ranked" if self.rank is not None else "excluded",
            "reason": self.reason,
            "last": self.last,
            "ema100": self.ema100,
            "avg_vol_20": self.avg_vol_20,
            "atr": self.atr,
            "atr_pct": self.atr_pct,
        }


def evaluate_instrument(ctx: RunContext, inst: Instrument) -> Evaluation:
    """Run the filter chain on one instrument and name the rule that stopped it, if any.

    Order of the rules, unchanged: enough history, close above the 100-EMA,
    20-day volume, ATR as a fraction of price, then the momentum score. Anything
    that throws is an ``error:<type>`` exclusion, as it was a silent skip before.
    """
    s = ctx.settings
    sym, tok = inst.tradingsymbol, inst.token
    last: float | None = None
    ema100: float | None = None
    avg_vol_20: float | None = None
    atr_value: float | None = None
    atr_pct: float | None = None

    def verdict(*, rank: RankItem | None = None, reason: str | None = None) -> Evaluation:
        return Evaluation(sym, tok, rank, reason, last, ema100, avg_vol_20, atr_value, atr_pct)

    try:
        df = ctx.candles.get(tok, MIN_HISTORY)
        if df.empty or len(df) < MIN_HISTORY:
            return verdict(reason="history")
        closes, vols = df["close"], df["volume"]
        ema100 = float(pd.Series(closes).ewm(span=MA_FILTER_100, adjust=False).mean().iloc[-1])
        last = float(closes.iloc[-1])
        if last <= ema100:
            return verdict(reason="below_ema100")
        avg_vol_20 = float(vols.iloc[-20:].mean())
        if avg_vol_20 < s.min_volume:
            return verdict(reason="volume")
        atr_value = atr(df, s.atr_period)
        atr_pct = atr_value / last if last > 0 else math.nan
        if math.isnan(atr_value) or (last > 0 and atr_value / last > s.max_atr_pct):
            return verdict(reason="atr_pct")
        score, ann_slope, r2 = _composite_momentum(closes)
        if math.isnan(score):
            logger.warning("Not enough data to rank for %s", sym)
            return verdict(reason="insufficient_data")
        return verdict(rank=RankItem(sym, float(score), float(ann_slope), float(r2), last, ema100))
    except Exception as exc:
        logger.warning("%s skipped – %s: %s", sym, type(exc).__name__, exc)
        return verdict(reason=f"error:{type(exc).__name__}")


def rank_universe(ctx: RunContext, universe: Iterable[Instrument]) -> list[RankItem]:
    """Rank eligible instruments in *universe* by composite momentum score; every verdict goes to universe.csv."""
    evaluations = [evaluate_instrument(ctx, inst) for inst in universe]
    ctx.artifacts.write_table("universe", UNIVERSE_COLUMNS, [e.row() for e in evaluations])
    ranks = [e.rank for e in evaluations if e.rank is not None]
    logger.info("Ranked universe: %d symbols", len(ranks))
    return sorted(ranks, key=lambda r: r.score, reverse=True)


# ── 3 ▸ ATR position size ────────────────────────────────────────────────
@dataclass(frozen=True)
class Sizing:
    """The ATR position size and the two quantities it was the smaller of (sizing.csv)."""

    price: float
    atr: float
    risk_qty: float
    cap_qty: float
    target_qty: int


def size_position(ctx: RunContext, sym: str, account_equity: float) -> Sizing:
    """ATR-based risk parity sizing with MAX_WEIGHT cap on dynamic equity.

    risk_qty = account_equity × RISK_FACTOR / ATR; cap_qty = account_equity × MAX_WEIGHT / price;
    target_qty = floor(min(risk_qty, cap_qty)), never negative.
    """
    s = ctx.settings
    tok = token_of(ctx, sym, "NSE")
    df = ctx.candles.get(tok, s.atr_period)
    if len(df) <= s.atr_period:
        raise ValueError("Not enough candles for ATR")
    atr_value = atr(df, s.atr_period)
    if not atr_value or atr_value <= 0:
        raise ValueError("ATR zero")
    risk_qty = (account_equity * s.risk_factor) / atr_value
    price = float(df["close"].iloc[-1])
    cap_qty = (account_equity * s.max_weight) / price if price > 0 else 0.0
    return Sizing(price, atr_value, risk_qty, cap_qty, max(math.floor(min(risk_qty, cap_qty)), 0))


def target_shares(ctx: RunContext, sym: str, account_equity: float) -> int:
    return size_position(ctx, sym, account_equity).target_qty


def gross_cost_for_buy(settings: Settings, price: float, qty: int) -> float:
    return qty * price * (1.0 + settings.fees_pct + settings.slippage_pct)


def net_proceeds_for_sell(settings: Settings, price: float, qty: int) -> float:
    return qty * price * (1.0 - settings.fees_pct - settings.slippage_pct)


# ── 4 ▸ order wrappers (cash-aware) ──────────────────────────────────────
# Trade lines keep the wording earlier runs logged, so runs can be compared line by line.
_TRADE_LINE = {
    (False, "BUY"): "BUY  %-10s x%4d @ %.2f   (cash → %.2f)",
    (False, "SELL"): "SELL %-10s x%4d @ %.2f   (cash → %.2f)",
    (True, "BUY"): "PAPER BUY %-10s x%4d @ %.2f   (cash → %.2f)",
    (True, "SELL"): "PAPER SELL %-9s x%4d @ %.2f   (cash → %.2f)",
}


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


def _place(ctx: RunContext, sym: str, side: Side, qty: int, price: float, order_type: OrderType) -> None:
    limit = price if order_type == "LIMIT" else None
    ctx.broker.place_order(Order(sym, side, qty, order_type, limit_price=limit))
    record_trade(ctx, side, sym, qty, price)
    logger.info(_TRADE_LINE[(ctx.paper, side)], sym, qty, price, ctx.portfolio.cash)


def safe_buy(ctx: RunContext, sym: str, qty: int) -> float | None:
    """Place a BUY order and record its cash impact; returns the price used if placed."""
    if qty < MIN_SHARES:
        return None
    price_used, order_type = _price_for(ctx, sym, "BUY")
    if price_used <= 0:
        logger.warning("No price for %s; BUY x%d skipped", sym, qty)
        return None
    need = gross_cost_for_buy(ctx.settings, price_used, qty)
    if need > ctx.portfolio.cash + 1e-6:
        logger.info("Not enough cash for BUY %s x%d (need %.2f, have %.2f)", sym, qty, need, ctx.portfolio.cash)
        return None
    _place(ctx, sym, "BUY", qty, price_used, order_type)
    return price_used


def safe_sell(ctx: RunContext, sym: str, qty: int) -> float | None:
    """Place a SELL order and record its cash impact; returns the price used if placed."""
    if qty < MIN_SHARES:
        return None
    price_used, order_type = _price_for(ctx, sym, "SELL")
    if price_used <= 0:
        logger.warning("No price for %s; SELL x%d skipped", sym, qty)
        return None
    _place(ctx, sym, "SELL", qty, price_used, order_type)
    return price_used


# ── 5 ▸ exit & size-rebalance ────────────────────────────────────────────
def _trailing_stop(ctx: RunContext, sym: str) -> tuple[bool, float | None]:
    """(hit, stop level) for the n×ATR trailing stop under the rolling high close."""
    s = ctx.settings
    tok = token_of(ctx, sym, "NSE")
    look = max(s.atr_period, LOOKBACK_R126)
    df = ctx.candles.get(tok, look)
    if len(df) < s.atr_period + 1:
        logger.warning("Not enough candles for trailing stop")
        return False, None
    last = float(df["close"].iloc[-1])
    _atr = atr(df, s.atr_period)
    if math.isnan(_atr):
        return False, None
    window = max(s.atr_period, 2 * s.atr_period)
    highest = float(df["close"].rolling(window).max().iloc[-1])
    stop_level = highest - s.exit_multiple * _atr
    return last < stop_level, stop_level


def _trailing_stop_hit(ctx: RunContext, sym: str) -> bool:
    return _trailing_stop(ctx, sym)[0]


@dataclass(frozen=True)
class ExitCheck:
    """Which exit rules fired for a holding (exits.csv); ``sell`` when any did."""

    reasons: tuple[str, ...]
    stop_level: float | None = None

    @property
    def sell(self) -> bool:
        return bool(self.reasons)


def exit_reasons(ctx: RunContext, rank: RankItem | None, pct_rank: float) -> ExitCheck:
    """Every exit rule, evaluated: ``unranked``, ``rank_cutoff``, ``below_ema100``, ``trailing_stop``.

    All rules are checked so the artifact shows every reason; the decision is the
    same OR of conditions as before. The trailing stop reads candles the ranking
    step has already cached this run, so it costs no broker call.
    """
    if rank is None:
        return ExitCheck(("unranked",))
    reasons = []
    if pct_rank > ctx.settings.cut_off_pct:
        reasons.append("rank_cutoff")
    if rank.close <= rank.ema100:
        reasons.append("below_ema100")
    hit, stop_level = _trailing_stop(ctx, rank.symbol)
    if hit:
        reasons.append("trailing_stop")
    return ExitCheck(tuple(reasons), stop_level)


def rank_says_exit(settings: Settings, rank: RankItem | None, pct_rank: float) -> bool:
    """The exit rules that need no market data: unranked, below the cut-off, or under the 100-EMA."""
    return rank is None or pct_rank > settings.cut_off_pct or rank.close <= rank.ema100


def should_exit(ctx: RunContext, rank: RankItem | None, pct_rank: float) -> bool:
    """Exit conditions: rank drop, below 100-EMA, or trailing stop."""
    return exit_reasons(ctx, rank, pct_rank).sell


def prune_portfolio(ctx: RunContext, ranks: list[RankItem]) -> None:
    """Sell names that violate exit rules; every holding's verdict goes to exits.csv. Guarded against empty ranking."""
    if not ranks:
        logger.warning("Ranking empty – skipping prune to avoid accidental liquidation")
        ctx.artifacts.write_table("exits", EXIT_COLUMNS, [])
        return

    pf = ctx.portfolio
    idx = {r.symbol: i for i, r in enumerate(ranks)}
    rmap = {r.symbol: r for r in ranks}
    total = len(ranks)
    rows: list[dict[str, Any]] = []
    for sym, qty in list(pf.positions.items()):
        rank = rmap.get(sym)
        pct = (idx[sym] + 1) / total if sym in idx else 1.0
        check = exit_reasons(ctx, rank, pct)
        price = None
        if check.sell:
            price = safe_sell(ctx, sym, qty)
            pf.positions.pop(sym)
            pf.sold.add(sym)
        rows.append(
            {
                "symbol": sym,
                "qty": qty,
                "rank": idx[sym] + 1 if sym in idx else None,
                "pct_rank": pct,
                "close": rank.close if rank else None,
                "ema100": rank.ema100 if rank else None,
                "stop_level": check.stop_level,
                "reasons": ";".join(check.reasons),
                "decision": "SELL" if check.sell else "HOLD",
                "price": price,
            }
        )
    ctx.artifacts.write_table("exits", EXIT_COLUMNS, rows)


def live_value(ctx: RunContext) -> float:
    """Mark-to-market portfolio value using LTP (batched)."""
    pf = ctx.portfolio.positions
    if not pf:
        return 0.0
    prices = ltp_map(ctx.broker, pf.keys())
    return float(sum(pf[s] * prices.get(s, 0.0) for s in pf))


def resize_positions(ctx: RunContext, bull: bool) -> None:
    """Every even ISO week (IST), rebalance sizes toward ATR targets (cash-aware); verdicts go to sizing.csv."""
    pf = ctx.portfolio
    if (ctx.now().isocalendar().week % 2) and not ctx.settings.force_resize:  # odd ISO week → skip
        logger.info("Size rebalance skipped (odd week, IST)")
        ctx.artifacts.write_table("sizing", SIZING_COLUMNS, [])
        ctx.artifacts.record(resize_performed=False)
        return
    ctx.artifacts.record(resize_performed=True)

    account_equity = pf.cash + live_value(ctx)

    rows: dict[str, dict[str, Any]] = {}
    to_up, to_down = {}, {}
    for sym, qty in pf.positions.items():
        try:
            size = size_position(ctx, sym, account_equity)
        except Exception as exc:
            logger.warning("size calc error %s – %s: %s", sym, type(exc).__name__, exc)
            rows[sym] = {"symbol": sym, "qty": qty, "action": "SKIP:size_error"}
            continue
        diff = size.target_qty - qty
        rows[sym] = {
            "symbol": sym,
            "qty": qty,
            "price": size.price,
            "atr": size.atr,
            "risk_qty": size.risk_qty,
            "cap_qty": size.cap_qty,
            "target_qty": size.target_qty,
            "delta": diff,
            "action": "BUY" if diff > 0 else "SELL" if diff < 0 else "HOLD",
        }
        if diff > 0:
            to_up[sym] = diff
        elif diff < 0:
            to_down[sym] = -diff

    # 1️⃣ sell downs first
    for sym, delta in to_down.items():
        if safe_sell(ctx, sym, delta) is None:
            rows[sym]["action"] = "SKIP:not_placed"
        pf.positions[sym] -= delta
        if pf.positions[sym] == 0:
            pf.positions.pop(sym)
            pf.sold.add(sym)

    if not bull or pf.cash <= 0:
        for sym in to_up:
            rows[sym]["action"] = "SKIP:bear" if not bull else "SKIP:no_cash"
        ctx.artifacts.write_table("sizing", SIZING_COLUMNS, rows.values())
        return

    prices = ltp_map(ctx.broker, to_up.keys()) if to_up else {}
    for sym, delta in to_up.items():
        price = prices.get(sym, 0.0)
        need = gross_cost_for_buy(ctx.settings, price, delta)
        if need > pf.cash + 1e-6:
            rows[sym]["action"] = "SKIP:no_cash"
            continue
        if safe_buy(ctx, sym, delta) is not None:
            pf.positions[sym] = pf.positions.get(sym, 0) + delta
        else:
            rows[sym]["action"] = "SKIP:not_placed"
    ctx.artifacts.write_table("sizing", SIZING_COLUMNS, rows.values())


def series_of(ts: str) -> str:
    """Extract recognised 2-char NSE series code from a tradingsymbol."""
    ts = ts.upper()
    if "-" in ts:
        _, maybe_series = ts.rsplit("-", 1)
        if maybe_series in SERIES_CODES:
            return maybe_series
    return "EQ"


def base_symbol(ts: str) -> str:
    """Remove trailing '-XX' only when XX is a known NSE series code."""
    ts = ts.upper()
    if "-" in ts:
        root, maybe_series = ts.rsplit("-", 1)
        if maybe_series in SERIES_CODES:
            return root
    return ts


def get_universe(ctx: RunContext, symbols: set[str]) -> list[Instrument]:
    """Universe = NSE equity instruments whose base symbols are in *symbols*."""
    return [
        i
        for i in ctx.broker.instruments("NSE")
        if i.instrument_type == "EQ" and i.segment == "NSE" and base_symbol(i.tradingsymbol) in symbols
    ]


# ── 6 ▸ cash management for withdrawals ──────────────────────────────────
def liquidate_all(ctx: RunContext) -> None:
    """KILL SWITCH: sell every position in the portfolio."""
    pf = ctx.portfolio
    logger.warning("KILL SWITCH activated – liquidating all %d positions", len(pf.positions))
    for sym, qty in list(pf.positions.items()):
        safe_sell(ctx, sym, qty)
        pf.positions.pop(sym)
    logger.warning("KILL SWITCH complete – portfolio empty, cash: %.2f", pf.cash)


def raise_cash_if_needed(ctx: RunContext, ranks: list[RankItem]) -> None:
    """If cash < 0 (withdrawal > cash), sell worst-ranked holdings to cover."""
    pf = ctx.portfolio
    if pf.cash >= 0:
        return
    need = -pf.cash + 1e-6

    idx = {r.symbol: i for i, r in enumerate(ranks)}

    def sort_key(sym: str) -> int:
        return idx.get(sym, 10**9)

    hold_syms = sorted(pf.positions.keys(), key=sort_key, reverse=True)
    prices = ltp_map(ctx.broker, hold_syms) if hold_syms else {}

    for sym in hold_syms:
        if need <= 0:
            break
        qty = pf.positions[sym]
        price = prices.get(sym, 0.0)
        if price <= 0:
            continue
        per_share = net_proceeds_for_sell(ctx.settings, price, 1)
        sell_qty = min(qty, int(math.ceil(need / per_share)))
        if sell_qty <= 0:
            continue
        safe_sell(ctx, sym, sell_qty)
        pf.positions[sym] -= sell_qty
        if pf.positions[sym] == 0:
            pf.positions.pop(sym)
            pf.sold.add(sym)
        need = -pf.cash  # update remaining need after cash change

    if pf.cash < 0:
        logger.warning("Could not fully raise cash for withdrawal. Short by %.2f", -pf.cash)
    else:
        logger.info("Raised cash for withdrawal. Cash now: %.2f", pf.cash)


# ═════════════════════════════════════════════════════════════════════════
# Main weekly routine
# ═════════════════════════════════════════════════════════════════════════
def _finish(ctx: RunContext, status: str, equity_after: float) -> None:
    """The closing artifacts: the portfolio after, this run's trades, the closing numbers, the status."""
    pf = ctx.portfolio
    ctx.artifacts.write_rows("portfolio_after", sorted(pf.positions.items()))
    ctx.artifacts.write_table("trades", TRADE_COLUMNS, pf.trades)
    ctx.artifacts.record(
        cash_after=pf.cash, equity_after=equity_after, positions_after=dict(sorted(pf.positions.items()))
    )
    ctx.artifacts.finish(status)


def run(ctx: RunContext) -> None:
    """Steps 2 to 12 of the weekly routine, against an assembled context."""
    s = ctx.settings
    pf = ctx.portfolio
    art = ctx.artifacts

    # 2) Load current portfolio and ledgers
    pf.positions = read_portfolio(s.portfolio_file)
    init_cash_balance(ctx)
    art.write_rows("portfolio_before", sorted(pf.positions.items()))

    # 3) Preload instrument tokens once (no quote() here)
    build_token_cache(ctx)
    value_before = live_value(ctx)
    logger.info("Portfolio value at start: %.2f", value_before)
    art.record(
        cash_before=pf.cash,
        equity_before=pf.cash + value_before,
        positions_before=dict(sorted(pf.positions.items())),
    )

    # 3.5) KILL SWITCH – liquidate everything and exit
    if s.kill_switch:
        liquidate_all(ctx)
        write_portfolio(s.out_file, pf.positions)
        logger.warning("KILL SWITCH run finished. Final cash: %.2f", pf.cash)
        _finish(ctx, "completed", equity_after=pf.cash)
        return

    # 4) The symbols the strategy may hold (NSE archives unless the context says otherwise)
    symbols = ctx.universe() if ctx.universe is not None else nse_universe_symbols(s)
    logger.info("Universe symbols loaded: %d", len(symbols))
    art.record(universe_size=len(symbols))
    if not symbols:
        logger.error("Empty universe – aborting run to avoid accidental actions")
        _finish(ctx, "aborted:empty_universe", equity_after=pf.cash + value_before)
        return

    # 5) Determine index regime (bull/bear)
    bull, idx_last, idx_ema = index_trend(ctx)
    logger.info("Index %.2f vs 200-EMA %.2f → %s", idx_last, idx_ema, "BULL" if bull else "BEAR")
    art.record(regime={"index_close": idx_last, "ema200": idx_ema, "bull": bull})

    # 6) Build & rank universe
    universe = get_universe(ctx, symbols)
    ranks = rank_universe(ctx, universe)
    total = len(ranks)  # maps best=0.0, worst=1.0
    art.record(ranked_count=total)
    art.write_table(
        "ranking",
        RANKING_COLUMNS,
        [
            {
                "rank": i + 1,
                "symbol": r.symbol,
                "pct_rank": (i + 1) / total,
                "score": r.score,
                "annual_slope": r.annual_slope,
                "r2": r.r2,
                "close": r.close,
                "ema100": r.ema100,
                "held": r.symbol in pf.positions,
            }
            for i, r in enumerate(ranks)
        ],
    )
    for r in ranks[:20]:
        logger.info("Top %s: score=%.4f ann=%.2f%% R²=%.2f", r.symbol, r.score, 100 * r.annual_slope, r.r2)

    # 7) Exits (with guard against empty ranks inside prune_portfolio)
    prune_portfolio(ctx, ranks)

    # 8) If withdrawals exceed available cash, raise cash by selling worst holdings
    raise_cash_if_needed(ctx, ranks)

    # 9) Size parity rebalance (every even ISO week in IST), cash-aware
    resize_positions(ctx, bull)

    # 10) Cash left after re-sizing
    cash_left = pf.cash
    account_equity = cash_left + live_value(ctx)
    logger.info(
        "After resize → Equity: %.0f  | Cash: %.0f  | Positions: %d", account_equity, cash_left, len(pf.positions)
    )

    # 11) New buys if bull regime and cash available; every ranked name visited goes to candidates.csv
    candidates: list[dict[str, Any]] = []
    if bull and cash_left > 0:
        for idx, r in enumerate(ranks):
            pct_rank = (idx + 1) / total
            row: dict[str, Any] = {"rank": idx + 1, "symbol": r.symbol, "pct_rank": pct_rank}
            candidates.append(row)
            if pct_rank > s.cut_off_pct:
                row["decision"] = "SKIP:beyond_cutoff"
                continue
            if len(pf.positions) >= s.max_positions:
                logger.info("Max positions reached (%d) – stopping new buys", s.max_positions)
                row["decision"] = "SKIP:max_positions"
                break
            if r.symbol in pf.positions:
                row["decision"] = "SKIP:held"
                continue
            if r.symbol in pf.sold:
                row["decision"] = "SKIP:sold_this_run"
                continue
            try:
                qty = target_shares(ctx, r.symbol, account_equity)
            except Exception as exc:
                logger.warning("size calc error %s – %s: %s", r.symbol, type(exc).__name__, exc)
                row["decision"] = "SKIP:size_error"
                continue
            if qty < MIN_SHARES:
                row["decision"] = "SKIP:below_min_shares"
                continue
            cost_needed = gross_cost_for_buy(s, r.close, qty)
            if cost_needed > pf.cash + 1e-6:
                # buy as much as possible
                affordable_qty = int(math.floor(pf.cash / (r.close * (1.0 + s.fees_pct + s.slippage_pct))))
            else:
                affordable_qty = qty
            row.update(qty=affordable_qty, est_cost=gross_cost_for_buy(s, r.close, affordable_qty))
            if affordable_qty < MIN_SHARES:
                row["decision"] = "SKIP:no_cash"
                continue

            if safe_buy(ctx, r.symbol, affordable_qty) is not None:
                pf.positions[r.symbol] = affordable_qty
                # update for subsequent picks
                account_equity = pf.cash + live_value(ctx)
                row.update(decision="BUY", cash_after=pf.cash)
            else:
                row["decision"] = "SKIP:not_placed"
    else:
        logger.info("No new buys – bear regime or no cash.")
    art.write_table("candidates", CANDIDATE_COLUMNS, candidates)

    # 12) Write next portfolio snapshot
    write_portfolio(s.out_file, pf.positions)
    equity_after = pf.cash + live_value(ctx)
    logger.info(
        "Done. Final → Equity: %.0f  | Cash: %.0f  | Holdings: %d",
        equity_after,
        pf.cash,
        len(pf.positions),
    )
    _finish(ctx, "completed", equity_after=equity_after)


def run_mode(settings: Settings) -> str:
    """``kill``, ``paper`` or ``live``: the run directory's suffix."""
    if settings.kill_switch:
        return "kill"
    return "live" if settings.allow_kite_execution else "paper"


def main() -> None:
    configure_logging("INFO")  # the package logger, at entry, never at import (ADR-015)

    # 0) Load and validate configuration; a bad value names itself and stops the run
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None
    configure_logging(settings.log_level)

    # 1) Run only on the configured weekday in IST (0=Mon; default 2=Wed), before any login
    if not settings.kill_switch and ist_now().weekday() != settings.trading_weekday:
        logger.info("Not scheduled trading weekday (IST) – abort")
        return

    # Past the guard: this run gets a directory, and its log goes there too (ADR-006)
    artifacts: Artifacts
    try:
        artifacts = RunArtifacts.create(
            settings.runs_dir, started=ist_now(), mode=run_mode(settings), settings=settings
        )
    except OSError as exc:
        logger.warning(
            "Could not create a run directory under %s (%s); running without artifacts", settings.runs_dir, exc
        )
        artifacts = NoArtifacts()
    artifacts.attach_log()

    try:
        kite = authenticate(settings)
        paper = not settings.allow_kite_execution
        broker: Broker = PaperBroker(kite) if paper else kite
        ctx = RunContext(
            settings=settings,
            broker=broker,
            candles=CandleStore(broker, settings.cache_dir, sleep_sec=settings.candle_sleep_sec),
            now=ist_now,
            paper=paper,
            artifacts=artifacts,
        )
        run(ctx)
    except KeyboardInterrupt:
        artifacts.finish("failed:KeyboardInterrupt")
        raise
    except Exception as exc:
        logger.exception("Run failed: %s", exc)
        artifacts.finish(f"failed:{type(exc).__name__}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
