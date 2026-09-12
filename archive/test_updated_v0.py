#!/usr/bin/env python3
"""
Weekly momentum portfolio for NSE equities — updated & documented
=================================================================

This script implements a weekly momentum strategy for Indian equities on NSE
using Zerodha's Kite Connect. It keeps the original public API / function
names wherever possible but fixes several bugs, improves robustness, and adds
clear comments for new contributors.

Highlights
----------
• Composite momentum score: (R21 + R63 + R126) × R²(90d)
• Absolute-momentum & 100-EMA filter for stocks; index regime via 200-EMA
• Liquidity & volatility screens (min volume, max ATR% of price)
• ATR-risk-parity sizing with 10% max-weight cap
• 3×ATR trailing stop (using rolling highs) on existing positions
• Scheduling & rebalancing in IST (Asia/Kolkata), not UTC
• Liquidation guard if ranking fails (e.g., NIFTY-500 CSV fetch error)
• Safer token resolution with a small cache and fallback to instruments dump
• Safer new-buys loop (skip unaffordable picks, avoid zero-share positions)

Portfolio CSV format is unchanged:  SYMBOL,QUANTITY  (no header)

NOTE: This is sample research code. Real-money deployment should add:
- Order status/error handling & retries
- Slippage/transaction cost modeling
- Persistent ledger for cash/equity (instead of constant ACCOUNT_VALUE)
- Audit logging & metrics
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

PORTFOLIO_FILE = os.getenv("PORTFOLIO_FILE", "portfolio_updated.csv")
OUT_FILE = os.getenv("OUT_FILE", "portfolio_next_updated.csv")

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
    """Fetch ~days of daily candles plus extra padding for indicators.

    Kite's historical_data expects date/datetime. We use date objects and an
    extra padding window to reduce the chance of short frames due to holidays.
    """
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
    """Resolve instrument_token for EXCH:SYMBOL.

    Uses a small in-memory cache; falls back to scanning instruments if quote
    payload doesn't include the token (which can happen for indices).
    """
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
        pass  # fall through to instruments dump

    # Fallback: scan instruments list for a matching tradingsymbol
    for inst in k.instruments(exch):
        ts = inst.get("tradingsymbol")
        if ts == sym and inst.get("instrument_token"):
            TOKEN_CACHE[key] = inst["instrument_token"]
            return inst["instrument_token"]
    raise KeyError(f"Cannot resolve instrument_token for {key}")


# ── 1 ▸ index regime ─────────────────────────────────────────────────────
def index_trend(k: KiteConnect):
    """Return (is_bull, last_close, ema200) for the chosen index.

    We use 200-EMA for regime (fast to adapt). If you prefer SMA, replace
    ewm(...).mean() with rolling(...).mean().
    """
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
    Where Rn is the simple return over n trading days using a strict n-day lag.
    """
    need = max(LOOKBACK_R126, REG_LOOKBACK) + 1
    if len(closes) < need:
        return math.nan, math.nan, math.nan

    last = float(closes.iloc[-1])
    # Use strict n-day lag: price from n sessions ago is at index -(n+1)
    r21 = (last / float(closes.iloc[-(LOOKBACK_R21 + 1)])) - 1.0
    r63 = (last / float(closes.iloc[-(LOOKBACK_R63 + 1)])) - 1.0
    r126 = (last / float(closes.iloc[-(LOOKBACK_R126 + 1)])) - 1.0
    comp = r21 + r63 + r126

    # R² of 90-day log-price regression
    y = np.log(closes.iloc[-REG_LOOKBACK:])
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = intercept + slope * x
    ss_res = float(((y - y_hat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0

    return comp * r2, annualise(float(slope)), float(r2)


def rank_universe(k: KiteConnect, universe: Iterable[dict]) -> list[RankItem]:
    """Rank eligible instruments in *universe* by composite momentum score.

    Applies filters: above 100-EMA, positive 6m absolute return, min 20-day avg
    volume, ATR/price <= MAX_ATR_PCT. Returns sorted list (desc by score).
    """
    ranks: list[RankItem] = []
    for inst in universe:
        tok, sym = inst["instrument_token"], inst["tradingsymbol"]
        try:
            # Ensure enough history for all filters & regressions
            min_history = max(MA_FILTER_100, LOOKBACK_R126 + 1, REG_LOOKBACK + 1)
            df = candles_df(k, tok, min_history)
            if df.empty:
                continue

            closes = df["close"]
            vols = df["volume"]
            if len(closes) < min_history:
                continue

            # Absolute-trend filter vs 100-EMA
            ema100 = pd.Series(closes).ewm(span=MA_FILTER_100, adjust=False).mean().iloc[-1]
            last = float(closes.iloc[-1])
            if last <= float(ema100):
                continue

            # 6m absolute momentum using strict lag
            if last <= float(closes.iloc[-(LOOKBACK_R126 + 1)]):
                continue

            # Liquidity filter (average 20-day volume)
            if float(vols.iloc[-20:].mean()) < MIN_VOLUME:
                continue

            # ATR/price volatility screen
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
def target_shares(k: KiteConnect, sym: str) -> int:
    """ATR-based risk parity sizing with MAX_WEIGHT cap.

    Size = (ACCOUNT_VALUE × RISK_FACTOR) / ATR
    Then cap by 10% notional per name.
    """
    tok = token_of(k, sym, "NSE")
    df = candles_df(k, tok, ATR_PERIOD)
    if len(df) <= ATR_PERIOD:
        raise ValueError("Not enough candles for ATR")
    atr_value = atr(df)
    if not atr_value or atr_value <= 0:
        raise ValueError("ATR zero")
    shares = (ACCOUNT_VALUE * RISK_FACTOR) / atr_value
    price = float(df["close"].iloc[-1])
    cap = (ACCOUNT_VALUE * MAX_WEIGHT) / price if price > 0 else 0
    return max(math.floor(min(shares, cap)), 0)


# ── 4 ▸ order wrappers ───────────────────────────────────────────────────
def safe_buy(k: KiteConnect, sym: str, qty: int) -> None:
    """Place a BUY order.

    • Uses true MARKET for normal EQ series
    • Uses marketable LIMIT (best ask) for series that ban MARKET (e.g., BE)
    """
    if not allow_kite_execution:
        logger.info("Skipping BUY for %s x %d (allow_kite_execution=False)", sym, qty)
        return
    if qty < MIN_SHARES:
        return

    srs = series_of(sym)

    if srs in NO_MARKET_SERIES:  # ---- BE, BZ, … path
        quote = k.quote([f"NSE:{sym}"])[f"NSE:{sym}"]
        # Hit the best ask; if depth is empty, fall back to last_price
        best_ask = quote["depth"]["sell"][0]["price"] if quote["depth"]["sell"] else quote["last_price"]
        k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=sym,
            transaction_type=k.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_LIMIT,
            price=best_ask,
            validity=k.VALIDITY_DAY,
        )

    else:  # ---- normal EQ path
        k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=sym,
            transaction_type=k.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_MARKET,
        )

    logger.info("BUY  %-10s x%4d", sym, qty)


def safe_sell(k: KiteConnect, sym: str, qty: int) -> None:
    """Place a SELL order matching logic from safe_buy() for series types."""
    if not allow_kite_execution:
        logger.info("Skipping SELL for %s x %d (allow_kite_execution=False)", sym, qty)
        return
    if qty < MIN_SHARES:
        return

    srs = series_of(sym)
    if srs in NO_MARKET_SERIES:  # ---------- BE etc.
        quote = k.quote([f"NSE:{sym}"])[f"NSE:{sym}"]
        best_bid = quote["depth"]["buy"][0]["price"] if quote["depth"]["buy"] else quote["last_price"]
        k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=sym,
            transaction_type=k.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_LIMIT,
            price=best_bid,
            validity=k.VALIDITY_DAY,
        )

    else:  # ---------- normal EQ path
        k.place_order(
            variety=k.VARIETY_REGULAR,
            exchange=k.EXCHANGE_NSE,
            tradingsymbol=sym,
            transaction_type=k.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=k.PRODUCT_CNC,
            order_type=k.ORDER_TYPE_MARKET,
        )
    logger.info("SELL %-10s x%4d", sym, qty)


# ── 5 ▸ exit & size-rebalance ────────────────────────────────────────────
def _trailing_stop_hit(k: KiteConnect, sym: str) -> bool:
    """3×ATR trailing stop using rolling highs over a modest window.

    Uses max(high) over max(ATR_PERIOD, 2*ATR_PERIOD). You can widen this
    window or track per-position peak since entry for a tighter approximation
    of "true" trailing stops.
    """
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
    """Exit conditions:
    - Not ranked any more, OR percentile rank worse than CUT_OFF_PCT, OR below 100-EMA
    - Plus: if Kite handle available and rank present, also check trailing stop
    """
    cond_rank = (rank is None) or (pct_rank > CUT_OFF_PCT) or (rank.close <= rank.ema100)
    if k is None or rank is None:
        return cond_rank
    return cond_rank or _trailing_stop_hit(k, rank.symbol)


def prune_portfolio(k: KiteConnect, ranks: list[RankItem], pf: dict[str, int]) -> None:
    """Sell names that violate exit rules. Guarded against empty ranking.

    If ranks is empty (e.g., network issue fetching constituents or candles),
    do nothing to avoid accidental liquidation.
    """
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
    """Every even ISO week (IST), rebalance sizes toward ATR targets.

    Sells reductions first, then buys increases subject to available cash and
    bull regime. Skips on odd ISO weeks to reduce churn.
    """
    if ist_now().isocalendar().week % 2:  # odd ISO week → skip
        logger.debug("Size rebalance skipped (odd week, IST)")
        return

    to_up, to_down = {}, {}
    for sym, qty in pf.items():
        try:
            tgt = target_shares(k, sym)
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

    cash = ACCOUNT_VALUE - live_value(k, pf)
    if not bull or cash <= 0:
        return

    prices = k.quote([f"NSE:{s}" for s in to_up]) if to_up else {}
    for sym, delta in to_up.items():
        cost = delta * prices[f"NSE:{sym}"]["last_price"]
        if cost > cash:
            continue  # don't stop scanning other candidates
        safe_buy(k, sym, delta)
        pf[sym] = pf.get(sym, 0) + delta
        cash -= cost


def series_of(ts: str) -> str:
    """Extract recognised 2-char NSE series code from a tradingsymbol.

    Falls back to 'EQ' if there is no suffix or the suffix is not recognised.
    """
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
    """Universe = NSE equity instruments whose base symbols are in NIFTY-500.

    Uses the global NIFTY500_SET filled at runtime by main().
    """
    return [
        i
        for i in kite.instruments("NSE")
        if (
            i.get("instrument_type") == "EQ"
            and i.get("segment") == "NSE"
            and base_symbol(str(i.get("tradingsymbol", ""))) in NIFTY500_SET
        )
    ]


# ═════════════════════════════════════════════════════════════════════════
# Main weekly routine
# ═════════════════════════════════════════════════════════════════════════
def main() -> None:
    # 1) Run only on the configured weekday in IST (0=Mon; default 2=Wed)
    if ist_now().weekday() != TRADING_WEEKDAY:
        logger.info("Not scheduled trading weekday (IST) – abort")
        return

    # 2) Load current portfolio & authenticate Kite
    portfolio = read_portfolio(PORTFOLIO_FILE)
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

    # 7) Size parity rebalance (every even ISO week in IST)
    resize_positions(kite, portfolio, bull)

    # 8) Cash left after re-sizing
    cash_left = ACCOUNT_VALUE - live_value(kite, portfolio)
    logger.info("Cash available: %.0f", cash_left)

    # 9) New buys if bull regime and cash available
    if bull and cash_left > 0:
        for r in ranks:
            if len(portfolio) >= MAX_POSITIONS:
                logger.info("Max positions reached (%d) – stopping new buys", MAX_POSITIONS)
                break
            if r.symbol in portfolio or r.symbol in sold_symbols:
                continue
            try:
                qty = target_shares(kite, r.symbol)
            except Exception as exc:
                logger.debug("size calc error %s – %s", r.symbol, exc)
                continue
            if qty < MIN_SHARES:
                continue
            cost = qty * r.close
            if cost > cash_left:
                continue  # don't stop scanning others
            safe_buy(kite, r.symbol, qty)
            portfolio[r.symbol] = qty
            cash_left -= cost
    else:
        logger.info("No new buys – bear regime or no cash.")

    # 10) Write next portfolio snapshot
    write_portfolio(OUT_FILE, portfolio)


if __name__ == "__main__":
    main()
