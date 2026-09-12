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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from stocks_on_the_move import kite_auth
from stocks_on_the_move.broker import Broker, Instrument, KiteBroker, Order, OrderType, PaperBroker
from stocks_on_the_move.candles import CandleStore
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


@dataclass
class RunContext:
    """Everything a run needs, assembled once by ``main()`` (ADR-008).

    ``paper`` only labels the trade log lines; paper mode itself is the choice
    of a ``PaperBroker`` as ``broker``. ``universe`` supplies the base symbols
    the strategy may hold; ``None`` means the NSE archives, per settings.
    """

    settings: Settings
    broker: Broker
    candles: CandleStore
    now: Callable[[], datetime] = ist_now
    paper: bool = False
    portfolio: Portfolio = field(default_factory=Portfolio)
    tokens: dict[str, int] = field(default_factory=dict)  # "EXCH:SYMBOL" -> instrument_token
    universe: Callable[[], set[str]] | None = None


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


def _append_row(path: str, row: list[str | float | int]) -> None:
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
    _ensure_csv(
        s.trades_ledger_file,
        ["timestamp", "side", "symbol", "qty", "price", "fees_pct", "slippage_pct", "cash_delta"],
    )
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
    _append_row(
        s.trades_ledger_file,
        [
            ctx.now().isoformat(timespec="seconds"),
            side,
            symbol.upper(),
            qty,
            f"{price:.4f}",
            f"{s.fees_pct:.6f}",
            f"{s.slippage_pct:.6f}",
            f"{cash_delta:.2f}",
        ],
    )
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


def rank_universe(ctx: RunContext, universe: Iterable[Instrument]) -> list[RankItem]:
    """Rank eligible instruments in *universe* by composite momentum score."""
    s = ctx.settings
    ranks: list[RankItem] = []
    min_history = max(MA_FILTER_100, LOOKBACK_R126 + 1, REG_LOOKBACK + 1)
    for inst in universe:
        tok, sym = inst.token, inst.tradingsymbol
        try:
            df = ctx.candles.get(tok, min_history)
            if df.empty:
                continue

            closes = df["close"]
            vols = df["volume"]
            if len(closes) < min_history:
                continue

            ema100 = pd.Series(closes).ewm(span=MA_FILTER_100, adjust=False).mean().iloc[-1]
            last = float(closes.iloc[-1])
            if last <= float(ema100):
                continue

            if float(vols.iloc[-20:].mean()) < s.min_volume:
                continue

            _atr = atr(df, s.atr_period)
            if math.isnan(_atr) or (last > 0 and _atr / last > s.max_atr_pct):
                continue

            score, ann_slope, r2 = _composite_momentum(closes)
            if math.isnan(score):
                logger.warning("Not enough data to rank for %s", sym)
                continue

            ranks.append(RankItem(sym, float(score), float(ann_slope), float(r2), last, float(ema100)))
        except Exception as exc:
            logger.debug("%s skipped – %s", sym, exc)
    logger.info("Ranked universe: %d symbols", len(ranks))
    return sorted(ranks, key=lambda r: r.score, reverse=True)


# ── 3 ▸ ATR position size ────────────────────────────────────────────────
def target_shares(ctx: RunContext, sym: str, account_equity: float) -> int:
    """ATR-based risk parity sizing with MAX_WEIGHT cap on dynamic equity.

    Size = (account_equity × RISK_FACTOR) / ATR, capped at MAX_WEIGHT notional.
    """
    s = ctx.settings
    tok = token_of(ctx, sym, "NSE")
    df = ctx.candles.get(tok, s.atr_period)
    if len(df) <= s.atr_period:
        raise ValueError("Not enough candles for ATR")
    atr_value = atr(df, s.atr_period)
    if not atr_value or atr_value <= 0:
        raise ValueError("ATR zero")
    shares = (account_equity * s.risk_factor) / atr_value
    price = float(df["close"].iloc[-1])
    cap = (account_equity * s.max_weight) / price if price > 0 else 0
    return max(math.floor(min(shares, cap)), 0)


# Helpers for cash-aware costing
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


def _price_for(ctx: RunContext, sym: str, side: str) -> tuple[float, OrderType]:
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


def _place(ctx: RunContext, sym: str, side: str, qty: int, price: float, order_type: OrderType) -> None:
    limit = price if order_type == "LIMIT" else None
    ctx.broker.place_order(Order(sym, side, qty, order_type, limit_price=limit))  # type: ignore[arg-type]
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
def _trailing_stop_hit(ctx: RunContext, sym: str) -> bool:
    """n×ATR trailing stop using rolling highs."""
    s = ctx.settings
    tok = token_of(ctx, sym, "NSE")
    look = max(s.atr_period, LOOKBACK_R126)
    df = ctx.candles.get(tok, look)
    if len(df) < s.atr_period + 1:
        logger.warning("Not enough candles for trailing stop")
        return False
    last = float(df["close"].iloc[-1])
    _atr = atr(df, s.atr_period)
    if math.isnan(_atr):
        return False
    window = max(s.atr_period, 2 * s.atr_period)
    highest = float(df["close"].rolling(window).max().iloc[-1])
    return last < (highest - s.exit_multiple * _atr)


def rank_says_exit(settings: Settings, rank: RankItem | None, pct_rank: float) -> bool:
    """The exit rules that need no market data: unranked, below the cut-off, or under the 100-EMA."""
    return rank is None or pct_rank > settings.cut_off_pct or rank.close <= rank.ema100


def should_exit(ctx: RunContext, rank: RankItem | None, pct_rank: float) -> bool:
    """Exit conditions: rank drop, below 100-EMA, or trailing stop."""
    if rank is None or rank_says_exit(ctx.settings, rank, pct_rank):
        return True
    return _trailing_stop_hit(ctx, rank.symbol)


def prune_portfolio(ctx: RunContext, ranks: list[RankItem]) -> None:
    """Sell names that violate exit rules. Guarded against empty ranking."""
    if not ranks:
        logger.warning("Ranking empty – skipping prune to avoid accidental liquidation")
        return

    pf = ctx.portfolio
    idx = {r.symbol: i for i, r in enumerate(ranks)}
    rmap = {r.symbol: r for r in ranks}
    total = len(ranks)
    for sym, qty in list(pf.positions.items()):
        pct = (idx[sym] + 1) / total if sym in idx else 1.0
        if should_exit(ctx, rmap.get(sym), pct):
            safe_sell(ctx, sym, qty)
            pf.positions.pop(sym)
            pf.sold.add(sym)


def live_value(ctx: RunContext) -> float:
    """Mark-to-market portfolio value using LTP (batched)."""
    pf = ctx.portfolio.positions
    if not pf:
        return 0.0
    prices = ltp_map(ctx.broker, pf.keys())
    return float(sum(pf[s] * prices.get(s, 0.0) for s in pf))


def resize_positions(ctx: RunContext, bull: bool) -> None:
    """Every even ISO week (IST), rebalance sizes toward ATR targets (cash-aware)."""
    pf = ctx.portfolio
    if (ctx.now().isocalendar().week % 2) and not ctx.settings.force_resize:  # odd ISO week → skip
        logger.debug("Size rebalance skipped (odd week, IST)")
        return

    account_equity = pf.cash + live_value(ctx)

    to_up, to_down = {}, {}
    for sym, qty in pf.positions.items():
        try:
            tgt = target_shares(ctx, sym, account_equity)
        except Exception as exc:
            logger.debug("size calc error %s – %s", sym, exc)
            continue
        diff = tgt - qty
        if diff > 0:
            to_up[sym] = diff
        elif diff < 0:
            to_down[sym] = -diff

    # 1️⃣ sell downs first
    for sym, delta in to_down.items():
        safe_sell(ctx, sym, delta)
        pf.positions[sym] -= delta
        if pf.positions[sym] == 0:
            pf.positions.pop(sym)
            pf.sold.add(sym)

    if not bull or pf.cash <= 0:
        return

    prices = ltp_map(ctx.broker, to_up.keys()) if to_up else {}
    for sym, delta in to_up.items():
        price = prices.get(sym, 0.0)
        need = gross_cost_for_buy(ctx.settings, price, delta)
        if need > pf.cash + 1e-6:
            continue
        if safe_buy(ctx, sym, delta) is not None:
            pf.positions[sym] = pf.positions.get(sym, 0) + delta


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
def run(ctx: RunContext) -> None:
    """Steps 2 to 12 of the weekly routine, against an assembled context."""
    s = ctx.settings
    pf = ctx.portfolio

    # 2) Load current portfolio and ledgers
    pf.positions = read_portfolio(s.portfolio_file)
    init_cash_balance(ctx)

    # 3) Preload instrument tokens once (no quote() here)
    build_token_cache(ctx)
    logger.info("Portfolio value at start: %.2f", live_value(ctx))

    # 3.5) KILL SWITCH – liquidate everything and exit
    if s.kill_switch:
        liquidate_all(ctx)
        write_portfolio(s.out_file, pf.positions)
        logger.warning("KILL SWITCH run finished. Final cash: %.2f", pf.cash)
        return

    # 4) The symbols the strategy may hold (NSE archives unless the context says otherwise)
    symbols = ctx.universe() if ctx.universe is not None else nse_universe_symbols(s)
    logger.info("Universe symbols loaded: %d", len(symbols))
    if not symbols:
        logger.error("Empty universe – aborting run to avoid accidental actions")
        return

    # 5) Determine index regime (bull/bear)
    bull, idx_last, idx_ema = index_trend(ctx)
    logger.info("Index %.2f vs 200-EMA %.2f → %s", idx_last, idx_ema, "BULL" if bull else "BEAR")

    # 6) Build & rank universe
    universe = get_universe(ctx, symbols)
    ranks = rank_universe(ctx, universe)
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

    # 11) New buys if bull regime and cash available
    total = len(ranks)  # maps best=0.0, worst=1.0
    if bull and cash_left > 0:
        for idx, r in enumerate(ranks):
            pct_rank = (idx + 1) / total
            if pct_rank > s.cut_off_pct:
                continue
            if len(pf.positions) >= s.max_positions:
                logger.info("Max positions reached (%d) – stopping new buys", s.max_positions)
                break
            if r.symbol in pf.positions or r.symbol in pf.sold:
                continue
            try:
                qty = target_shares(ctx, r.symbol, account_equity)
            except Exception as exc:
                logger.debug("size calc error %s – %s", r.symbol, exc)
                continue
            if qty < MIN_SHARES:
                continue
            cost_needed = gross_cost_for_buy(s, r.close, qty)
            if cost_needed > pf.cash + 1e-6:
                # buy as much as possible
                affordable_qty = int(math.floor(pf.cash / (r.close * (1.0 + s.fees_pct + s.slippage_pct))))
            else:
                affordable_qty = qty

            if safe_buy(ctx, r.symbol, affordable_qty) is not None:
                pf.positions[r.symbol] = affordable_qty
                # update for subsequent picks
                account_equity = pf.cash + live_value(ctx)
    else:
        logger.info("No new buys – bear regime or no cash.")

    # 12) Write next portfolio snapshot
    write_portfolio(s.out_file, pf.positions)
    logger.info(
        "Done. Final → Equity: %.0f  | Cash: %.0f  | Holdings: %d",
        pf.cash + live_value(ctx),
        pf.cash,
        len(pf.positions),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 0) Load and validate configuration; a bad value names itself and stops the run
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None

    # 1) Run only on the configured weekday in IST (0=Mon; default 2=Wed), before any login
    if not settings.kill_switch and ist_now().weekday() != settings.trading_weekday:
        logger.info("Not scheduled trading weekday (IST) – abort")
        return

    kite = authenticate(settings)
    paper = not settings.allow_kite_execution
    broker: Broker = PaperBroker(kite) if paper else kite
    ctx = RunContext(
        settings=settings,
        broker=broker,
        candles=CandleStore(broker, settings.cache_dir, sleep_sec=settings.candle_sleep_sec),
        now=ist_now,
        paper=paper,
    )
    run(ctx)


if __name__ == "__main__":
    main()
