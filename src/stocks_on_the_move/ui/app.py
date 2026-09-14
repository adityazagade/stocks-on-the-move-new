"""The console's routes (ADR-031). ``create_app`` builds it over a settings object; ``main`` serves it on loopback.

Stage one is read-only: every page is a view over ``runs/`` and the settings. The
pages never compute a strategy number; a figure that is not in a file is not on
a page.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from stocks_on_the_move.artifacts import settings_snapshot
from stocks_on_the_move.context import ist_now
from stocks_on_the_move.ledger import last_resize_date, load_state
from stocks_on_the_move.settings import Settings, SettingsError
from stocks_on_the_move.ui import research
from stocks_on_the_move.ui import runs as runs_mod
from stocks_on_the_move.ui.runs import PORTFOLIO_COLUMNS, TABLE_ORDER, RunInfo, Table
from stocks_on_the_move.ui.settings_view import settings_rows

HERE = Path(__file__).parent
DEFAULT_PORT = 8766
LOOPBACK = "127.0.0.1"
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")


@dataclass(frozen=True)
class Band:
    """The mode band every page carries: what a booking run from this environment would be, and where the account is."""

    booking_mode: str  # paper | live
    runs_dir: str
    strategy: str | None


def band_for(settings: Settings) -> Band:
    snapshot = settings_snapshot(settings)
    strategy = snapshot.get("STRATEGY")
    return Band(
        booking_mode="live" if settings.allow_kite_execution else "paper",
        runs_dir=str(settings.runs_dir),
        strategy=str(strategy) if strategy else None,
    )


def money(value: Any) -> str:
    """Two decimals with thousands separators for the floats in ``run.json``; anything else as it is."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{value:,.2f}"
    return "" if value is None else str(value)


def _json_for_html(data: Any) -> str:
    return json.dumps(data).replace("<", "\\u003c")


def create_app(
    settings: Settings,
    *,
    now: Callable[[], datetime] = ist_now,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
) -> FastAPI:
    """The console over ``settings.runs_dir``; ``allowed_hosts`` is the Host header check (ADR-031)."""
    app = FastAPI(title="Stocks on the Move console", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    page_globals: dict[str, Any] = {
        "band": band_for(settings),
        "is_number": runs_mod.is_number,
        "table_names": TABLE_ORDER,
    }
    templates.env.globals.update(page_globals)
    templates.env.filters["money"] = money
    runs_dir = settings.runs_dir

    def render(request: Request, name: str, **context: Any) -> Response:
        return templates.TemplateResponse(request, name, context)

    def get_run(day: str, name: str) -> RunInfo:
        run = runs_mod.load_run(runs_dir, day, name, now())
        if run is None:
            raise HTTPException(status_code=404, detail="no such run")
        return run

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/runs")

    # -- runs -------------------------------------------------------------------
    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, mode: str = "", status: str = "") -> Response:
        every = runs_mod.list_runs(runs_dir, now())
        shown = [r for r in every if (not mode or r.mode == mode) and (not status or r.display_status == status)]
        return render(
            request,
            "runs.html",
            nav="runs",
            runs=shown,
            mode=mode,
            status=status,
            modes=sorted({r.mode for r in every}),
            statuses=sorted({r.display_status for r in every}),
        )

    @app.get("/runs/{day}/{name}", response_class=HTMLResponse)
    def run_page(request: Request, day: str, name: str) -> Response:
        run = get_run(day, name)
        return render(request, "run.html", nav="runs", run=run, tab="summary")

    @app.get("/runs/{day}/{name}/{tab}", response_class=HTMLResponse)
    def run_tab(request: Request, day: str, name: str, tab: str, sort: str = "", desc: bool = False) -> Response:
        run = get_run(day, name)
        if tab == "log":
            return render(request, "run.html", nav="runs", run=run, tab=tab, log=runs_mod.read_log(run))
        if tab == "meta":
            meta = json.dumps(run.meta, indent=2)
            return render(request, "run.html", nav="runs", run=run, tab=tab, meta_json=meta)
        data = runs_mod.read_table(run, tab)
        if data is None:
            raise HTTPException(status_code=404, detail="no such table")
        table = Table(columns=data.columns, rows=runs_mod.sort_rows(data.rows, sort or None, descending=desc))
        context = {
            "nav": "runs",
            "run": run,
            "tab": tab,
            "table": table,
            "sort": sort,
            "desc": desc,
            "base": f"/runs/{run.id}/{tab}",
            "table_id": "table",
            "empty_text": "no rows" if run.has(f"{tab}.csv") else "not written yet",
        }
        if request.headers.get("HX-Request"):
            return render(request, "partials/table.html", **context)
        return render(request, "run.html", **context)

    # -- account ----------------------------------------------------------------
    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request) -> Response:
        every = runs_mod.list_runs(runs_dir, now())
        newest = runs_mod.newest_booking_run(every)
        series = runs_mod.equity_series(every)
        chart = runs_mod.aligned_chart({mode: {p.date: p.equity for p in pts} for mode, pts in series.items()})
        positions = newest.meta.get("positions_after") if newest else None
        state = load_state(settings.state_file)
        return render(
            request,
            "account.html",
            nav="account",
            newest=newest,
            positions=sorted(positions.items()) if isinstance(positions, dict) else [],
            series=series,
            chart_json=_json_for_html(chart),
            current_pf=runs_mod.read_csv(Path(settings.portfolio_file), PORTFOLIO_COLUMNS, headerless=True),
            next_pf=runs_mod.read_csv(Path(settings.out_file), PORTFOLIO_COLUMNS, headerless=True),
            cash_ledger=runs_mod.read_csv(Path(settings.cash_ledger_file)),
            trades_ledger=runs_mod.read_csv(Path(settings.trades_ledger_file)),
            state=state,
            last_resize=last_resize_date(state),
            files={
                "current": settings.portfolio_file,
                "next": settings.out_file,
                "cash": settings.cash_ledger_file,
                "trades": settings.trades_ledger_file,
                "state": settings.state_file,
            },
        )

    # -- research ---------------------------------------------------------------
    @app.get("/research", response_class=HTMLResponse)
    def research_page(request: Request, select: Annotated[list[str] | None, Query()] = None) -> Response:
        every = research.list_backtests(runs_dir)
        chosen = [b for b in every if not select or b.name in select]
        return render(
            request,
            "research.html",
            nav="research",
            backtests=every,
            chosen=chosen,
            selected={b.name for b in chosen},
            comparison=research.comparison(chosen),
            chart_json=_json_for_html(research.chart_data(chosen)),
            ribbon=research.regime_ribbon(chosen[0]) if chosen else [],
            note=chosen[0].note if chosen else "",
        )

    @app.get("/research/{name}", response_class=HTMLResponse)
    def backtest_page(request: Request, name: str, sort: str = "", desc: bool = False) -> Response:
        bt = research.load_backtest(runs_dir, name)
        if bt is None:
            raise HTTPException(status_code=404, detail="no such backtest")
        raw = research.trades(bt)
        table = Table(columns=raw.columns, rows=runs_mod.sort_rows(raw.rows, sort or None, descending=desc))
        context = {
            "nav": "research",
            "bt": bt,
            "table": table,
            "sort": sort,
            "desc": desc,
            "base": f"/research/{bt.name}",
            "table_id": "table",
            "empty_text": "no trades",
            "weekly": research.weekly(bt),
            "params_json": research.params(bt),
            "comparison": research.comparison([bt]),
        }
        if request.headers.get("HX-Request"):
            return render(request, "partials/table.html", **context)
        return render(request, "backtest.html", **context)

    # -- settings ---------------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Response:
        return render(request, "settings.html", nav="settings", rows=settings_rows(settings))

    return app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="stocks-on-the-move-ui",
        description="The operator console (ADR-031): reads runs/ and starts the command. Serves on 127.0.0.1 only.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"TCP port on 127.0.0.1 (default {DEFAULT_PORT})"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None
    band = band_for(settings)
    print(f"Console on http://{LOOPBACK}:{args.port}/  RUNS_DIR={band.runs_dir}  booking runs: {band.booking_mode}")
    uvicorn.run(create_app(settings), host=LOOPBACK, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
