"""Tests for the backtest harness (ADR-023): replay data, run dates, overrides, simulation, metrics, the command."""

from __future__ import annotations

import csv
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from fakes import FakeBroker, make_candles, trending_closes
from stocks_on_the_move import backtest as bt
from stocks_on_the_move.broker import Instrument, Order
from stocks_on_the_move.params import StrategyParams

GOLDEN = Path(__file__).parent / "fixtures" / "golden"
WEDNESDAY = 2


def read_table(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def replay(tmp_path, make_settings):
    """The golden fixtures as a warm cache: 37 instruments, a year of candles, the NIFTY 500 list."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for f in (GOLDEN / "candles").glob("*.csv"):
        shutil.copy(f, cache / f.name)
    shutil.copy(GOLDEN / "instruments.csv", cache / bt.INSTRUMENTS_FILE)
    meta = json.loads((GOLDEN / "meta.json").read_text())
    settings = make_settings(**meta["settings"], cache_dir=cache, runs_dir=tmp_path / "runs")
    universe = set((GOLDEN / "nifty500.txt").read_text().split())
    instruments = bt.load_instruments(cache / bt.INSTRUMENTS_FILE)
    wanted = bt.universe_instruments(instruments, universe, settings)
    candles = bt.ReplayCandles.load(cache, [i.token for i in wanted])
    index_token = next(i.token for i in wanted if i.tradingsymbol == settings.index_symbol)
    return settings, instruments, universe, candles, index_token


def build(replay, start: date, end: date, label: str = "test", overrides=()) -> bt.Backtest:
    settings, instruments, universe, candles, index_token = replay
    dates = bt.run_dates(candles.trading_days(index_token), start, end, settings.trading_weekday)
    params = bt.parse_overrides(overrides, StrategyParams.from_settings(settings))
    return bt.Backtest(settings, params, frozenset(universe), instruments, candles, dates, label)


# ── data ─────────────────────────────────────────────────────────────────


def test_replay_candles_never_show_the_run_date_or_anything_after_it(replay):
    _, _, _, candles, index_token = replay
    for d in (date(2026, 8, 5), date(2026, 9, 16), date(2026, 9, 23)):
        candles.as_of = d
        frame = candles.get(index_token, 200)
        last = frame["date"].max().date()
        assert last < d and len(frame) >= 200
    candles.as_of = None
    assert candles.get(index_token, 200).empty


def test_replay_broker_prices_at_the_run_dates_close_and_sends_nothing(replay):
    _, instruments, _, candles, index_token = replay
    broker = bt.ReplayBroker(instruments, candles)
    assert broker.ltp(["NSE:ALPHA"]) == {}  # no run date yet
    broker.as_of = date(2026, 9, 16)
    price = broker.ltp(["NSE:ALPHA", "NSE:NOPE"])
    assert set(price) == {"NSE:ALPHA"} and price["NSE:ALPHA"] == candles.close_on(1001, date(2026, 9, 16))
    assert broker.quote(["NSE:ALPHA"])["NSE:ALPHA"].best_bid == price["NSE:ALPHA"]
    assert broker.historical_data(1001, date(2026, 1, 1), date(2026, 2, 1)) == []
    assert [i.tradingsymbol for i in broker.instruments("NSE")][:2] == ["ALPHA", "BRAVO"]
    with pytest.raises(RuntimeError, match="sends no orders"):
        broker.place_order(Order("ALPHA", "BUY", 1, "MARKET"))


def test_run_dates_follow_the_weekday_and_move_to_the_next_trading_day():
    trading = [date(2026, 9, d) for d in (1, 2, 3, 4, 7, 8, 10, 11, 21, 22, 23, 24)]  # 9 Sep shut; 14 to 18 shut
    dates = bt.run_dates(trading, date(2026, 9, 1), date(2026, 9, 30), WEDNESDAY)
    assert dates == [date(2026, 9, 2), date(2026, 9, 10), date(2026, 9, 23)]
    assert bt.run_dates(trading, date(2026, 10, 1), date(2026, 10, 31), WEDNESDAY) == []


# ── overrides ────────────────────────────────────────────────────────────


def test_overrides_are_typed_by_the_field_they_name():
    params = bt.parse_overrides(["lookback_short=21", "weight_short=0.5", "atr_period=14"], StrategyParams())
    assert (params.lookback_short, params.weight_short, params.atr_period) == (21, 0.5, 14)
    assert params.lookback_mid == 15
    with pytest.raises(ValueError, match="choose one of"):
        bt.parse_overrides(["nope=1"], StrategyParams())
    with pytest.raises(ValueError, match="expected int"):
        bt.parse_overrides(["lookback_short=x"], StrategyParams())


# ── the simulation ───────────────────────────────────────────────────────


def test_a_replay_over_the_fixtures_trades_carries_state_and_writes_its_files(replay, tmp_path):
    run = build(replay, date(2026, 8, 5), date(2026, 9, 23))
    assert len(run.dates) == 8 and run.dates[0] == date(2026, 8, 5) and run.dates[-1] == date(2026, 9, 23)

    summary = bt.simulate(run, tmp_path / "out")

    equity = read_table(tmp_path / "out" / "equity.csv")
    weekly = read_table(tmp_path / "out" / "weekly.csv")
    trades = read_table(tmp_path / "out" / "trades.csv")
    assert [r["date"] for r in equity] == [d.isoformat() for d in run.dates]
    assert float(equity[0]["cash"]) < run.settings.starting_cash  # the first date bought something
    assert int(equity[-1]["positions"]) > 0 and float(equity[-1]["exposure"]) > 0
    assert {r["bull"] for r in weekly} == {"true"} and all(int(r["ranked"]) > 0 for r in weekly)
    assert trades and {t["reason"] for t in trades} >= {"new_position"}
    assert all(t["date"] in {d.isoformat() for d in run.dates} for t in trades)
    assert summary["weeks"] == 8 and summary["trades"] == len(trades) and summary["note"] == bt.SUMMARY_NOTE
    assert set(bt.COMPARE_FIELDS) <= set(summary)
    params = json.loads((tmp_path / "out" / "params.json").read_text())
    assert params["params"]["lookback_short"] == 5 and "KITE_API_KEY" not in params["settings"]
    assert json.loads((tmp_path / "out" / "summary.json").read_text()) == summary
    # the state files it carried between dates stayed inside its own directory
    assert (tmp_path / "out" / "state" / "trades_ledger.csv").exists()
    assert not (tmp_path / "trades_ledger.csv").exists() and not (tmp_path / "next_portfolio.csv").exists()


def test_a_replay_is_deterministic(replay, tmp_path):
    a = bt.simulate(build(replay, date(2026, 8, 5), date(2026, 9, 9)), tmp_path / "a")
    b = bt.simulate(build(replay, date(2026, 8, 5), date(2026, 9, 9)), tmp_path / "b")
    assert a == b
    for name in ("equity.csv", "weekly.csv", "trades.csv", "params.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_an_override_changes_the_replay(replay, tmp_path):
    base = bt.simulate(build(replay, date(2026, 8, 5), date(2026, 9, 9)), tmp_path / "base")
    other = bt.simulate(
        build(replay, date(2026, 8, 5), date(2026, 9, 9), "long", ["lookback_short=21"]), tmp_path / "long"
    )
    assert json.loads((tmp_path / "long" / "params.json").read_text())["params"]["lookback_short"] == 21
    assert base["weeks"] == other["weeks"]


# ── metrics ──────────────────────────────────────────────────────────────


def week(day: int, equity: float, positions: int = 5) -> bt.WeekResult:
    return bt.WeekResult(
        date(2026, 1, 1) + __import__("datetime").timedelta(days=day),
        0.0,
        equity,
        equity,
        positions,
        1.0,
        True,
        10,
        0,
        0,
        False,
    )


def test_metrics_on_a_known_series():
    weeks = [week(0, 100.0), week(7, 110.0), week(14, 99.0), week(21, 120.0)]
    m = bt.metrics(weeks, [{"qty": 10, "price": 12.0}])
    assert m["cagr"] == pytest.approx(1.2 ** (365 / 21) - 1, rel=1e-6)
    assert m["max_drawdown"] == pytest.approx(99 / 110 - 1, rel=1e-6)
    assert (m["max_drawdown_peak"], m["max_drawdown_trough"]) == ("2026-01-08", "2026-01-15")
    assert m["weeks"] == 4 and m["trades"] == 1 and m["avg_positions"] == 5.0
    assert m["annual_volatility"] > 0 and m["return_over_volatility"] > 0


def test_metrics_on_one_flat_week_do_not_divide_by_zero():
    m = bt.metrics([week(0, 100.0)], [])
    assert m["cagr"] == 0.0 and m["annual_volatility"] == 0.0 and m["return_over_volatility"] is None
    assert m["max_drawdown"] == 0.0 and m["turnover_per_year"] == 0.0


# ── warm-up ──────────────────────────────────────────────────────────────


def test_warm_cache_chunks_long_requests_and_merges_into_the_cache(make_settings, tmp_path):
    settings = make_settings(cache_dir=tmp_path / "cache", candle_sleep_sec=0.0)
    broker = FakeBroker()
    broker.add_equity("AAA", 1, trending_closes(30), end=date(2026, 9, 12))
    broker._instruments.append(Instrument(256265, "NIFTY 50", "NSE", "INDICES", "EQ"))
    broker.candles[256265] = make_candles(trending_closes(30, start=20_000.0), end=date(2026, 9, 12))
    broker.add_equity("OUTSIDE", 2, trending_closes(30), end=date(2026, 9, 12))  # not in the universe

    counts = bt.warm_cache(broker, settings, {"AAA"}, history_days=4000, today=date(2026, 9, 12), sleep=lambda _: None)

    fetched = [c for c in broker.calls if c[0] == "historical_data"]
    assert counts == {"instruments": 3, "tokens": 2, "requests": 6}  # 4000 days is three chunks per token
    assert {c[1][0] for c in fetched} == {1, 256265}
    assert all((c[1][2] - c[1][1]).days < bt.CHUNK_DAYS for c in fetched)
    cached = read_table(tmp_path / "cache" / "1.csv")
    assert len(cached) == 30 and cached[-1]["date"].startswith("2026-09-11")  # 12 Sep 2026 is a Saturday
    assert not (tmp_path / "cache" / "2.csv").exists()
    assert [i.tradingsymbol for i in bt.load_instruments(bt.instruments_path(settings))] == [
        "AAA",
        "NIFTY 50",
        "OUTSIDE",
    ]

    bt.warm_cache(broker, settings, {"AAA"}, history_days=10, today=date(2026, 9, 12), sleep=lambda _: None)
    assert len(read_table(tmp_path / "cache" / "1.csv")) == 30  # a second warm merges, never duplicates


# ── the command ──────────────────────────────────────────────────────────


def test_compare_table_lines_up_the_summaries():
    a = {"label": "a", "from": "2021-01-06", "to": "2026-09-09", "weeks": 296, "cagr": 0.1234, "max_drawdown": -0.2}
    b = {"label": "book", "from": "2021-01-06", "to": "2026-09-09", "weeks": 296, "cagr": 0.2, "max_drawdown": -0.15}
    table = bt.compare_table([a, b]).splitlines()
    assert table[0].split() == ["a", "book"]
    cagr = next(line for line in table if line.startswith("cagr"))
    assert cagr.split() == ["cagr", "0.1234", "0.2000"]


def test_run_refuses_without_a_warm_cache(make_settings, tmp_path):
    settings = make_settings(cache_dir=tmp_path / "empty")
    with pytest.raises(FileNotFoundError, match="warm"):
        bt._build(settings, date(2026, 1, 1), date(2026, 2, 1), "x", [])
