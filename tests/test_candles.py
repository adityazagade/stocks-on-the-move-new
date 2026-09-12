"""Tests for the candle store's cache paths (ADR-008)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from fakes import FakeBroker, make_candles
from stocks_on_the_move.candles import CandleStore

TOKEN = 42
TODAY = date(2026, 9, 16)  # a Wednesday


def store(broker: FakeBroker, tmp_path) -> tuple[CandleStore, list[float]]:
    sleeps: list[float] = []
    return CandleStore(broker, tmp_path / "candles", sleep_sec=0.15, sleep=sleeps.append, today=lambda: TODAY), sleeps


def fetches(broker: FakeBroker) -> list:
    return [c for c in broker.calls if c[0] == "historical_data"]


def test_cold_cache_fetches_the_full_window_and_writes_it(tmp_path):
    broker = FakeBroker(candles={TOKEN: make_candles([10, 11, 12, 13, 14], end=TODAY)})
    s, sleeps = store(broker, tmp_path)

    df = s.get(TOKEN, days=20)

    assert list(df["close"]) == [10, 11, 12, 13, 14]
    assert (s.cache_dir / f"{TOKEN}.csv").exists()
    ((_, (_, start, end, interval)),) = fetches(broker)
    assert (end, interval) == (TODAY, "day")
    assert (TODAY - start).days == 20 + 75  # days plus the indicator padding
    assert sleeps == [0.15]


def test_current_cache_is_served_without_a_broker_call(tmp_path):
    broker = FakeBroker(candles={TOKEN: make_candles([10, 11, 12], end=TODAY)})
    s, sleeps = store(broker, tmp_path)
    s.get(TOKEN, days=5)
    broker.calls.clear()

    df = s.get(TOKEN, days=5)

    assert list(df["close"]) == [10, 11, 12]
    assert fetches(broker) == []
    assert sleeps == [0.15]  # only the first, cold, call paused


def test_stale_cache_is_extended_incrementally_when_the_overlap_matches(tmp_path):
    closes = list(range(100, 160))  # 60 days
    broker = FakeBroker(candles={TOKEN: make_candles(closes[:50], end=date(2026, 9, 2))})
    s, _ = store(broker, tmp_path)
    s._today = lambda: date(2026, 9, 2)
    s.get(TOKEN, days=30)  # warm the cache up to 2 Sep

    broker.candles[TOKEN] = make_candles(closes, end=TODAY)  # history unchanged, ten new days
    s._today = lambda: TODAY
    broker.calls.clear()
    df = s.get(TOKEN, days=30)

    assert list(df["close"])[-3:] == [157, 158, 159]
    ((_, (_, start, end, _)),) = fetches(broker)
    assert end == TODAY and start == date(2026, 9, 2) - timedelta(days=30)
    cached = pd.read_csv(s.cache_dir / f"{TOKEN}.csv")
    assert len(cached) == 60 and cached["close"].is_monotonic_increasing


def test_overlap_mismatch_invalidates_and_refetches_everything(tmp_path, caplog):
    closes = list(range(100, 150))
    broker = FakeBroker(candles={TOKEN: make_candles(closes, end=date(2026, 9, 2))})
    s, _ = store(broker, tmp_path)
    s._today = lambda: date(2026, 9, 2)
    s.get(TOKEN, days=30)

    # A 1:2 split rewrote history: every old close is halved, and there are new days.
    split = [c / 2 for c in closes] + [76, 77, 78]
    broker.candles[TOKEN] = make_candles(split, end=TODAY)
    s._today = lambda: TODAY
    broker.calls.clear()
    with caplog.at_level("WARNING", logger="stocks_on_the_move.candles"):
        df = s.get(TOKEN, days=30)

    assert "corporate action" in caplog.text
    assert len(fetches(broker)) == 2  # the validation fetch, then the full re-fetch
    assert list(df["close"])[-3:] == [76, 77, 78]
    assert df["close"].max() < 100


def test_empty_cache_file_is_deleted_and_refetched(tmp_path, caplog):
    broker = FakeBroker(candles={TOKEN: make_candles([1, 2, 3], end=TODAY)})
    s, _ = store(broker, tmp_path)
    s.cache_dir.mkdir(parents=True)
    (s.cache_dir / f"{TOKEN}.csv").write_text("")

    with caplog.at_level("WARNING", logger="stocks_on_the_move.candles"):
        df = s.get(TOKEN, days=5)

    assert "empty" in caplog.text
    assert list(df["close"]) == [1, 2, 3]


def test_no_data_at_all_returns_an_empty_frame_and_writes_nothing(tmp_path):
    s, _ = store(FakeBroker(), tmp_path)
    df = s.get(TOKEN, days=5)
    assert df.empty
    assert not (s.cache_dir / f"{TOKEN}.csv").exists()


@pytest.mark.parametrize("days", [5, 100])
def test_slice_returns_only_the_requested_window(tmp_path, days):
    broker = FakeBroker(candles={TOKEN: make_candles(list(range(300)), end=TODAY)})
    s, _ = store(broker, tmp_path)
    df = s.get(TOKEN, days=days)
    window_start = TODAY - timedelta(days=days + max(days // 2, 75))
    assert pd.to_datetime(df["date"]).dt.date.min() >= window_start
    assert pd.to_datetime(df["date"]).dt.date.max() == TODAY
