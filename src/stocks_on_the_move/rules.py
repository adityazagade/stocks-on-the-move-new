"""The strategy's rules as pure functions of a snapshot and the parameters (ADR-020, ADR-021).

Regime, the filter chain and the ranking, the exit rules with the trailing
stop, ATR sizing. Nothing here reads candles, prices, settings or the broker;
the pipeline gathers one ``Snapshot`` per instrument and hands it in, with
the price bands the entry rules read (ADR-034). A backtest calls the same
functions.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from stocks_on_the_move.indicators import Snapshot, SnapshotError
from stocks_on_the_move.params import StrategyParams
from stocks_on_the_move.universe import NO_BANDS, NO_MARKET_SERIES, NON_COMPLIANT_SERIES, PriceBands, series_of

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankItem:
    """One ranked instrument: its score, the regression it came from, and the closes the exit rules read.

    ``qualified`` is whether the entry filters let it be bought, and ``reason``
    the first one it failed if they did not (ADR-033). A disqualified name still
    takes the place its momentum earns it; only the buy step reads the flag.
    """

    symbol: str
    score: float
    annual_slope: float
    r2: float
    close: float
    ma100: float
    qualified: bool = True
    reason: str | None = None


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
    max_gap: float | None = None

    @property
    def status(self) -> str:
        """``ranked`` when it may be bought, ``disqualified`` when it has a score but failed an entry filter,
        ``excluded`` when there is no score to place it by at all (ADR-033)."""
        if self.rank is None:
            return "excluded"
        return "ranked" if self.rank.qualified else "disqualified"

    def row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "token": self.token,
            "status": self.status,
            "reason": self.reason,
            "last": self.last,
            "ma100": self.ma100,
            "avg_vol_20": self.avg_vol_20,
            "atr": self.atr,
            "atr_pct": self.atr_pct,
            "max_gap": self.max_gap,
        }


def band_disqualification(symbol: str, params: StrategyParams, bands: PriceBands) -> str | None:
    """``price_band`` when the daily band is below the floor; the series stands in when no bands came (ADR-034).

    With the floor at zero the rule is off. On the fallback rung — no fresh
    list and no copy — every series without market orders is refused as
    ``price_band:fallback``, stricter than the rule it stands in for and
    saying so. A name the list does not know passes: the list covers the
    universe, and a miss is a symbol quirk, not a band.
    """
    if params.min_price_band_pct <= 0:
        return None
    if bands.fallback:
        return "price_band:fallback" if series_of(symbol) in NO_MARKET_SERIES else None
    band = bands.band_of(symbol)
    if band is not None and band < params.min_price_band_pct:
        return "price_band"
    return None


def disqualification(snap: Snapshot, params: StrategyParams, bands: PriceBands = NO_BANDS) -> str | None:
    """The first entry filter a scoreable name fails, or ``None`` when it may be bought.

    In order: the issuer's series and the price band, which say whether the
    exchange will trade the name at all (ADR-034); then close above the trend
    average (ADR-024), 20-day volume, ATR as a fraction of price, the gap
    filter (ADR-025). These say whether a name is worth opening a position in;
    only the trend average and the gap rule also say anything about closing
    one (ADR-033).
    """
    if series_of(snap.symbol) in NON_COMPLIANT_SERIES:
        return "non_compliant"
    if (reason := band_disqualification(snap.symbol, params, bands)) is not None:
        return reason
    if snap.last <= snap.ma100:
        return "below_ma100"
    if snap.avg_vol_20 < params.min_volume:
        return "volume"
    if math.isnan(snap.atr) or (snap.last > 0 and snap.atr / snap.last > params.max_atr_pct):
        return "atr_pct"
    if not math.isnan(snap.max_gap) and snap.max_gap >= params.max_gap_pct:
        return "gap"
    return None


def evaluate(snap: Snapshot, params: StrategyParams, bands: PriceBands = NO_BANDS) -> Evaluation:
    """Score one snapshot, and say whether the entry filters let it be bought (ADR-033, ADR-034).

    A name is *rankable* when there is a momentum score to place it by. When
    there is not — the snapshot failed to build (``error:<type>``), the history
    is short (``history``), or the score came back ``nan``
    (``insufficient_data``) — it is excluded, carries no ``RankItem``, and a
    holding in that state exits as ``unranked:<cause>``. A rankable name always
    gets its ``RankItem``, with ``qualified`` and the first entry filter it
    failed, if any, recorded on it rather than used to drop it.
    """
    sym, tok = snap.symbol, snap.token
    last: float | None = None
    ma100: float | None = None
    avg_vol_20: float | None = None
    atr_value: float | None = None
    atr_pct: float | None = None
    max_gap: float | None = None

    def verdict(*, rank: RankItem | None = None, reason: str | None = None) -> Evaluation:
        return Evaluation(sym, tok, rank, reason, last, ma100, avg_vol_20, atr_value, atr_pct, max_gap)

    if snap.error is not None:
        return verdict(reason=f"error:{snap.error_type}")
    if not snap.enough_history:
        return verdict(reason="history")

    # Every metric, whatever fails: a disqualified name still reports its numbers in universe.csv
    last = snap.last
    ma100 = snap.ma100
    avg_vol_20 = snap.avg_vol_20
    atr_value = snap.atr
    atr_pct = atr_value / last if last > 0 else math.nan
    max_gap = snap.max_gap

    if math.isnan(snap.score):
        logger.warning("Not enough data to rank for %s", sym)
        return verdict(reason="insufficient_data")
    reason = disqualification(snap, params, bands)
    item = RankItem(
        sym,
        float(snap.score),
        float(snap.annual_slope),
        float(snap.r2),
        last,
        ma100,
        qualified=reason is None,
        reason=reason,
    )
    return verdict(rank=item, reason=reason)


def rank(evaluations: Iterable[Evaluation], params: StrategyParams) -> list[RankItem]:
    """The ranking list, best score first (ADR-033).

    Under ``rank_scope="universe"`` it is every name that has a score, each
    carrying whether it may be bought; under ``"qualified"``, the default, only
    the names that pass every entry filter, as the strategy has always ranked.
    """
    ranks = [e.rank for e in evaluations if e.rank is not None]
    if params.rank_scope == "qualified":
        ranks = [r for r in ranks if r.qualified]
    return sorted(ranks, key=lambda r: r.score, reverse=True)


def unrankable(evaluations: Iterable[Evaluation]) -> list[tuple[str, str]]:
    """``(symbol, reason)`` for the names with no score to place them by, for the tail of ranking.csv.

    They are listed, never scored: a fabricated score is a position in the
    ranking that was assigned rather than measured, and where it would land
    moves with the market (ADR-033, Option 4).
    """
    return [(e.symbol, e.reason or "unknown") for e in evaluations if e.rank is None]


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


def exit_check(
    snap: Snapshot | None,
    rank: RankItem | None,
    pct_rank: float,
    params: StrategyParams,
    *,
    unranked_cause: str | None = None,
) -> ExitCheck:
    """Every exit rule, evaluated: ``unranked``, ``rank_cutoff``, ``below_ma100``, ``gap``, ``trailing_stop``.

    All rules are checked so the artifact shows every reason; the decision is the
    OR of them. An unranked holding needs no snapshot; when the caller knows why
    it was excluded, the reason reads ``unranked:<cause>`` (ADR-025).

    The gap rule is stated here rather than inherited from the ranking, so that
    a gapped name that now takes its place in the ranking still exits on it
    (ADR-025's Option 4 stays rejected; ADR-033). The volume floor and the ATR
    ceiling are not here: both were chosen to keep a name out of a new position,
    neither as a reason to close a working one.
    """
    if rank is None:
        return ExitCheck((f"unranked:{unranked_cause}" if unranked_cause else "unranked",))
    reasons = []
    if pct_rank > params.cut_off_pct:
        reasons.append("rank_cutoff")
    if rank.close <= rank.ma100:
        reasons.append("below_ma100")
    if snap is not None and snap.error is None and not math.isnan(snap.max_gap) and snap.max_gap >= params.max_gap_pct:
        reasons.append("gap")
    hit, stop_level = trailing_stop(snap, params)
    if hit:
        reasons.append("trailing_stop")
    return ExitCheck(tuple(reasons), stop_level)
