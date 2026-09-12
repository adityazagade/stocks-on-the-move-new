"""Logging at entry, two handler levels, and the WARNING promotions (ADR-015)."""

from __future__ import annotations

import logging

import pytest
from kiteconnect.exceptions import NetworkException
from pydantic import ValidationError

from fakes import FakeBroker, trending_closes
from stocks_on_the_move import logging_setup as ls
from stocks_on_the_move.broker import KiteBroker, PaperBroker
from stocks_on_the_move.ledger import init_cash_balance
from stocks_on_the_move.pipeline import resize_positions, run
from stocks_on_the_move.rules import rank_universe
from stocks_on_the_move.settings import Settings
from test_pipeline import DRIFTS, TODAY, bull_market


@pytest.fixture
def clean_package_logger():
    """Leave the package logger as we found it: tests must not configure it for each other."""
    logger = ls.package_logger()
    before = (logger.level, list(logger.handlers))
    yield logger
    for handler in list(logger.handlers):
        if handler not in before[1]:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(before[0])


def test_configure_logging_is_idempotent_and_leaves_root_alone(clean_package_logger):
    root_handlers = list(logging.getLogger().handlers)
    first = ls.configure_logging("INFO")
    second = ls.configure_logging("warning")
    assert first is second
    assert ls.console_handler() is first
    assert first.level == logging.WARNING
    assert clean_package_logger.level == logging.DEBUG  # the file handler needs everything
    assert [h for h in clean_package_logger.handlers if getattr(h, "_sotm_console", False)] == [first]
    assert logging.getLogger().handlers == root_handlers


def test_log_level_setting_is_validated_and_upper_cased():
    assert Settings.from_values(kite_api_key="k", kite_api_secret="s", log_level="debug").log_level == "DEBUG"
    assert Settings.from_values(kite_api_key="k", kite_api_secret="s").log_level == "INFO"
    with pytest.raises(ValidationError, match="log_level"):
        Settings.from_values(kite_api_key="k", kite_api_secret="s", log_level="LOUD")


def test_a_swallowed_ranking_error_is_a_warning_naming_symbol_and_type(make_context, caplog):
    class BrokenBroker(FakeBroker):
        def historical_data(self, *args, **kwargs):
            raise RuntimeError("boom")

    broker = BrokenBroker()
    broker.add_equity("X", 1, [1.0], end=TODAY)
    ctx = make_context(broker)
    with caplog.at_level(logging.WARNING, logger="stocks_on_the_move.rules"):
        rank_universe(ctx, broker.instruments("NSE"))
    assert "X skipped – RuntimeError: boom" in caplog.text


def test_a_size_error_in_resize_is_a_warning(make_context, caplog):
    ctx = make_context()
    init_cash_balance(ctx)
    ctx.portfolio.positions = {"GHOST": 5}  # no instrument, no candles: size_position raises
    with caplog.at_level(logging.WARNING, logger="stocks_on_the_move.pipeline"):
        resize_positions(ctx, bull=True)
    assert "size calc error GHOST – KeyError" in caplog.text


def test_rate_limit_backoff_is_a_warning_with_the_attempt(caplog):
    class Kite:
        calls = 0

        def ltp(self, keys):
            self.calls += 1
            if self.calls == 1:
                raise NetworkException("Too many requests", code=429)
            return {"NSE:TCS": {"last_price": 1.0}}

    broker = KiteBroker(Kite(), min_interval=0.0, max_retries=3, sleep=lambda _: None, uniform=lambda a, b: 0.0)
    with caplog.at_level(logging.WARNING, logger="stocks_on_the_move.broker"):
        broker.ltp(["NSE:TCS"])
    assert "Kite rate limited on attempt 1/3" in caplog.text


def test_console_at_warning_while_the_run_file_keeps_debug(make_context, clean_package_logger, capsys):
    """Plan step 3, locally: LOG_LEVEL=WARNING shows only warnings; run.log holds DEBUG; no secret anywhere."""
    ls.configure_logging("WARNING")
    broker = PaperBroker(bull_market(DRIFTS))  # its unsent orders are the DEBUG lines a real paper run has
    ctx = make_context(broker, cut_off_pct=0.5, log_level="WARNING", artifacts=True)
    ctx.universe = lambda: set(DRIFTS)
    ctx.artifacts.attach_log()

    run(ctx)
    ctx.artifacts.finish("completed")

    console = capsys.readouterr().err
    assert "INFO" not in console and "DEBUG" not in console
    log_text = (ctx.artifacts.path / "run.log").read_text()
    assert "INFO     stocks_on_the_move.pipeline pipeline:" in log_text and "Done. Final" in log_text
    assert "DEBUG    stocks_on_the_move.broker broker:" in log_text and "not sent" in log_text
    for secret in ("test-key", "test-secret"):
        assert secret not in log_text and secret not in console


def test_rank_universe_stays_quiet_at_info_when_nothing_is_wrong(make_context, caplog):
    broker = FakeBroker()
    broker.add_equity("GOOD", 1, trending_closes(150, daily=0.002), end=TODAY)
    ctx = make_context(broker)
    with caplog.at_level(logging.WARNING, logger="stocks_on_the_move.rules"):
        rank_universe(ctx, broker.instruments("NSE"))
    assert caplog.records == []
