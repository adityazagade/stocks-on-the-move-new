"""The console's routes (ADR-031). ``create_app`` builds it over a settings object; ``main`` serves it on loopback.

Every page is a view over ``runs/`` and the settings; the pages never compute a
strategy number. The one write in this stage is starting the command as a
child process, in a mode no higher than the environment the console was
started from allows, behind the token in ``security.py`` and the Host check.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from stocks_on_the_move.artifacts import settings_snapshot
from stocks_on_the_move.context import ist_now
from stocks_on_the_move.kite_auth import load_session, session_is_live
from stocks_on_the_move.ledger import last_resize_date, load_state
from stocks_on_the_move.settings import Settings, SettingsError
from stocks_on_the_move.ui import research, security
from stocks_on_the_move.ui import runs as runs_mod
from stocks_on_the_move.ui.launcher import Child, Launcher, LauncherBusy
from stocks_on_the_move.ui.runs import PORTFOLIO_COLUMNS, TABLE_ORDER, RunInfo, Table
from stocks_on_the_move.ui.settings_view import settings_rows

HERE = Path(__file__).parent
DEFAULT_PORT = 8766
LOOPBACK = "127.0.0.1"
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")
LIVE_WORD = "LIVE"  # typed by the operator before a live booking run starts


@dataclass(frozen=True)
class Band:
    """The mode band every page carries: what a booking run from this environment would be, and where the account is."""

    booking_mode: str  # paper | live | kill
    runs_dir: str
    strategy: str | None
    trading_weekday: int
    plan_only_env: bool  # PLAN_ONLY=1 in the environment; the console's booking run overrides it for that run
    env_cashflow: float  # a non-zero ENV_CASHFLOW appends a ledger row on every booking run


def _booking_mode(settings: Settings) -> str:
    """What a run with PLAN_ONLY=0 from this environment is: the console never raises it (ADR-031)."""
    if settings.kill_switch:
        return "kill"
    return "live" if settings.allow_kite_execution else "paper"


def band_for(settings: Settings) -> Band:
    snapshot = settings_snapshot(settings)
    strategy = snapshot.get("STRATEGY")
    return Band(
        booking_mode=_booking_mode(settings),
        runs_dir=str(settings.runs_dir),
        strategy=str(strategy) if strategy else None,
        trading_weekday=settings.trading_weekday,
        plan_only_env=settings.plan_only,
        env_cashflow=settings.env_cashflow,
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
    launcher: Launcher | None = None,
    token: str | None = None,
) -> FastAPI:
    """The console over ``settings.runs_dir``; ``allowed_hosts`` is the Host header check (ADR-031)."""
    app = FastAPI(title="Stocks on the Move console", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    band = band_for(settings)
    launcher = launcher if launcher is not None else Launcher(now=now)
    token = token if token is not None else security.new_token()
    page_globals: dict[str, Any] = {
        "band": band,
        "is_number": runs_mod.is_number,
        "table_names": TABLE_ORDER,
        "token": token,
        "token_field": security.FIELD,
    }
    templates.env.globals.update(page_globals)
    templates.env.filters["money"] = money
    runs_dir = settings.runs_dir

    def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def get_run(day: str, name: str) -> RunInfo:
        run = runs_mod.load_run(runs_dir, day, name, now())
        if run is None:
            raise HTTPException(status_code=404, detail="no such run")
        return run

    def run_created_by(child: Child | None) -> RunInfo | None:
        """The oldest run directory that started after the child did: the one it made."""
        if child is None:
            return None
        created = None
        for run in runs_mod.list_runs(runs_dir, now()):  # newest first
            try:
                started = datetime.fromisoformat(run.started)
            except ValueError:
                continue
            if started >= child.started:
                created = run
        return created

    def launch_context() -> dict[str, Any]:
        child = launcher.current
        return {"child": child, "created": run_created_by(child)}

    def today_context() -> dict[str, Any]:
        today = now()
        record = load_session(Path(settings.kite_session_file))
        session = {  # the record's identity and age; never its token
            "user_id": record.user_id if record else "",
            "issued_at": record.issued_at.isoformat(timespec="seconds") if record else "",
            "source": record.source if record else "",
            "live": record is not None and session_is_live(record, today),
        }
        every = runs_mod.list_runs(runs_dir, today)
        return {
            "nav": "today",
            "today": today,
            "weekday_ok": today.weekday() == settings.trading_weekday,
            "weekday_name": calendar.day_name[settings.trading_weekday],
            "session": session,
            "newest": every[0] if every else None,
            **launch_context(),
        }

    # -- today and the launcher -----------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def today_page(request: Request) -> Response:
        return render(request, "today.html", **today_context())

    @app.get("/launch", response_class=HTMLResponse)
    def launch_panel(request: Request) -> Response:
        return render(request, "partials/launch.html", **launch_context())

    @app.post("/runs/start", response_class=HTMLResponse)
    def start_run(
        request: Request,
        mode: Annotated[str, Form()] = "",
        confirm: Annotated[str, Form()] = "",
        form_token: Annotated[str | None, Form(alias=security.FIELD)] = None,
    ) -> Response:
        presented = form_token if form_token is not None else request.headers.get(security.HEADER)
        if not security.token_matches(token, presented):
            raise HTTPException(status_code=403, detail="the console token is missing or wrong")

        def refuse(status: int, message: str) -> Response:
            return render(request, "today.html", status_code=status, error=message, **today_context())

        if mode not in ("plan", "book"):
            return refuse(400, "mode must be plan or book")
        if mode == "book" and band.booking_mode == "kill":
            return refuse(
                409, "KILL_SWITCH=1 in the environment: a booking run would liquidate everything. Use the command."
            )
        if mode == "book" and band.booking_mode == "live" and confirm != LIVE_WORD:
            return refuse(403, f"A live booking run sends real orders; type {LIVE_WORD} in the field to start one.")
        try:
            launcher.start(mode)
        except LauncherBusy:
            return refuse(409, "A run is already going; wait for it to finish.")
        return RedirectResponse("/", status_code=303)

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

    @app.get("/runs/{day}/{name}/log/stream")
    def log_stream(day: str, name: str, skip: int = 0) -> StreamingResponse:
        """Server-sent events: the log's lines after the first ``skip``, then new ones as they land, then ``done``."""
        run = get_run(day, name)

        async def events() -> AsyncIterator[str]:
            sent = max(skip, 0)
            while True:
                lines = runs_mod.read_log(run, max_lines=None)
                for line in lines[sent:]:
                    yield f"data: {line}\n\n"
                sent = max(sent, len(lines))
                fresh = runs_mod.load_run(runs_dir, day, name, now())
                if fresh is None or fresh.display_status != "running":
                    yield "event: done\ndata: end\n\n"
                    return
                await asyncio.sleep(1.0)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/runs/{day}/{name}/{tab}", response_class=HTMLResponse)
    def run_tab(request: Request, day: str, name: str, tab: str, sort: str = "", desc: bool = False) -> Response:
        run = get_run(day, name)
        if tab == "rail":
            return render(request, "partials/progress.html", run=run, tab=request.query_params.get("tab", "summary"))
        if tab == "log":
            return render(request, "run.html", nav="runs", run=run, tab=tab, log=runs_mod.read_log(run))
        if tab == "meta":
            meta = json.dumps(run.meta, indent=2)
            return render(request, "run.html", nav="runs", run=run, tab=tab, meta_json=meta)
        data = runs_mod.read_table(run, tab)
        if data is None:
            raise HTTPException(status_code=404, detail="no such table")
        table = Table(columns=data.columns, rows=runs_mod.sort_rows(data.rows, sort or None, descending=desc))
        context: dict[str, Any] = {
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
        context: dict[str, Any] = {
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
