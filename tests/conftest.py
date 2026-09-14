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
from stocks_on_the_move.broker import Broker
from stocks_on_the_move.candles import CandleStore
from stocks_on_the_move.context import RunContext
from stocks_on_the_move.settings import Settings
from stocks_on_the_move.universe import NO_BANDS


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/fixtures/golden/expected/ from the current build (ADR-009); review the diff",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


@pytest.fixture
def make_settings(tmp_path) -> Callable[..., Settings]:
    """``make_settings(**overrides)`` -> a Settings that ignores the environment; paper mode, tmp paths."""

    def make(*, state_files: bool = True, **overrides) -> Settings:
        """``state_files=False`` leaves the five state paths at their production defaults under ``runs_dir``."""
        values = {
            "kite_api_key": "test-key",
            "kite_api_secret": "test-secret",
            "account_value": 100_000,  # the cash the tests were written against; the production default is 0 (819f95a)
            "allow_kite_execution": False,
            "cache_dir": tmp_path / "candles",
            "kite_session_file": tmp_path / "kite_session.json",
            "kite_redirect_port": 0,
            "kite_open_browser": False,
            "runs_dir": tmp_path / "runs",
        }
        if state_files:  # explicit paths at the tmp root, so tests can assert what a run did and did not write
            values.update(
                portfolio_file=str(tmp_path / "current_portfolio.csv"),
                out_file=str(tmp_path / "next_portfolio.csv"),
                cash_ledger_file=str(tmp_path / "cash_ledger.csv"),
                trades_ledger_file=str(tmp_path / "trades_ledger.csv"),
                state_file=str(tmp_path / "strategy_state.json"),
            )
        values.update(overrides)
        return Settings.from_values(**values)

    return make


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest.fixture
def make_context(make_settings) -> Callable[..., RunContext]:
    """``make_context(broker=None, *, now=None, **settings_overrides)`` -> RunContext over a FakeBroker.

    The clock is frozen at a Wednesday in an even ISO week unless ``now`` says
    otherwise; the candle store's "today" follows that clock, and neither it nor
    the wait for a fill ever sleeps. ``artifacts=True`` gives the context a real
    run directory under the tmp path.
    """

    def make(
        broker: Broker | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        artifacts: bool = False,
        state_files: bool = True,
        **overrides,
    ):
        s = make_settings(state_files=state_files, **overrides)
        broker = FakeBroker() if broker is None else broker
        clock = now or (lambda: EVEN_WEEK_WEDNESDAY)
        candles = CandleStore(
            broker, s.cache_dir, sleep_sec=s.candle_sleep_sec, sleep=lambda _: None, today=lambda: clock().date()
        )
        ctx = RunContext(
            settings=s,
            broker=broker,
            candles=candles,
            now=clock,
            paper=True,
            sleep=lambda _: None,
            bands=NO_BANDS,  # a static, permissive lookup, as StaticUniverse stands in for the archives (ADR-034)
        )
        if artifacts:  # a real run directory under the test's runs_dir (ADR-006)
            ctx.artifacts = RunArtifacts.create(s.runs_dir, started=clock(), mode=m.run_mode(s), settings=s)
        return ctx

    return make


@pytest.fixture
def ctx(make_context) -> RunContext:
    return make_context()
