#!/usr/bin/env python3
"""
Weekly momentum portfolio for NSE equities
=================================================================

Adds persistent cash handling:
• Track CASH via:
  - STARTING_CASH (once) + cash_ledger (deposits/withdrawals) + trades_ledger (buys/sells)
• ENV_CASHFLOW lets you add/withdraw money "today" without editing files
• Auto-raise cash for withdrawals by selling worst-ranked holdings
• Sizing now uses dynamic equity = cash + live mark-to-market
• Trades are recorded with cash deltas (fees + slippage included)

Portfolio CSV format unchanged: SYMBOL,QUANTITY (no header)

Files (default names; override via env):
- portfolio_updated.csv                  (current positions)
- portfolio_next_updated.csv             (next snapshot after run)
- cash_ledger_updated.csv                (date, amount, note)  # +ve deposit, -ve withdraw
- trades_ledger_updated.csv              (timestamp, side, symbol, qty, price, fees_pct, slippage_pct, cash_delta)

NOTE: Research code. Real-money deployment should add robust order-state handling,
retries, and broker-confirmed fills before writing the trade ledger.
"""

# ── std / 3-party ────────────────────────────────────────────────────────
import csv
import logging
import math
import os
import time
from collections import namedtuple
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect

# ── configuration ────────────────────────────────────────────────────────
API_KEY = os.getenv("KITE_API_KEY", "YOUR_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET", "YOUR_API_SECRET")

INDEX_SYM = os.getenv("INDEX_SYMBOL", "NIFTY 50")
INDEX_EXCH = os.getenv("INDEX_EXCHANGE", "NSE")

# Legacy default; now only used as fallback for STARTING_CASH default:
ACCOUNT_VALUE = float(os.getenv("ACCOUNT_VALUE", 1_00_000))

RISK_FACTOR = float(os.getenv("RISK_FACTOR", 0.001))  # 0.1 % per ATR
ATR_PERIOD = int(os.getenv("ATR_PERIOD", 20))
MIN_SHARES = 1

# new sizing guard
MAX_WEIGHT = float(os.getenv("MAX_WEIGHT", 0.10))  # 10 % cap

# scoring & filters
MA_PERIOD_200 = 200
MA_FILTER_100: int = 100
LOOKBACK_R21 = 21
LOOKBACK_R63 = 63
LOOKBACK_R126 = 126
look_backs = [LOOKBACK_R21, LOOKBACK_R63, LOOKBACK_R126]
REG_LOOKBACK = 90
MIN_VOLUME = int(os.getenv("MIN_VOLUME", 10_000))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", 0.10))  # 10 % of price
EXIT_MULTIPLE = float(os.getenv("EXIT_MULTIPLE", 3.0))  # 3×ATR stop

EXTRA_DAYS_PAD = 50
TRADING_DAYS_YR = 250
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", 20))
TRADING_WEEKDAY = int(os.getenv("TRADING_WEEKDAY", 2))  # 0=Mon (Wed=2)
CUT_OFF_PCT = float(os.getenv("CUT_OFF_PCT", 0.20))

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

PORTFOLIO_FILE = os.getenv("PORTFOLIO_FILE", "current_portfolio.csv")
OUT_FILE = os.getenv("OUT_FILE", "next_portfolio.csv")

# NEW: cash/trades ledgers & cashflow env toggles
STARTING_CASH = float(os.getenv("STARTING_CASH", ACCOUNT_VALUE))
CASH_LEDGER_FILE = os.getenv("CASH_LEDGER_FILE", "cash_ledger.csv")
TRADES_LEDGER_FILE = os.getenv("TRADES_LEDGER_FILE", "trades_ledger.csv")
ENV_CASHFLOW = float(os.getenv("ENV_CASHFLOW", 0.0))  # +ve deposit today; -ve withdrawal
CASHFLOW_NOTE = os.getenv("CASHFLOW_NOTE", "env-cashflow")

# rough all-in fees (+ STT, exchange, GST…) and a small slippage budget
FEES_PCT = float(os.getenv("FEES_PCT", 0.0015))  # 0.15%
SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", 0.0005))  # 0.05%

# Timezone: run scheduling and biweekly parity in IST
IST = ZoneInfo("Asia/Kolkata")


def ist_now() -> datetime:
    """Convenience: current time in IST."""
    return datetime.now(IST)


# ── logging init ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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

sold_symbols: set[str] = set()
allow_kite_execution = os.getenv("ALLOW_KITE_EXECUTION", "1").lower() not in ("0", "false", "no", "n", "off")

# Universe set (filled at runtime inside main; empty at import to avoid net fetch)
NIFTY500_SET: set[str] = set()

# Simple cache for instrument tokens to reduce API calls
TOKEN_CACHE: dict[str, int] = {}

# Global cash balance (updated as we trade this run)
CASH_BAL: float = 0.0


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


def append_env_cashflow_if_any() -> None:
    """If ENV_CASHFLOW!=0, append a dated row to cash ledger for today."""
    if abs(ENV_CASHFLOW) < 1e-9:
        return
    _ensure_csv(CASH_LEDGER_FILE, ["date", "amount", "note"])
    _append_row(CASH_LEDGER_FILE, [ist_now().date().isoformat(), f"{ENV_CASHFLOW:.2f}", CASHFLOW_NOTE])
    logger.info("Applied ENV_CASHFLOW: %+,.2f (%s)", ENV_CASHFLOW, CASHFLOW_NOTE)


def cash_from_cash_ledger() -> float:
    """Sum deposits/withdrawals from cash ledger."""
    if not os.path.isfile(CASH_LEDGER_FILE):
        return 0.0
    total = 0.0
    with open(CASH_LEDGER_FILE, newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                amt = float(row.get("amount", "0").strip())
                # optional: honor date col ≤ today, but most users log after-the-fact
                total += amt
            except Exception:
                continue
    return total


def trades_cash_delta() -> float:
    """Sum cash impact from the trades ledger (already net of fees/slippage)."""
    if not os.path.isfile(TRADES_LEDGER_FILE):
        return 0.0
    total = 0.0
    with open(TRADES_LEDGER_FILE, newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                total += float(row.get("cash_delta", "0").strip())
            except Exception:
                continue
    return total


def init_cash_balance() -> float:
    """Compute starting CASH_BAL for this run and set global."""
    global CASH_BAL
    _ensure_csv(CASH_LEDGER_FILE, ["date", "amount", "note"])
    _ensure_csv(
        TRADES_LEDGER_FILE, ["timestamp", "side", "symbol", "qty", "price", "fees_pct", "slippage_pct", "cash_delta"]
    )
    append_env_cashflow_if_any()
    CASH_BAL = STARTING_CASH + cash_from_cash_ledger() + trades_cash_delta()
    logger.info(
        "Cash reconstructed: START=%.2f, ledger=%.2f, trades=%.2f → CASH=%.2f",
        STARTING_CASH,
        cash_from_cash_ledger(),
        trades_cash_delta(),
        CASH_BAL,
    )
    return CASH_BAL


def record_trade(side: str, symbol: str, qty: int, price: float) -> float:
    """Write a trade to the trades ledger and update CASH_BAL.

    Returns the cash_delta applied (positive if cash increases).
    """
    global CASH_BAL
    if qty <= 0 or price <= 0:
        return 0.0
    # Approximate all-in price impact
    if side.upper() == "BUY":
        cash_delta = -qty * price * (1.0 + FEES_PCT + SLIPPAGE_PCT)
    elif side.upper() == "SELL":
        cash_delta = +qty * price * (1.0 - FEES_PCT - SLIPPAGE_PCT)
    else:
        raise ValueError("side must be BUY or SELL")
    _append_row(
        TRADES_LEDGER_FILE,
        [
            ist_now().isoformat(timespec="seconds"),
            side.upper(),
            symbol.upper(),
            qty,
            f"{price:.4f}",
            f"{FEES_PCT:.6f}",
            f"{SLIPPAGE_PCT:.6f}",
            f"{cash_delta:.2f}",
        ],
    )
    CASH_BAL += cash_delta
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


# ═════════════════════════════════════════════════════════════════════════
# Kite helpers
# ═════════════════════════════════════════════════════════════════════════
def authenticate() -> KiteConnect:
    """Interactive Kite authentication: prints login URL and prompts for request-token."""
    kite = KiteConnect(api_key=API_KEY)
    print("Login URL:\n", kite.login_url())
    rq = input("Paste request-token ➜ ").strip()
    sess = kite.generate_session(rq, api_secret=API_SECRET)
    kite.set_access_token(sess["access_token"])
    logger.info("Kite session established.")
    return kite


# ═════════════════════════════════════════════════════════════════════════
# Market-data utilities
# ═════════════════════════════════════════════════════════════════════════
def candles_df(k: KiteConnect, token: int, days: int) -> pd.DataFrame:
    """Fetch ~days of daily candles plus extra padding for indicators."""
    end: date = datetime.now().date()
    extra = max(days // 2, EXTRA_DAYS_PAD)
    start: date = end - timedelta(days=days + extra)
    raw = k.historical_data(token, start, end, "day", continuous=False, oi=False)
    return pd.DataFrame(raw)


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """Average True Range over *period* (simple mean), guarded for tiny frames."""
    if df.empty:
        return float("nan")
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    tr = tr.iloc[1:]  # drop first NaN due to shift
    if tr.empty:
        return float("nan")
    n = min(period, len(tr))
    return float(tr.tail(n).mean())


# ═════════════════════════════════════════════════════════════════════════
# Strategy building blocks
# ═════════════════════════════════════════════════════════════════════════
def token_of(k: KiteConnect, sym: str = INDEX_SYM, exch: str = INDEX_EXCH) -> int:
    """Resolve instrument_token for EXCH:SYMBOL."""
    key = f"{exch}:{sym}"
    if key in TOKEN_CACHE:
        return TOKEN_CACHE[key]

    try:
        q = k.quote([key]).get(key, {})
        tok = q.get("instrument_token")
        if tok is not None:
            TOKEN_CACHE[key] = tok
            return tok
    except Exception:
        pass

    for inst in k.instruments(exch):
        ts = inst.get("tradingsymbol")
        if ts == sym and inst.get("instrument_token"):
            TOKEN_CACHE[key] = inst["instrument_token"]
            return inst["instrument_token"]
    raise KeyError(f"Cannot resolve instrument_token for {key}")


# ── 1 ▸ index regime ─────────────────────────────────────────────────────
def index_trend(k: KiteConnect):
    """Return (is_bull, last_close, ema200) for the chosen index."""
    tok = token_of(k)
    closes = candles_df(k, tok, MA_PERIOD_200)["close"]
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
    comp = r21 + r63 + r126

    y = np.log(closes.iloc[-REG_LOOKBACK:])
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = intercept + slope * x
    ss_res = float(((y - y_hat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0

    return comp * r2, annualise(float(slope)), float(r2)


def rank_universe(k: KiteConnect, universe: Iterable[dict]) -> list[RankItem]:
    """Rank eligible instruments in *universe* by composite momentum score."""
    ranks: list[RankItem] = []
    for inst in universe:
        tok, sym = inst["instrument_token"], inst["tradingsymbol"]
        try:
            min_history = max(MA_FILTER_100, LOOKBACK_R126 + 1, REG_LOOKBACK + 1)
            df = candles_df(k, tok, min_history)
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

            if last <= float(closes.iloc[-(LOOKBACK_R126 + 1)]):
                continue

            if float(vols.iloc[-20:].mean()) < MIN_VOLUME:
                continue

            _atr = atr(df)
            if math.isnan(_atr) or (last > 0 and _atr / last > MAX_ATR_PCT):
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
def target_shares(k: KiteConnect, sym: str, account_equity: float) -> int:
    """ATR-based risk parity sizing with MAX_WEIGHT cap on dynamic equity.

    Size = (account_equity × RISK_FACTOR) / ATR, capped at MAX_WEIGHT notional.
    """
    tok = token_of(k, sym, "NSE")
    df = candles_df(k, tok, ATR_PERIOD)
    if len(df) <= ATR_PERIOD:
        raise ValueError("Not enough candles for ATR")
    atr_value = atr(df)
    if not atr_value or atr_value <= 0:
        raise ValueError("ATR zero")
    shares = (account_equity * RISK_FACTOR) / atr_value
    price = float(df["close"].iloc[-1])
    cap = (account_equity * MAX_WEIGHT) / price if price > 0 else 0
    return max(math.floor(min(shares, cap)), 0)


# Helpers for cash-aware costing
def gross_cost_for_buy(price: float, qty: int) -> float:
    return qty * price * (1.0 + FEES_PCT + SLIPPAGE_PCT)


def net_proceeds_for_sell(price: float, qty: int) -> float:
    return qty * price * (1.0 - FEES_PCT - SLIPPAGE_PCT)


# ── 4 ▸ order wrappers (cash-aware) ──────────────────────────────────────
def safe_buy(k: KiteConnect, sym: str, qty: int) -> float | None:
    """Place a BUY order and record cash impact; returns used price if placed."""
    if qty < MIN_SHARES:
        return None

    srs = series_of(sym)
    quote = k.quote([f"NSE:{sym}"])[f"NSE:{sym}"]
    # Use a reasonable price even in paper mode
    price_used = (
        quote["depth"]["sell"][0]["price"]
        if (srs in NO_MARKET_SERIES and quote["depth"]["sell"])
        else quote["last_price"]
    )

    # If execution disabled, SIMULATE the fill so portfolio & cash update
    if not allow_kite_execution:
        need = gross_cost_for_buy(price_used, qty)
        if need > CASH_BAL + 1e-6:
            logger.info("Not enough cash for BUY %s x%d (need %.2f, have %.2f)", sym, qty, need, CASH_BAL)
            return None
        record_trade("BUY", sym, qty, price_used)
        logger.info("PAPER BUY %-10s x%4d @ %.2f   (cash → %.2f)", sym, qty, price_used, CASH_BAL)
        return price_used

    srs = series_of(sym)
    price_used: float

    if srs in NO_MARKET_SERIES:  # ---- BE, BZ, … path
        quote = k.quote([f"NSE:{sym}"])[f"NSE:{sym}"]
        price_used = quote["depth"]["sell"][0]["price"] if quote["depth"]["sell"] else quote["last_price"]
        if gross_cost_for_buy(price_used, qty) > CASH_BAL + 1e-6:
            logger.info(
                "Not enough cash for BUY %s x%d (need %.2f, have %.2f)",
                sym,
                qty,
                gross_cost_for_buy(price_used, qty),
                CASH_BAL,
            )
            return None
        k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=sym,
            transaction_type=k.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_LIMIT,
            price=price_used,
            validity=k.VALIDITY_DAY,
        )
    else:  # ---- normal EQ path (market)
        quote = k.quote([f"NSE:{sym}"])[f"NSE:{sym}"]
        price_used = quote["last_price"]
        if gross_cost_for_buy(price_used, qty) > CASH_BAL + 1e-6:
            logger.info(
                "Not enough cash for BUY %s x%d (need %.2f, have %.2f)",
                sym,
                qty,
                gross_cost_for_buy(price_used, qty),
                CASH_BAL,
            )
            return None
        k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=sym,
            transaction_type=k.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_MARKET,
        )

    record_trade("BUY", sym, qty, price_used)
    logger.info("BUY  %-10s x%4d @ %.2f   (cash → %.2f)", sym, qty, price_used, CASH_BAL)
    return price_used


def safe_sell(k: KiteConnect, sym: str, qty: int) -> float | None:
    """Place a SELL order and record cash impact; returns used price if placed."""
    if qty < MIN_SHARES:
        return None

    quote = k.quote([f"NSE:{sym}"])[f"NSE:{sym}"]
    price_used = quote["last_price"]

    if not allow_kite_execution:
        record_trade("SELL", sym, qty, price_used)
        logger.info("PAPER SELL %-9s x%4d @ %.2f   (cash → %.2f)", sym, qty, price_used, CASH_BAL)
        return price_used

    srs = series_of(sym)
    price_used: float

    if srs in NO_MARKET_SERIES:  # ---------- BE etc. (limit ~ best bid)
        quote = k.quote([f"NSE:{sym}"])[f"NSE:{sym}"]
        price_used = quote["depth"]["buy"][0]["price"] if quote["depth"]["buy"] else quote["last_price"]
        k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=sym,
            transaction_type=k.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_LIMIT,
            price=price_used,
            validity=k.VALIDITY_DAY,
        )
    else:  # ---------- normal EQ path (market)
        quote = k.quote([f"NSE:{sym}"])[f"NSE:{sym}"]
        price_used = quote["last_price"]
        k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=sym,
            transaction_type=k.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_MARKET,
        )

    record_trade("SELL", sym, qty, price_used)
    logger.info("SELL %-10s x%4d @ %.2f   (cash → %.2f)", sym, qty, price_used, CASH_BAL)
    return price_used


# ── 5 ▸ exit & size-rebalance ────────────────────────────────────────────
def _trailing_stop_hit(k: KiteConnect, sym: str) -> bool:
    """3×ATR trailing stop using rolling highs."""
    tok = token_of(k, sym, "NSE")
    look = max(ATR_PERIOD, max(look_backs))
    df = candles_df(k, tok, look)
    if len(df) < ATR_PERIOD + 1:
        logger.warning("Not enough candles for trailing stop")
        return False
    last = float(df["close"].iloc[-1])
    _atr = atr(df)
    if math.isnan(_atr):
        return False
    window = max(ATR_PERIOD, 2 * ATR_PERIOD)
    highest = float(df["high"].rolling(window).max().iloc[-1])
    return last < (highest - EXIT_MULTIPLE * _atr)


def should_exit(rank: RankItem | None, pct_rank: float, k: KiteConnect | None = None) -> bool:
    """Exit conditions: rank drop, below 100-EMA, or trailing stop."""
    cond_rank = (rank is None) or (pct_rank > CUT_OFF_PCT) or (rank.close <= rank.ema100)
    if k is None or rank is None:
        return cond_rank
    return cond_rank or _trailing_stop_hit(k, rank.symbol)


def prune_portfolio(k: KiteConnect, ranks: list[RankItem], pf: dict[str, int]) -> None:
    """Sell names that violate exit rules. Guarded against empty ranking."""
    if not ranks:
        logger.warning("Ranking empty – skipping prune to avoid accidental liquidation")
        return

    idx = {r.symbol: i for i, r in enumerate(ranks)}
    rmap = {r.symbol: r for r in ranks}
    total = len(ranks) if ranks else 1
    for sym, qty in list(pf.items()):
        pct = (idx[sym] + 1) / total if sym in idx else 1.0
        if should_exit(rmap.get(sym), pct, k=k):
            safe_sell(k, sym, qty)
            pf.pop(sym)
            sold_symbols.add(sym)


def live_value(k: KiteConnect, pf: dict[str, int]) -> float:
    """Mark-to-market portfolio value using last traded price (LTP)."""
    if not pf:
        return 0.0
    q = k.quote([f"NSE:{s}" for s in pf])
    return float(sum(pf[s] * q[f"NSE:{s}"]["last_price"] for s in pf))


def resize_positions(k: KiteConnect, pf: dict[str, int], bull: bool) -> None:
    """Every even ISO week (IST), rebalance sizes toward ATR targets (cash-aware)."""
    if ist_now().isocalendar().week % 2:  # odd ISO week → skip
        logger.debug("Size rebalance skipped (odd week, IST)")
        return

    account_equity = CASH_BAL + live_value(k, pf)

    to_up, to_down = {}, {}
    for sym, qty in pf.items():
        try:
            tgt = target_shares(k, sym, account_equity)
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
        safe_sell(k, sym, delta)
        pf[sym] -= delta
        if pf[sym] == 0:
            pf.pop(sym)
            sold_symbols.add(sym)

    if not bull or CASH_BAL <= 0:
        return

    prices = k.quote([f"NSE:{s}" for s in to_up]) if to_up else {}
    for sym, delta in to_up.items():
        price = prices[f"NSE:{sym}"]["last_price"]
        need = gross_cost_for_buy(price, delta)
        if need > CASH_BAL + 1e-6:
            continue
        if safe_buy(k, sym, delta) is not None:
            pf[sym] = pf.get(sym, 0) + delta


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


def get_universe(kite: KiteConnect) -> list[dict]:
    """Universe = NSE equity instruments whose base symbols are in NIFTY-500."""
    return [
        i
        for i in kite.instruments("NSE")
        if (
            i.get("instrument_type") == "EQ"
            and i.get("segment") == "NSE"
            and base_symbol(str(i.get("tradingsymbol", ""))) in NIFTY500_SET
        )
    ]


# ── 6 ▸ cash management for withdrawals ──────────────────────────────────
def raise_cash_if_needed(k: KiteConnect, ranks: list[RankItem], pf: dict[str, int]) -> None:
    """If CASH_BAL < 0 (withdrawal > cash), sell worst-ranked holdings to cover."""
    global CASH_BAL
    if CASH_BAL >= 0:
        return
    need = -CASH_BAL + 1e-6

    idx = {r.symbol: i for i, r in enumerate(ranks)}

    # Sort holdings by WORST rank first; unknowns at the very end
    def sort_key(sym: str) -> int:
        return idx.get(sym, 10**9)

    hold_syms = sorted(list(pf.keys()), key=sort_key, reverse=True)
    quotes = k.quote([f"NSE:{s}" for s in hold_syms]) if hold_syms else {}

    for sym in hold_syms:
        if need <= 0:
            break
        qty = pf[sym]
        price = quotes[f"NSE:{sym}"]["last_price"]
        if price <= 0:
            continue
        per_share = net_proceeds_for_sell(price, 1)
        sell_qty = min(qty, int(math.ceil(need / per_share)))
        if sell_qty <= 0:
            continue
        safe_sell(k, sym, sell_qty)
        pf[sym] -= sell_qty
        if pf[sym] == 0:
            pf.pop(sym)
            sold_symbols.add(sym)
        need = -CASH_BAL  # update remaining need after cash change

    if CASH_BAL < 0:
        logger.warning("Could not fully raise cash for withdrawal. Short by %.2f", -CASH_BAL)
    else:
        logger.info("Raised cash for withdrawal. Cash now: %.2f", CASH_BAL)


# ═════════════════════════════════════════════════════════════════════════
# Main weekly routine
# ═════════════════════════════════════════════════════════════════════════
def main() -> None:
    # 1) Run only on the configured weekday in IST (0=Mon; default 2=Wed)
    if ist_now().weekday() != TRADING_WEEKDAY:
        logger.info("Not scheduled trading weekday (IST) – abort")
        return

    # 2) Load current portfolio, ledgers & authenticate Kite
    portfolio = read_portfolio(PORTFOLIO_FILE)
    init_cash_balance()
    kite = authenticate()

    # 3) Fetch NIFTY-500 constituents at runtime (not at import time)
    global NIFTY500_SET
    NIFTY500_SET = set(fetch_index_constituents("NIFTY 500"))
    logger.info("NIFTY-500 symbols loaded: %d", len(NIFTY500_SET))
    if not NIFTY500_SET:
        logger.error("Empty universe – aborting run to avoid accidental actions")
        return

    # 4) Determine index regime (bull/bear)
    bull, idx_last, idx_sma = index_trend(kite)
    logger.info("Index %.2f vs 200-EMA %.2f → %s", idx_last, idx_sma, "BULL" if bull else "BEAR")

    # 5) Build & rank universe
    universe = get_universe(kite)
    ranks = rank_universe(kite, universe)
    for r in ranks[:10]:
        logger.info("Top %s: score=%.4f ann=%.2f%% R²=%.2f", r.symbol, r.score, 100 * r.annual_slope, r.r2)

    # 6) Exits (with guard against empty ranks inside prune_portfolio)
    prune_portfolio(kite, ranks, portfolio)

    # 7) If withdrawals exceed available cash, raise cash by selling worst holdings
    raise_cash_if_needed(kite, ranks, portfolio)

    # 8) Size parity rebalance (every even ISO week in IST), cash-aware
    resize_positions(kite, portfolio, bull)

    # 9) Cash left after re-sizing
    cash_left = CASH_BAL
    eq_val = live_value(kite, portfolio)
    account_equity = cash_left + eq_val
    logger.info(
        "After rebalance → Equity: %.0f  | Cash: %.0f  | Positions: %d", account_equity, cash_left, len(portfolio)
    )

    # 10) New buys if bull regime and cash available
    if bull and cash_left > 0:
        for r in ranks:
            if len(portfolio) >= MAX_POSITIONS:
                logger.info("Max positions reached (%d) – stopping new buys", MAX_POSITIONS)
                break
            if r.symbol in portfolio or r.symbol in sold_symbols:
                continue
            try:
                qty = target_shares(kite, r.symbol, account_equity)
            except Exception as exc:
                logger.debug("size calc error %s – %s", r.symbol, exc)
                continue
            if qty < MIN_SHARES:
                continue
            cost_needed = gross_cost_for_buy(r.close, qty)
            if cost_needed > CASH_BAL + 1e-6:
                continue
            if safe_buy(kite, r.symbol, qty) is not None:
                portfolio[r.symbol] = qty
                # update for subsequent picks
                eq_val = live_value(kite, portfolio)
                account_equity = CASH_BAL + eq_val
    else:
        logger.info("No new buys – bear regime or no cash.")

    # 11) Write next portfolio snapshot
    write_portfolio(OUT_FILE, portfolio)
    logger.info(
        "Done. Final → Equity: %.0f  | Cash: %.0f  | Holdings: %d",
        CASH_BAL + live_value(kite, portfolio),
        CASH_BAL,
        len(portfolio),
    )


if __name__ == "__main__":
    main()
