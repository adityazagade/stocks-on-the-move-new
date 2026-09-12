#!/usr/bin/env python3
"""
Weekly momentum portfolio for NSE equities, after Andreas Clenow's "Stocks on the Move".

This is the entry point: ``main()`` loads the settings, applies the weekday
guard, opens the run directory, logs in, assembles a ``RunContext`` and hands
it to ``pipeline.run``. The strategy itself lives in one module per job
(ADR-020):

- ``context.py``     the run context, the portfolio, a fill, the token cache
- ``indicators.py``  strategy constants and the pure computations on prices
- ``rules.py``       regime, filter chain and ranking, exit rules, ATR sizing
- ``execution.py``   prices, order placement, the wait for a fill, booking
- ``pipeline.py``    the twelve steps and ``run(ctx)``
- ``reporting.py``   the artifact tables' columns and row builders
- ``universe.py``    the symbols the strategy may hold
- ``ledger.py``      the portfolio snapshot and the two ledgers

Around them: ``broker.py`` (the Kite adapter and the paper broker, ADR-008),
``candles.py`` (the candle cache), ``settings.py`` (ADR-007), ``kite_auth.py``
(ADR-005), ``artifacts.py`` (ADR-006), ``logging_setup.py`` (ADR-015).

Portfolio CSV format: SYMBOL,QUANTITY (no header). Files, all overridable
through the environment:
- current_portfolio.csv   positions going into the run
- next_portfolio.csv      positions after the run
- cash_ledger.csv         date, amount, note   (+ deposit, - withdrawal)
- trades_ledger.csv       timestamp, side, symbol, qty, price, fees_pct, slippage_pct, cash_delta
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from stocks_on_the_move import kite_auth
from stocks_on_the_move.artifacts import Artifacts, NoArtifacts, RunArtifacts
from stocks_on_the_move.broker import Broker, KiteBroker, PaperBroker
from stocks_on_the_move.candles import CandleStore
from stocks_on_the_move.context import RunContext, ist_now
from stocks_on_the_move.logging_setup import configure_logging
from stocks_on_the_move.pipeline import run
from stocks_on_the_move.settings import Settings, SettingsError

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


def authenticate(settings: Settings) -> KiteBroker:
    """A Kite session (cached, captured from the redirect, or pasted; ADR-005) behind the Broker protocol."""
    auth = kite_auth.AuthSettings(
        session_file=settings.kite_session_file,
        redirect_port=settings.kite_redirect_port,
        open_browser=settings.kite_open_browser,
        forget_session=settings.kite_forget_session,
    )
    kite = kite_auth.authenticate(
        settings.kite_api_key.get_secret_value(),
        settings.kite_api_secret.get_secret_value(),
        settings=auth,
    )
    return KiteBroker(kite, min_interval=settings.kite_min_interval, max_retries=settings.kite_max_retries)


def run_mode(settings: Settings) -> str:
    """``plan``, ``kill``, ``paper`` or ``live``: the run directory's suffix."""
    if settings.plan_only:
        return "plan"
    if settings.kill_switch:
        return "kill"
    return "live" if settings.allow_kite_execution else "paper"


def is_trading_day(settings: Settings, now: datetime) -> bool:
    """The weekday guard: the configured weekday only, unless the run is a kill switch or a plan (ADR-022)."""
    return settings.kill_switch or settings.plan_only or now.weekday() == settings.trading_weekday


def main() -> None:
    configure_logging("INFO")  # the package logger, at entry, never at import (ADR-015)

    # 0) Load and validate configuration; a bad value names itself and stops the run
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None
    configure_logging(settings.log_level)

    # 1) Run only on the configured weekday in IST (0=Mon; default 2=Wed), before any login
    if not is_trading_day(settings, ist_now()):
        logger.info("Not scheduled trading weekday (IST) – abort")
        return

    # Past the guard: this run gets a directory, and its log goes there too (ADR-006)
    artifacts: Artifacts
    try:
        artifacts = RunArtifacts.create(
            settings.runs_dir, started=ist_now(), mode=run_mode(settings), settings=settings
        )
    except OSError as exc:
        logger.warning(
            "Could not create a run directory under %s (%s); running without artifacts", settings.runs_dir, exc
        )
        artifacts = NoArtifacts()
    artifacts.attach_log()

    try:
        kite = authenticate(settings)
        paper = not settings.allow_kite_execution or settings.plan_only  # a plan never reaches the real broker
        broker: Broker = PaperBroker(kite) if paper else kite
        ctx = RunContext(
            settings=settings,
            broker=broker,
            candles=CandleStore(
                broker, settings.cache_dir, sleep_sec=settings.candle_sleep_sec, today=lambda: ist_now().date()
            ),
            now=ist_now,
            paper=paper,
            artifacts=artifacts,
        )
        run(ctx)
    except KeyboardInterrupt:
        artifacts.finish("failed:KeyboardInterrupt")
        raise
    except Exception as exc:
        logger.exception("Run failed: %s", exc)
        artifacts.finish(f"failed:{type(exc).__name__}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
