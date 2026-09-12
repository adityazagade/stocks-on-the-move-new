"""Strategy constants and the pure computations on price series (ADR-020).

Nothing here touches the broker, the candle store or the settings.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# Fixed by the strategy, not configurable. Every environment knob is a field of
# settings.Settings (ADR-007), reached through the RunContext.
MIN_SHARES = 1


# scoring & filters
MA_PERIOD_200 = 200


MA_FILTER_100: int = 100


# Momentum lookbacks in trading days and their weights in the composite score (ADR-016).
# Shortened from the book's 21/63/126 before version control, for a reason nobody recorded;
# changing them again is a strategy ADR with the golden test (ADR-009) as its evidence.
LOOKBACK_SHORT = 5


LOOKBACK_MID = 15


LOOKBACK_LONG = 45


WEIGHT_SHORT = 0.6


WEIGHT_MID = 0.3


WEIGHT_LONG = 0.1


REG_LOOKBACK = 90


TRADING_DAYS_YR = 250


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


def annualise(slope_day: float) -> float:
    """Convert daily log-price slope to annualised simple return."""
    return math.exp(slope_day * TRADING_DAYS_YR) - 1.0


def _composite_momentum(closes: pd.Series):
    """Return (score, annual_slope, r2) or (nan, nan, nan) if insufficient data.

    score = (0.6·R5 + 0.3·R15 + 0.1·R45) × R²(90d): a weighted sum of the simple
    returns over LOOKBACK_SHORT, LOOKBACK_MID and LOOKBACK_LONG trading days, scaled
    by the R² of a log-linear fit over REG_LOOKBACK days. annual_slope is that fit's
    slope annualised.
    """
    need = max(LOOKBACK_LONG, REG_LOOKBACK) + 1
    if len(closes) < need:
        return math.nan, math.nan, math.nan

    last = float(closes.iloc[-1])
    r_short = (last / float(closes.iloc[-(LOOKBACK_SHORT + 1)])) - 1.0
    r_mid = (last / float(closes.iloc[-(LOOKBACK_MID + 1)])) - 1.0
    r_long = (last / float(closes.iloc[-(LOOKBACK_LONG + 1)])) - 1.0
    comp = WEIGHT_SHORT * r_short + WEIGHT_MID * r_mid + WEIGHT_LONG * r_long

    y = np.log(closes.iloc[-REG_LOOKBACK:])
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = intercept + slope * x
    ss_res = float(((y - y_hat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0

    return comp * r2, annualise(float(slope)), float(r2)


MIN_HISTORY = max(MA_FILTER_100, LOOKBACK_LONG + 1, REG_LOOKBACK + 1)
