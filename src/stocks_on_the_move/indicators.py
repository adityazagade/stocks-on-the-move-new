"""Pure computations on price series, and the snapshot that holds them (ADR-020, ADR-021).

Nothing here touches the broker, the candle store or the settings. A
``Snapshot`` is built once per instrument per run from its candle frame and
carries every derived value a rule reads, so the rules in ``rules.py`` are
functions of a snapshot and a ``StrategyParams`` and nothing else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stocks_on_the_move.params import StrategyParams


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


def annualise(slope_day: float, trading_days_yr: int = 250) -> float:
    """Convert daily log-price slope to annualised simple return."""
    return math.exp(slope_day * trading_days_yr) - 1.0


def composite_momentum(closes: pd.Series, params: StrategyParams) -> tuple[float, float, float]:
    """Return (score, annual_slope, r2) or (nan, nan, nan) if insufficient data.

    score = (w_short·R_short + w_mid·R_mid + w_long·R_long) × R²(reg_lookback): a
    weighted sum of the simple returns over the three lookbacks, scaled by the R²
    of a log-linear fit over ``reg_lookback`` days. annual_slope is that fit's
    slope annualised.
    """
    if len(closes) < params.score_history:
        return math.nan, math.nan, math.nan

    last = float(closes.iloc[-1])
    r_short = (last / float(closes.iloc[-(params.lookback_short + 1)])) - 1.0
    r_mid = (last / float(closes.iloc[-(params.lookback_mid + 1)])) - 1.0
    r_long = (last / float(closes.iloc[-(params.lookback_long + 1)])) - 1.0
    comp = params.weight_short * r_short + params.weight_mid * r_mid + params.weight_long * r_long

    y = np.log(closes.iloc[-params.reg_lookback :])
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = intercept + slope * x
    ss_res = float(((y - y_hat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0

    return comp * r2, annualise(float(slope), params.trading_days_yr), float(r2)


def _sma(closes: pd.Series, period: int) -> float:
    """The mean of the last ``period`` closes; ``nan`` when there are fewer (ADR-024)."""
    return float(closes.iloc[-period:].mean()) if len(closes) >= period else math.nan


def _max_gap(closes: pd.Series, lookback: int) -> float:
    """The largest absolute close-to-close daily return over the last ``lookback`` returns (ADR-025)."""
    tail = closes.iloc[-(lookback + 1) :]
    if len(tail) < 2:
        return math.nan
    return float(tail.pct_change().abs().iloc[1:].max())


class SnapshotError(RuntimeError):
    """A snapshot could not be built; ``str(exc)`` is ``"<Type>: <message>"`` of the cause."""


@dataclass(frozen=True)
class Snapshot:
    """Everything the rules read about one instrument, computed once from its candle frame (ADR-021).

    Fields are ``nan`` where the frame is too short for them. ``enough_history``
    is the filter chain's first rule. ``error`` is set, and every number ``nan``,
    when the frame could not be fetched or built; ``evaluate`` turns that into
    an ``error:<type>`` exclusion.
    """

    symbol: str
    token: int
    rows: int
    last: float
    ma100: float  # the trend filter's simple moving average, nan until trend_ma_period closes exist (ADR-024)
    ma200: float  # the regime's simple moving average, nan until regime_ma_period closes exist
    atr: float
    avg_vol_20: float
    rolling_high: float  # the highest close over the trailing stop's window
    max_gap: float  # the largest one-day close-to-close move over the gap window, nan with fewer than two closes
    score: float
    annual_slope: float
    r2: float
    enough_history: bool
    closes: np.ndarray = field(repr=False, compare=False, default_factory=lambda: np.array([], dtype=float))
    error: str | None = None

    @classmethod
    def from_candles(cls, symbol: str, token: int, frame: pd.DataFrame, params: StrategyParams) -> Snapshot:
        rows = len(frame)
        if rows == 0:
            return cls.empty(symbol, token)
        closes = frame["close"]
        score, slope, r2 = composite_momentum(closes, params)
        return cls(
            symbol=symbol,
            token=token,
            rows=rows,
            last=float(closes.iloc[-1]),
            ma100=_sma(closes, params.trend_ma_period),
            ma200=_sma(closes, params.regime_ma_period),
            atr=atr(frame, params.atr_period),
            avg_vol_20=float(frame["volume"].iloc[-20:].mean()),
            rolling_high=float(closes.rolling(params.stop_window).max().iloc[-1]),
            max_gap=_max_gap(closes, params.gap_lookback),
            score=score,
            annual_slope=slope,
            r2=r2,
            enough_history=rows >= params.history_days,
            closes=closes.to_numpy(dtype=float),
        )

    @classmethod
    def empty(cls, symbol: str, token: int, *, error: str | None = None) -> Snapshot:
        """No rows: every number ``nan``, nothing enough for any rule."""
        nan = math.nan
        return cls(
            symbol=symbol,
            token=token,
            rows=0,
            last=nan,
            ma100=nan,
            ma200=nan,
            atr=nan,
            avg_vol_20=nan,
            rolling_high=nan,
            max_gap=nan,
            score=nan,
            annual_slope=nan,
            r2=nan,
            enough_history=False,
            error=error,
        )

    @classmethod
    def failed(cls, symbol: str, token: int, exc: BaseException) -> Snapshot:
        return cls.empty(symbol, token, error=f"{type(exc).__name__}: {exc}")

    @property
    def error_type(self) -> str | None:
        return None if self.error is None else self.error.split(":", 1)[0]
