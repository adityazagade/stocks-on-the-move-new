"""Smoke tests for the pure, broker-independent parts of the strategy."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stocks_on_the_move import momentum as m

# ── symbol / series helpers ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tradingsymbol", "series", "base"),
    [
        ("RELIANCE", "EQ", "RELIANCE"),
        ("RELIANCE-EQ", "EQ", "RELIANCE"),
        ("IDEA-BE", "BE", "IDEA"),
        ("M&M", "EQ", "M&M"),
        ("BAJAJ-AUTO", "EQ", "BAJAJ-AUTO"),  # "-AUTO" is not a series code
        ("nifty-be", "BE", "NIFTY"),  # normalised to upper case
    ],
)
def test_series_and_base_symbol(tradingsymbol, series, base):
    assert m.series_of(tradingsymbol) == series
    assert m.base_symbol(tradingsymbol) == base


# ── portfolio CSV round trip ─────────────────────────────────────────────


def test_portfolio_roundtrip_is_sorted(tmp_path):
    path = tmp_path / "pf.csv"
    m.write_portfolio(str(path), {"TCS": 5, "INFY": 10})
    assert path.read_text().splitlines() == ["INFY,10", "TCS,5"]
    assert m.read_portfolio(str(path)) == {"INFY": 10, "TCS": 5}


def test_read_portfolio_missing_file_and_bad_rows(tmp_path):
    assert m.read_portfolio(str(tmp_path / "nope.csv")) == {}
    bad = tmp_path / "bad.csv"
    bad.write_text("tcs,5\nINFY,ten\n")
    assert m.read_portfolio(str(bad)) == {"TCS": 5}


# ── costs ────────────────────────────────────────────────────────────────


def test_buy_cost_and_sell_proceeds_bracket_the_notional(settings):
    price, qty = 100.0, 10
    friction = settings.fees_pct + settings.slippage_pct
    assert m.gross_cost_for_buy(settings, price, qty) == pytest.approx(1000 * (1 + friction))
    assert m.net_proceeds_for_sell(settings, price, qty) == pytest.approx(1000 * (1 - friction))


# ── indicators ───────────────────────────────────────────────────────────


def test_annualise_inverts_daily_log_slope():
    daily = math.log(1.5) / m.TRADING_DAYS_YR  # +50 % over one trading year
    assert m.annualise(daily) == pytest.approx(0.5)
    assert m.annualise(0.0) == 0.0


def test_atr_constant_range():
    n = 30
    df = pd.DataFrame({"high": [102.0] * n, "low": [98.0] * n, "close": [100.0] * n})
    assert m.atr(df, period=20) == pytest.approx(4.0)
    assert math.isnan(m.atr(pd.DataFrame(), 20))
    assert math.isnan(m.atr(df.head(1), 20))  # one bar -> no true range after the shift


def test_composite_momentum_perfect_log_linear_uptrend():
    n = max(m.LOOKBACK_R126, m.REG_LOOKBACK) + 10
    closes = pd.Series(100.0 * np.exp(0.001 * np.arange(n)))  # exactly 0.1 %/day in log space
    score, ann, r2 = m._composite_momentum(closes)
    assert r2 == pytest.approx(1.0)
    assert ann == pytest.approx(m.annualise(0.001))
    assert score > 0


def test_composite_momentum_insufficient_data():
    closes = pd.Series(np.linspace(100, 110, 20))
    assert all(math.isnan(v) for v in m._composite_momentum(closes))
