"""Tests for the pure rules over snapshots (ADR-021): no broker, no candle store, no settings."""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd
import pytest

from fakes import EVEN_WEEK_WEDNESDAY, make_candles, trending_closes
from stocks_on_the_move.indicators import Snapshot, SnapshotError, annualise, composite_momentum
from stocks_on_the_move.params import StrategyParams
from stocks_on_the_move.rules import (
    ExitCheck,
    RankItem,
    evaluate,
    exit_check,
    rank,
    regime,
    size,
    trailing_stop,
    unrankable,
)
from stocks_on_the_move.settings import Settings

TODAY = EVEN_WEEK_WEDNESDAY.date()
P = StrategyParams()


def snapshot(symbol: str, closes, *, volume: int = 1_000_000, spread: float = 0.01, params=P) -> Snapshot:
    frame = pd.DataFrame(make_candles(closes, end=TODAY, volume=volume, spread=spread))
    return Snapshot.from_candles(symbol, 1, frame, params)


def rank_item(symbol: str, *, close: float = 100.0, ma100: float = 90.0) -> RankItem:
    return RankItem(symbol, 0.5, 0.3, 0.9, close, ma100)


# ── parameters ───────────────────────────────────────────────────────────


def test_params_from_default_settings_equal_the_code_defaults():
    settings = Settings.from_values(kite_api_key="k", kite_api_secret="s")
    assert StrategyParams.from_settings(settings) == P


def test_params_carry_the_settings_knobs(make_settings):
    params = StrategyParams.from_settings(make_settings(atr_period=14, cut_off_pct=0.3, max_atr_pct=0.2))
    assert (params.atr_period, params.cut_off_pct, params.max_atr_pct) == (14, 0.3, 0.2)
    assert params.lookback_short == 21  # the code's constants are untouched by settings


def test_derived_windows():
    # the long lookback drives both windows: max(100, 126 + 1, 90 + 1) and max(126, 90) + 1
    assert P.history_days == 127 and P.score_history == 127 and P.stop_window == 40


# ── the snapshot ─────────────────────────────────────────────────────────


def test_snapshot_fields_on_a_full_frame():
    snap = snapshot("AAA", trending_closes(150, daily=0.002))
    assert snap.rows == 150 and snap.enough_history and snap.error is None
    assert snap.last == pytest.approx(trending_closes(150, daily=0.002)[-1])
    assert snap.last > snap.ma100 > 0
    assert snap.atr > 0 and snap.avg_vol_20 == 1_000_000
    assert snap.rolling_high == pytest.approx(snap.last)  # an uptrend's highest close is the last one
    assert snap.r2 == pytest.approx(1.0) and snap.score > 0
    assert len(snap.closes) == 150


def test_snapshot_measures_the_largest_one_day_move_over_the_gap_window():
    steady = trending_closes(150, daily=0.001)
    assert snapshot("STEADY", steady).max_gap == pytest.approx(math.exp(0.001) - 1)
    jumped = steady[:-50] + [c * 1.2 for c in steady[-50:]]  # a 20 % gap fifty days ago
    assert snapshot("JUMPED", jumped).max_gap == pytest.approx(1.2 * math.exp(0.001) - 1)
    old_jump = steady[:-95] + [c * 1.2 for c in steady[-95:]]  # ninety-five days ago: outside the window
    assert snapshot("OLD", old_jump).max_gap == pytest.approx(math.exp(0.001) - 1)
    assert math.isnan(snapshot("ONE", steady[:1]).max_gap)


def test_snapshot_marks_a_short_frame_and_leaves_the_windows_it_cannot_fill_nan():
    snap = snapshot("SHORT", trending_closes(30))
    assert snap.rows == 30 and not snap.enough_history
    assert not math.isnan(snap.last) and not math.isnan(snap.atr)
    assert math.isnan(snap.rolling_high)  # fewer than 40 closes
    assert math.isnan(snap.score) and math.isnan(snap.r2)  # fewer than 91 closes


def test_an_empty_frame_and_a_failed_build():
    empty = Snapshot.from_candles("NONE", 1, pd.DataFrame(), P)
    assert empty.rows == 0 and math.isnan(empty.last) and not empty.enough_history
    failed = Snapshot.failed("X", 1, RuntimeError("boom"))
    assert (failed.error, failed.error_type) == ("RuntimeError: boom", "RuntimeError")


# ── the composite score ──────────────────────────────────────────────────


def test_composite_momentum_perfect_log_linear_uptrend():
    closes = pd.Series(100.0 * np.exp(0.001 * np.arange(P.score_history + 10)))
    score, ann, r2 = composite_momentum(closes, P)
    assert r2 == pytest.approx(1.0)
    assert ann == pytest.approx(annualise(0.001, P.trading_days_yr))
    assert score > 0


def test_the_slope_score_is_the_annualised_slope_times_r2():
    closes = pd.Series(100.0 * np.exp(0.001 * np.arange(P.score_history + 10)))
    # "slope" is the default, so name both scores explicitly rather than leaning on P
    blend, ann, r2 = composite_momentum(closes, dataclasses.replace(P, score="blend"))
    slope, ann_again, r2_again = composite_momentum(closes, dataclasses.replace(P, score="slope"))
    assert (ann_again, r2_again) == (ann, r2)  # the switch changes the score and nothing else
    assert slope == pytest.approx(ann * r2) and slope != blend
    assert P.score == "slope"  # the live default


def test_composite_momentum_insufficient_data():
    assert all(math.isnan(v) for v in composite_momentum(pd.Series(np.linspace(100, 110, 20)), P))


# ── regime ───────────────────────────────────────────────────────────────


def test_regime_is_bull_above_the_long_ema_and_bear_below():
    up = snapshot("NIFTY 50", trending_closes(260, start=20_000.0, daily=0.001))
    down = snapshot("NIFTY 50", trending_closes(260, start=20_000.0, daily=-0.001))
    assert regime(up, P) == regime(up, P)  # deterministic
    assert regime(up, P).bull is True and regime(up, P).last > regime(up, P).ma200
    assert regime(down, P).bull is False
    with pytest.raises(ValueError, match="not enough index candles"):
        regime(snapshot("NIFTY 50", trending_closes(150)), P)


# ── the filter chain ─────────────────────────────────────────────────────


def test_evaluate_names_the_entry_rule_that_disqualified():
    """Each filter names itself, in order, and the name is still scored and ranked (ADR-033)."""
    falling = evaluate(snapshot("FALLING", trending_closes(150, daily=-0.003)), P)
    assert falling.reason == "below_ma100"
    assert falling.last is not None and falling.ma100 is not None and falling.last < falling.ma100
    thin = evaluate(snapshot("THIN", trending_closes(150), volume=100), P)
    assert thin.reason == "volume" and thin.avg_vol_20 == 100
    wild = evaluate(snapshot("WILD", trending_closes(150), spread=0.25), P)
    assert wild.reason == "atr_pct" and wild.atr_pct is not None and wild.atr_pct > P.max_atr_pct
    steady = trending_closes(150, daily=0.002)
    gapped = evaluate(snapshot("GAPPED", steady[:-30] + [c * 1.2 for c in steady[-30:]]), P)
    assert gapped.reason == "gap" and gapped.max_gap is not None and gapped.max_gap >= P.max_gap_pct

    # every one of them is scored, ranked and flagged, not dropped
    for verdict in (falling, thin, wild, gapped):
        assert verdict.rank is not None, verdict.symbol
        assert verdict.rank.qualified is False and verdict.rank.reason == verdict.reason
        assert verdict.status == "disqualified" and verdict.row()["status"] == "disqualified"

    good = evaluate(snapshot("GOOD", steady), P)
    assert good.reason is None and good.rank is not None and good.rank.symbol == "GOOD"
    assert good.rank.qualified is True and good.rank.reason is None
    assert good.row()["status"] == "ranked" and good.atr_pct is not None and good.atr_pct < P.max_atr_pct
    assert good.max_gap is not None and good.max_gap < P.max_gap_pct
    off = evaluate(
        snapshot("GAPPED", steady[:-30] + [c * 1.2 for c in steady[-30:]]), dataclasses.replace(P, max_gap_pct=1.0)
    )
    assert off.reason is None  # 1 disables the rule


def test_a_disqualified_name_still_reports_every_metric():
    """The chain no longer stops at the first failure, so universe.csv is filled in whatever failed (ADR-033)."""
    falling = evaluate(snapshot("FALLING", trending_closes(150, daily=-0.003)), P)
    assert falling.avg_vol_20 is not None and falling.atr is not None
    assert falling.atr_pct is not None and falling.max_gap is not None


def test_a_name_with_no_score_is_excluded_and_never_ranked():
    """No score means no place in the ranking: it is listed, not measured (ADR-033)."""
    short = evaluate(snapshot("SHORT", trending_closes(10)), P)
    assert (short.reason, short.last, short.rank) == ("history", None, None)
    assert short.status == "excluded"

    failed = evaluate(Snapshot.failed("X", 1, RuntimeError("boom")), P)
    assert failed.reason == "error:RuntimeError" and failed.rank is None
    assert failed.row()["status"] == "excluded"

    assert unrankable([short, failed]) == [("SHORT", "history"), ("X", "error:RuntimeError")]


def test_rank_orders_by_score_and_scopes_the_list():
    good = evaluate(snapshot("GOOD", trending_closes(150, daily=0.002)), P)
    better = evaluate(snapshot("BETTER", trending_closes(150, daily=0.004)), P)
    out = evaluate(snapshot("OUT", trending_closes(10)), P)  # no score at all
    falling = evaluate(snapshot("FALLING", trending_closes(150, daily=-0.003)), P)  # scored, disqualified
    evaluations = [good, out, better, falling]

    # the qualified scope is the opt-out: only what may be bought
    qualified_only = rank(evaluations, dataclasses.replace(P, rank_scope="qualified"))
    assert [r.symbol for r in qualified_only] == ["BETTER", "GOOD"]

    # the default ranks the disqualified name too, on its own momentum, flagged
    whole = rank(evaluations, P)
    assert [r.symbol for r in whole] == ["BETTER", "GOOD", "FALLING"]
    assert [r.qualified for r in whole] == [True, True, False]
    assert whole[-1].reason == "below_ma100"
    # OUT is in neither list under either scope: there is no score to place it by
    assert "OUT" not in {r.symbol for r in whole}


def test_a_disqualified_name_is_ranked_by_momentum_not_pushed_to_the_end():
    """The point of ADR-033: position is measured, never assigned."""
    strong_but_gapped = trending_closes(150, daily=0.002)
    strong_but_gapped = strong_but_gapped[:-30] + [c * 1.2 for c in strong_but_gapped[-30:]]
    gapped = evaluate(snapshot("GAPPED", strong_but_gapped), P)
    weak = evaluate(snapshot("WEAK", trending_closes(150, daily=0.0002)), P)
    assert gapped.rank is not None and gapped.rank.qualified is False

    whole = rank([weak, gapped], dataclasses.replace(P, rank_scope="universe"))
    # the gapped name outscores the weak one, so it ranks above it despite being unbuyable
    assert [r.symbol for r in whole] == ["GAPPED", "WEAK"]


# ── exits ────────────────────────────────────────────────────────────────


def test_trailing_stop_fires_after_a_collapse_and_not_in_an_uptrend():
    steady = trending_closes(120, daily=0.001)
    up = snapshot("UP", steady)
    down = snapshot("DOWN", steady[:-10] + [c * 0.5 for c in steady[-10:]])
    assert trailing_stop(up, P)[0] is False
    hit, stop_level = trailing_stop(down, P)
    assert hit is True and stop_level > steady[-1] * 0.5  # the last close sits under the stop


def test_trailing_stop_needs_a_snapshot_with_enough_candles():
    assert trailing_stop(None, P) == (False, None)
    assert trailing_stop(snapshot("NEW", trending_closes(P.atr_period)), P) == (False, None)
    assert trailing_stop(Snapshot.failed("X", 1, KeyError("no token")), P) == (False, None)


def test_exit_check_lists_every_rule_that_fired():
    steady = trending_closes(120, daily=0.001)
    up = snapshot("UP", steady)
    down = snapshot("DOWN", steady[:-10] + [c * 0.5 for c in steady[-10:]])

    assert exit_check(None, None, 0.1, P) == ExitCheck(("unranked",))
    assert exit_check(None, None, 0.1, P, unranked_cause="gap") == ExitCheck(("unranked:gap",))
    hold = exit_check(up, rank_item("UP", close=200.0, ma100=150.0), 0.1, P)
    assert hold.reasons == () and hold.sell is False and hold.stop_level is not None
    both = exit_check(up, rank_item("UP", close=90.0, ma100=90.0), 0.9, P)
    assert both.reasons == ("rank_cutoff", "below_ma100")
    # the collapse halves the close in a day, so the gap rule fires beside the stop
    stopped = exit_check(down, rank_item("DOWN", close=200.0, ma100=150.0), 0.1, P)
    assert stopped.reasons == ("gap", "trailing_stop") and stopped.sell is True


def test_the_gap_rule_exits_a_ranked_holding():
    """A gapped name now takes its place in the ranking, so the exit is stated rather than inherited (ADR-033)."""
    steady = trending_closes(150, daily=0.002)
    gapped = snapshot("GAPPED", steady[:-30] + [c * 1.2 for c in steady[-30:]])
    item = rank_item("GAPPED", close=gapped.last, ma100=gapped.ma100)
    assert exit_check(gapped, item, 0.1, P).reasons == ("gap",)
    # and the knob still turns it off
    off = dataclasses.replace(P, max_gap_pct=1.0)
    assert exit_check(gapped, item, 0.1, off).reasons == ()


def test_volume_and_atr_do_not_exit_a_ranked_holding():
    """Both were chosen to keep a name out of a new position, neither to close a working one (ADR-033)."""
    thin = snapshot("THIN", trending_closes(150, daily=0.002), volume=100)
    wild = snapshot("WILD", trending_closes(150, daily=0.002), spread=0.25)
    assert evaluate(thin, P).reason == "volume" and evaluate(wild, P).reason == "atr_pct"
    for snap in (thin, wild):
        item = rank_item(snap.symbol, close=snap.last, ma100=snap.ma100)
        assert exit_check(snap, item, 0.1, P).reasons == ()


# ── sizing ───────────────────────────────────────────────────────────────


def test_size_is_the_floor_of_the_smaller_quantity():
    snap = snapshot("AAA", trending_closes(60, start=100.0))
    sizing = size(snap, 100_000.0, P)
    assert sizing.price == snap.last and sizing.atr == snap.atr
    assert sizing.risk_qty == pytest.approx(100_000 * P.risk_factor / sizing.atr)
    assert sizing.cap_qty == pytest.approx(100_000 * P.max_weight / sizing.price)
    assert sizing.target_qty == int(min(sizing.risk_qty, sizing.cap_qty))


def test_size_refuses_a_short_frame_and_a_failed_snapshot():
    with pytest.raises(ValueError, match="Not enough candles"):
        size(snapshot("NEW", trending_closes(P.atr_period)), 100_000.0, P)
    with pytest.raises(SnapshotError, match="KeyError"):
        size(Snapshot.failed("GHOST", 0, KeyError("Cannot resolve instrument_token for NSE:GHOST")), 1.0, P)
