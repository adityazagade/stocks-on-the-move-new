"""The strategy's rules as pure functions of a snapshot and the parameters (ADR-020, ADR-021).

Regime, the filter chain and the ranking, the exit rules with the trailing
stop, ATR sizing. Nothing here reads candles, prices, settings or the broker;
the pipeline gathers one ``Snapshot`` per instrument and hands it in. A
backtest calls the same functions.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from stocks_on_the_move.indicators import Snapshot, SnapshotError
from stocks_on_the_move.params import StrategyParams

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankItem:
    """One ranked instrument: its score, the regression it came from, and the closes the exit rules read."""

    symbol: str
    score: float
    annual_slope: float
    r2: float
    close: float
    ma100: float


# ── 1 ▸ index regime ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class Regime:
    """The index against its long moving average: ``bull`` gates new buys, never sells."""

    bull: bool
    last: float
    ma200: float


def regime(index: Snapshot, params: StrategyParams) -> Regime:
    """Bull when the index's last close is above its ``regime_ma_period``-day simple moving average (ADR-024)."""
    if index.rows < params.regime_ma_period:
        raise ValueError("not enough index candles for the 200-day average")
    return Regime(index.last > index.ma200, index.last, index.ma200)


# ── 2 ▸ filter chain and ranking ─────────────────────────────────────────
@dataclass(frozen=True)
class Evaluation:
    """Why an instrument was ranked or excluded, with the metrics known at that point (universe.csv)."""

    symbol: str
    token: int
    rank: RankItem | None = None
    reason: str | None = None
    last: float | None = None
    ma100: float | None = None
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
            "ma100": self.ma100,
            "avg_vol_20": self.avg_vol_20,
            "atr": self.atr,
            "atr_pct": self.atr_pct,
        }


def evaluate(snap: Snapshot, params: StrategyParams) -> Evaluation:
    """Run the filter chain on one snapshot and name the rule that stopped it, if any.

    Order of the rules, unchanged: enough history, close above the trend average,
    20-day volume, ATR as a fraction of price, then the momentum score. A
    snapshot that could not be built is an ``error:<type>`` exclusion.
    """
    sym, tok = snap.symbol, snap.token
    last: float | None = None
    ma100: float | None = None
    avg_vol_20: float | None = None
    atr_value: float | None = None
    atr_pct: float | None = None

    def verdict(*, rank: RankItem | None = None, reason: str | None = None) -> Evaluation:
        return Evaluation(sym, tok, rank, reason, last, ma100, avg_vol_20, atr_value, atr_pct)

    if snap.error is not None:
        return verdict(reason=f"error:{snap.error_type}")
    if not snap.enough_history:
        return verdict(reason="history")
    ma100 = snap.ma100
    last = snap.last
    if last <= ma100:
        return verdict(reason="below_ma100")
    avg_vol_20 = snap.avg_vol_20
    if avg_vol_20 < params.min_volume:
        return verdict(reason="volume")
    atr_value = snap.atr
    atr_pct = atr_value / last if last > 0 else math.nan
    if math.isnan(atr_value) or (last > 0 and atr_value / last > params.max_atr_pct):
        return verdict(reason="atr_pct")
    if math.isnan(snap.score):
        logger.warning("Not enough data to rank for %s", sym)
        return verdict(reason="insufficient_data")
    return verdict(rank=RankItem(sym, float(snap.score), float(snap.annual_slope), float(snap.r2), last, ma100))


def rank(evaluations: Iterable[Evaluation]) -> list[RankItem]:
    """The ranked names, best score first."""
    ranks = [e.rank for e in evaluations if e.rank is not None]
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


def size(snap: Snapshot, account_equity: float, params: StrategyParams) -> Sizing:
    """ATR-based risk parity sizing with the weight cap on dynamic equity.

    risk_qty = account_equity × risk_factor / ATR; cap_qty = account_equity × max_weight / price;
    target_qty = floor(min(risk_qty, cap_qty)), never negative.
    """
    if snap.error is not None:
        raise SnapshotError(snap.error)
    if snap.rows <= params.atr_period:
        raise ValueError("Not enough candles for ATR")
    atr_value = snap.atr
    if not atr_value or atr_value <= 0:
        raise ValueError("ATR zero")
    risk_qty = (account_equity * params.risk_factor) / atr_value
    price = snap.last
    cap_qty = (account_equity * params.max_weight) / price if price > 0 else 0.0
    return Sizing(price, atr_value, risk_qty, cap_qty, max(math.floor(min(risk_qty, cap_qty)), 0))


# ── 4 ▸ exit rules ───────────────────────────────────────────────────────
def trailing_stop(snap: Snapshot | None, params: StrategyParams) -> tuple[bool, float | None]:
    """(hit, stop level) for the n×ATR trailing stop under the rolling high close."""
    if snap is None or snap.error is not None or snap.rows < params.atr_period + 1:
        logger.warning("Not enough candles for trailing stop")
        return False, None
    if math.isnan(snap.atr):
        return False, None
    stop_level = snap.rolling_high - params.exit_multiple * snap.atr
    return snap.last < stop_level, stop_level


@dataclass(frozen=True)
class ExitCheck:
    """Which exit rules fired for a holding (exits.csv); ``sell`` when any did."""

    reasons: tuple[str, ...]
    stop_level: float | None = None

    @property
    def sell(self) -> bool:
        return bool(self.reasons)


def exit_check(snap: Snapshot | None, rank: RankItem | None, pct_rank: float, params: StrategyParams) -> ExitCheck:
    """Every exit rule, evaluated: ``unranked``, ``rank_cutoff``, ``below_ma100``, ``trailing_stop``.

    All rules are checked so the artifact shows every reason; the decision is the
    OR of them. An unranked holding needs no snapshot.
    """
    if rank is None:
        return ExitCheck(("unranked",))
    reasons = []
    if pct_rank > params.cut_off_pct:
        reasons.append("rank_cutoff")
    if rank.close <= rank.ma100:
        reasons.append("below_ma100")
    hit, stop_level = trailing_stop(snap, params)
    if hit:
        reasons.append("trailing_stop")
    return ExitCheck(tuple(reasons), stop_level)
