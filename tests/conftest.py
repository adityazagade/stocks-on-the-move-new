"""Test-session fixtures.

Importing ``stocks_on_the_move.momentum`` has no side effects (ADR-007) and the
strategy holds no module state (ADR-008): everything a run needs travels in a
``RunContext``. These fixtures build one over ``fakes.FakeBroker`` with a
frozen clock, explicit settings that ignore the developer's environment, and
every file path inside the test's temporary directory.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest

from fakes import EVEN_WEEK_WEDNESDAY, FakeBroker
from stocks_on_the_move import momentum as m
from stocks_on_the_move.artifacts import RunArtifacts
from stocks_on_the_move.candles import CandleStore
from stocks_on_the_move.settings import Settings


@pytest.fixture
def make_settings(tmp_path) -> Callable[..., Settings]:
    """``make_settings(**overrides)`` -> a Settings that ignores the environment; paper mode, tmp paths."""

    def make(**overrides) -> Settings:
        values = {
            "kite_api_key": "test-key",
            "kite_api_secret": "test-secret",
            "allow_kite_execution": False,
            "cache_dir": tmp_path / "candles",
            "kite_session_file": tmp_path / "kite_session.json",
            "kite_redirect_port": 0,
            "kite_open_browser": False,
            "portfolio_file": str(tmp_path / "current_portfolio.csv"),
            "out_file": str(tmp_path / "next_portfolio.csv"),
            "cash_ledger_file": str(tmp_path / "cash_ledger.csv"),
            "trades_ledger_file": str(tmp_path / "trades_ledger.csv"),
            "runs_dir": tmp_path / "runs",
        }
        values.update(overrides)
        return Settings.from_values(**values)

    return make


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest.fixture
def make_context(make_settings) -> Callable[..., m.RunContext]:
    """``make_context(broker=None, *, now=None, **settings_overrides)`` -> RunContext over a FakeBroker.

    The clock is frozen at a Wednesday in an even ISO week unless ``now`` says
    otherwise; the candle store's "today" follows that clock, and it never sleeps.
    ``artifacts=True`` gives the context a real run directory under the tmp path.
    """

    def make(
        broker: FakeBroker | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        artifacts: bool = False,
        **overrides,
    ):
        s = make_settings(**overrides)
        broker = FakeBroker() if broker is None else broker
        clock = now or (lambda: EVEN_WEEK_WEDNESDAY)
        candles = CandleStore(
            broker, s.cache_dir, sleep_sec=s.candle_sleep_sec, sleep=lambda _: None, today=lambda: clock().date()
        )
        ctx = m.RunContext(settings=s, broker=broker, candles=candles, now=clock, paper=True)
        if artifacts:  # a real run directory under the test's runs_dir (ADR-006)
            ctx.artifacts = RunArtifacts.create(s.runs_dir, started=clock(), mode=m.run_mode(s), settings=s)
        return ctx

    return make


@pytest.fixture
def ctx(make_context) -> m.RunContext:
    return make_context()
