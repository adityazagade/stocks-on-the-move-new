"""The golden-file regression test (ADR-009).

One end-to-end run of the pipeline against frozen, synthetic inputs in
``tests/fixtures/golden/``, compared table by table with the committed
expected outputs. It answers "did the ranking, the exits, the sizes or the
buys change" for a fixed input, which is what a strategy edit, a pandas
upgrade or a Python bump must be checked against (ADR-003).

Regenerate the expected files deliberately with ``uv run pytest --update-golden``
and review the diff in the commit. The expected files encode the current
build's behaviour, not independently verified behaviour.
"""

from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from fakes import FakeBroker
from stocks_on_the_move.artifacts import RunArtifacts
from stocks_on_the_move.broker import Candle, Instrument, Quote
from stocks_on_the_move.candles import CandleStore
from stocks_on_the_move.context import RunContext
from stocks_on_the_move.pipeline import run
from stocks_on_the_move.universe import StaticUniverse

GOLDEN = Path(__file__).parent / "fixtures" / "golden"
EXPECTED = GOLDEN / "expected"
TABLES = ["universe.csv", "ranking.csv", "exits.csv", "sizing.csv", "candidates.csv", "trades.csv", "orders.csv"]
HEADERLESS = ["portfolio_after.csv"]


def load_meta() -> dict:
    return json.loads((GOLDEN / "meta.json").read_text())


def build_broker() -> FakeBroker:
    """A FakeBroker holding every fixture: instruments, candles per token, last prices, quotes."""
    with (GOLDEN / "instruments.csv").open(newline="") as f:
        instruments = [
            Instrument(
                int(r["instrument_token"]), r["tradingsymbol"], r["exchange"], r["segment"], r["instrument_type"]
            )
            for r in csv.DictReader(f)
        ]
    candles: dict[int, list[Candle]] = {}
    for path in sorted((GOLDEN / "candles").glob("*.csv")):
        with path.open(newline="") as f:
            candles[int(path.stem)] = [
                Candle(
                    date=datetime.fromisoformat(r["date"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=int(r["volume"]),
                )
                for r in csv.DictReader(f)
            ]
    with (GOLDEN / "ltp.csv").open(newline="") as f:
        ltp = {f"NSE:{r['symbol']}": float(r["last_price"]) for r in csv.DictReader(f)}
    with (GOLDEN / "quotes.csv").open(newline="") as f:
        quotes = {
            f"NSE:{r['symbol']}": Quote(float(r["last_price"]), float(r["best_bid"]), float(r["best_ask"]))
            for r in csv.DictReader(f)
        }
    return FakeBroker(instruments=instruments, candles=candles, ltp=ltp, quotes=quotes)


def read_table(path: Path, headerless: bool) -> pd.DataFrame:
    if path.stat().st_size == 0:
        return pd.DataFrame()
    if headerless:
        return pd.read_csv(path, header=None, names=["symbol", "qty"])
    return pd.read_csv(path)


@pytest.mark.parametrize("config", ["even_week", "odd_week"])
def test_pipeline_matches_the_golden_files(config, tmp_path, make_settings, update_golden):
    meta = load_meta()
    as_of = datetime.fromisoformat(meta["configs"][config]["as_of"])
    account = tmp_path / "account"
    account.mkdir()
    for name in ("portfolio_before.csv", "cash_ledger.csv", "trades_ledger.csv"):
        shutil.copy(GOLDEN / name, account / name)  # the run appends to the ledgers; never touch the fixtures

    settings = make_settings(
        **meta["settings"],
        portfolio_file=str(account / "portfolio_before.csv"),
        out_file=str(account / "next_portfolio.csv"),
        cash_ledger_file=str(account / "cash_ledger.csv"),
        trades_ledger_file=str(account / "trades_ledger.csv"),
        cache_dir=tmp_path / "candles",
        runs_dir=tmp_path / "runs",
    )
    broker = build_broker()
    artifacts = RunArtifacts.create(settings.runs_dir, started=as_of, mode="paper", settings=settings)
    ctx = RunContext(
        settings=settings,
        broker=broker,
        candles=CandleStore(broker, settings.cache_dir, sleep_sec=0.0, sleep=lambda _: None, today=as_of.date),
        now=lambda: as_of,
        paper=True,
        universe=StaticUniverse((GOLDEN / "nifty500.txt").read_text().split()),
        artifacts=artifacts,
    )

    run(ctx)

    actual_dir = artifacts.path
    expected_dir = EXPECTED / config
    if update_golden:
        expected_dir.mkdir(parents=True, exist_ok=True)
        for name in TABLES + HEADERLESS:
            shutil.copy(actual_dir / name, expected_dir / name)
        pytest.skip(f"golden files rewritten in {expected_dir}; review the diff, then run without --update-golden")

    assert expected_dir.is_dir(), "no expected files yet: run `uv run pytest --update-golden` once and review them"
    for name in TABLES + HEADERLESS:
        actual = read_table(actual_dir / name, name in HEADERLESS)
        expected = read_table(expected_dir / name, name in HEADERLESS)
        pd.testing.assert_frame_equal(actual, expected, check_exact=False, rtol=1e-9, check_dtype=False, obj=name)
    meta_out = json.loads((actual_dir / "run.json").read_text())
    assert meta_out["status"] == "completed"
    assert meta_out["resize_performed"] is (config == "even_week")
