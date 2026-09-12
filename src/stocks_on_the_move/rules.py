"""The strategy's rules: regime, filter chain and ranking, exit rules, ATR sizing (ADR-020).

Every rule still takes the ``RunContext`` and reads candles through it; ADR-021
makes them pure functions of a snapshot.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from stocks_on_the_move.broker import Instrument
from stocks_on_the_move.context import RunContext, token_of
from stocks_on_the_move.indicators import (
    LOOKBACK_LONG,
    MA_FILTER_100,
    MA_PERIOD_200,
    MIN_HISTORY,
    _composite_momentum,
    atr,
)
from stocks_on_the_move.reporting import UNIVERSE_COLUMNS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankItem:
    """One ranked instrument: its score, the regression it came from, and the closes the exit rules read."""

    symbol: str
    score: float
    annual_slope: float
    r2: float
    close: float
    ema100: float


def index_trend(ctx: RunContext) -> tuple[bool, float, float]:
    """Return (is_bull, last_close, ema200) for the chosen index."""
    tok = token_of(ctx)
    closes = ctx.candles.get(tok, MA_PERIOD_200)["close"]
    if len(closes) < MA_PERIOD_200:
        raise ValueError("not enough index candles for EMA-200")
    ema200 = pd.Series(closes).ewm(span=MA_PERIOD_200, adjust=False).mean().iloc[-1]
    last = float(closes.iloc[-1])
    return last > float(ema200), float(last), float(ema200)


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


def _trailing_stop(ctx: RunContext, sym: str) -> tuple[bool, float | None]:
    """(hit, stop level) for the n×ATR trailing stop under the rolling high close."""
    s = ctx.settings
    tok = token_of(ctx, sym, "NSE")
    look = max(s.atr_period, LOOKBACK_LONG)
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
