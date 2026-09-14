"""The operator console (ADR-031): the import boundary, the vendored files, and every read-only page.

The fixture tree is a ``runs/`` directory assembled from the golden fixtures'
inputs and expected tables (ADR-009) with hand-written ``run.json`` files, so
each page is checked against files whose content is known, cell by cell.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stocks_on_the_move.context import IST
from stocks_on_the_move.kite_auth import SessionRecord
from stocks_on_the_move.ledger import append_cashflow
from stocks_on_the_move.reporting import EXIT_COLUMNS
from stocks_on_the_move.ui import runs as runs_mod
from stocks_on_the_move.ui.app import create_app, main, parse_args
from stocks_on_the_move.ui.launcher import Launcher, LauncherBusy

UI_DIR = Path(__file__).resolve().parents[1] / "src" / "stocks_on_the_move" / "ui"
GOLDEN = Path(__file__).parent / "fixtures" / "golden"
EXPECTED = GOLDEN / "expected" / "odd_week"
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=IST)
NEWEST = "2026-09-09/100000-paper"
ABANDONED = "2026-09-13/155259-paper"


# ── the boundary and the vendored files ──────────────────────────────────────
FORBIDDEN = {"rules", "indicators", "pipeline", "broker", "candles", "universe", "momentum", "backtest"}


def test_the_console_imports_nothing_from_the_strategy():
    """ADR-031: the column lists, the settings, the ledger readers, the session helpers, and nothing else."""
    checked = 0
    for py in sorted(UI_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("stocks_on_the_move"):
                parts = node.module.split(".")
                module = parts[1] if len(parts) > 1 else ""
                assert module not in FORBIDDEN, f"{py.name} imports stocks_on_the_move.{module}"
                if module == "execution":
                    assert {a.name for a in node.names} == {"ORDER_COLUMNS"}, py.name
                checked += 1
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] != "kiteconnect", py.name
                    parts = alias.name.split(".")
                    if parts[0] == "stocks_on_the_move" and len(parts) > 1:
                        assert parts[1] not in FORBIDDEN, f"{py.name} imports {alias.name}"
    assert checked > 0


def test_vendored_files_match_their_manifest():
    vendor = UI_DIR / "static" / "vendor"
    listed: dict[str, str] = {}
    for line in (vendor / "VERSIONS").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, _version, digest, _upstream = line.split()
            listed[name] = digest
    assert set(listed) == {p.name for p in vendor.iterdir() if p.name != "VERSIONS"}
    for name, digest in listed.items():
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == digest, name


# ── the fixture tree ─────────────────────────────────────────────────────────
def _meta(mode: str, started: str, status: str, **extra) -> dict:
    meta = {
        "started": started,
        "finished": None,
        "status": status,
        "mode": mode,
        "version": "0.1.0",
        "commit": "abc1234",
        "settings": {"RUNS_DIR": "runs"},
    }
    meta.update(extra)
    return meta


def _write_run(path: Path, meta: dict, tables: dict[str, Path] | None = None, log=(), mtime: float | None = None):
    path.mkdir(parents=True)
    (path / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    for name, src in (tables or {}).items():
        shutil.copy(src, path / name)
    if log:
        (path / "run.log").write_text("\n".join(log) + "\n")
    if mtime is not None:
        for p in path.iterdir():
            os.utime(p, (mtime, mtime))


def _positions(path: Path) -> dict[str, int]:
    with path.open(newline="") as f:
        return {sym: int(qty) for sym, qty in csv.reader(f)}


@pytest.fixture
def runs_tree(tmp_path) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    shutil.copy(GOLDEN / "cash_ledger.csv", root / "cash_ledger.csv")
    shutil.copy(GOLDEN / "trades_ledger.csv", root / "trades_ledger.csv")
    shutil.copy(GOLDEN / "portfolio_before.csv", root / "current_portfolio.csv")
    shutil.copy(EXPECTED / "portfolio_after.csv", root / "next_portfolio.csv")
    (root / "strategy_state.json").write_text('{"last_resize_date": "2026-09-09"}\n')

    golden_tables = {f"{t}.csv": EXPECTED / f"{t}.csv" for t in runs_mod.TABLE_ORDER if t != "portfolio_before"}
    golden_tables["portfolio_before.csv"] = GOLDEN / "portfolio_before.csv"
    # the newest completed booking run: every golden table and the closing numbers
    _write_run(
        root / "2026-09-09" / "100000-paper",
        _meta(
            "paper",
            "2026-09-09T10:00:00+05:30",
            "completed",
            finished="2026-09-09T10:04:00+05:30",
            cash_before=25000.0,
            equity_before=1200000.0,
            positions_before=_positions(GOLDEN / "portfolio_before.csv"),
            universe_size=37,
            regime={"index_close": 25000.0, "ma200": 24000.0, "bull": True},
            ranked_count=22,
            last_resize_date_before="2026-08-26",
            resize_performed=True,
            last_resize_date_after="2026-09-09",
            cash_after=465996.5143,
            equity_after=1250000.0,
            positions_after=_positions(EXPECTED / "portfolio_after.csv"),
        ),
        tables=golden_tables,
        log=["2026-09-09 10:00:00 INFO pipeline:448 Portfolio value at start: 1175000.00", "... done"],
    )
    # an older completed paper run: only its closing numbers matter
    _write_run(
        root / "2026-09-02" / "100000-paper",
        _meta(
            "paper",
            "2026-09-02T10:00:00+05:30",
            "completed",
            finished="2026-09-02T10:03:00+05:30",
            cash_after=30000.0,
            equity_after=1180000.0,
            positions_after={"ANCHOR": 2000},
        ),
        tables={"portfolio_after.csv": GOLDEN / "portfolio_before.csv"},
    )
    # a plan: completed, books nothing, must stay out of the account and the equity series
    _write_run(
        root / "2026-09-10" / "183000-plan",
        _meta(
            "plan",
            "2026-09-10T18:30:00+05:30",
            "completed",
            finished="2026-09-10T18:31:00+05:30",
            cash_after=1.0,
            equity_after=1.0,
            positions_after={},
        ),
        tables={"ranking.csv": EXPECTED / "ranking.csv"},
    )
    # abandoned: run.json still says running and every file is an hour old
    _write_run(
        root / "2026-09-13" / "155259-paper",
        _meta(
            "paper",
            "2026-09-13T15:52:59+05:30",
            "running",
            universe_size=501,
            regime={"index_close": 23398.1, "ma200": 24543.87, "bull": False},
        ),
        tables={"universe.csv": EXPECTED / "universe.csv", "ranking.csv": EXPECTED / "ranking.csv"},
        log=["2026-09-13 15:53:00 INFO pipeline:476 Index 23398.10 vs 200-day MA 24543.87 → BEAR"],
        mtime=(NOW - timedelta(hours=1)).timestamp(),
    )
    # still running: a minute old
    _write_run(
        root / "2026-09-14" / "113000-paper",
        _meta("paper", "2026-09-14T11:30:00+05:30", "running"),
        tables={"portfolio_before.csv": GOLDEN / "portfolio_before.csv"},
        log=["starting"],
        mtime=(NOW - timedelta(minutes=1)).timestamp(),
    )
    # a failed live run: books, but never completed
    _write_run(
        root / "2026-09-01" / "100000-live",
        _meta("live", "2026-09-01T10:00:00+05:30", "failed:BrokerError", finished="2026-09-01T10:00:30+05:30"),
    )
    # the symlink the run leaves behind, which the list must skip
    (root / "latest").symlink_to("2026-09-14/113000-paper", target_is_directory=True)

    # two backtests (ADR-023 outputs)
    for label, end in (("slope", 199327.58), ("blend", 210000.0)):
        bt = root / "backtests" / f"2022-07-13_2026-09-09-{label}"
        bt.mkdir(parents=True)
        summary = {
            "note": "Relative comparison only: today's constituents over the whole range (survivorship bias).",
            "label": label,
            "from": "2022-07-13",
            "to": "2026-09-09",
            "weeks": 3,
            "start_equity": 100000.0,
            "end_equity": end,
            "cagr": 0.18,
            "max_drawdown": -0.1,
            "trades": 2,
        }
        (bt / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        (bt / "equity.csv").write_text(
            "date,cash,market_value,equity,positions,exposure\n"
            "2022-07-13,100000.000000,0.000000,100000.000000,0,0.000000\n"
            "2022-07-20,50000.000000,52000.000000,102000.000000,5,0.509804\n"
            f"2022-07-27,50000.000000,{end - 50000:.6f},{end:.6f},5,0.500000\n"
        )
        (bt / "weekly.csv").write_text(
            "date,bull,ranked,exits,buys,resize_performed,equity\n"
            "2022-07-13,false,153,0,0,true,100000.000000\n"
            "2022-07-20,true,181,0,5,false,102000.000000\n"
            f"2022-07-27,true,190,1,1,false,{end:.6f}\n"
        )
        (bt / "trades.csv").write_text(
            "date,timestamp,side,symbol,qty,price,fees_pct,slippage_pct,cash_delta,reason\n"
            "2022-07-20,2022-07-20T10:00:00+05:30,BUY,ELECON,10,169.2000,0.001500,0.000500,-1695.38,new_position\n"
            "2022-07-27,2022-07-27T10:00:00+05:30,SELL,ELECON,10,180.0000,0.001500,0.000500,1796.40,rank_cutoff\n"
        )
        (bt / "params.json").write_text(json.dumps({"params": {"score": label}}, indent=2) + "\n")
    return root


@pytest.fixture
def client(make_settings, runs_tree) -> TestClient:
    settings = make_settings(state_files=False, runs_dir=runs_tree)
    return TestClient(create_app(settings, now=lambda: NOW, allowed_hosts=["testserver"], token="tok"))


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _chart_data(html: str) -> dict:
    return json.loads(html.split('id="equity-data">')[1].split("</script>")[0])


# ── runs ─────────────────────────────────────────────────────────────────────
def test_runs_page_lists_every_run_newest_first_with_its_status(client):
    html = client.get("/runs").text
    order = [
        "2026-09-14/113000-paper",
        ABANDONED,
        "2026-09-10/183000-plan",
        NEWEST,
        "2026-09-02/100000-paper",
        "2026-09-01/100000-live",
    ]
    positions = [html.index(f'href="/runs/{run_id}"') for run_id in order]
    assert positions == sorted(positions)
    assert "6 shown" in html
    assert html.count("status-completed") == 3
    for status in ("running", "abandoned", "failed"):
        assert html.count(f"status-{status}") == 1, status
    assert "latest" not in html.split("<tbody>")[1]


def test_runs_page_filters_by_mode_and_by_status(client):
    html = client.get("/runs", params={"mode": "plan"}).text
    assert "183000-plan" in html and "100000-paper" not in html
    html = client.get("/runs", params={"status": "abandoned"}).text
    assert "155259-paper" in html and "113000-paper" not in html
    assert "no runs under" in client.get("/runs", params={"mode": "kill"}).text


def test_run_summary_shows_the_rail_and_the_closing_numbers(client):
    html = client.get(f"/runs/{NEWEST}").text
    assert html.count('<li class="done">') == 12
    assert "465,996.51" in html and "1,250,000.00" in html and "BULL" in html
    assert "status-completed" in html and "abc1234" in html
    assert "performed" in html and "2026-08-26" in html


def test_an_abandoned_run_is_named_and_its_rail_stops_where_the_files_do(client):
    html = client.get(f"/runs/{ABANDONED}").text
    assert "Abandoned" in html and "status-abandoned" in html
    assert html.count('<li class="done">') == 3  # universe size, regime and the ranking are the only evidence
    assert "BEAR" in html
    still_running = client.get("/runs/2026-09-14/113000-paper").text
    assert "status-running" in still_running and "Abandoned" not in still_running


def test_a_table_shows_the_file_cell_for_cell_and_marks_the_held_names(client):
    html = client.get(f"/runs/{NEWEST}/ranking").text
    rows = _rows(EXPECTED / "ranking.csv")
    for row in rows:
        for value in row.values():
            assert f">{value}<" in html, value
    assert html.count('<tr class="held">') == sum(r["held"] == "true" for r in rows)
    assert f"{len(rows)} rows" in html


def test_sorting_is_a_round_trip_and_htmx_gets_only_the_table(client):
    url = f"/runs/{NEWEST}/ranking"
    full = client.get(url, params={"sort": "score", "desc": "true"}).text
    assert "<html" in full
    partial = client.get(url, params={"sort": "score", "desc": "true"}, headers={"HX-Request": "true"}).text
    assert "<html" not in partial and "<table" in partial
    rows = _rows(EXPECTED / "ranking.csv")
    top = max(rows, key=lambda r: float(r["score"]))["score"]
    first_row = partial.split("<tbody>")[1].split("</tr>")[0]
    assert f">{top}<" in first_row
    ascending = client.get(url, params={"sort": "score"}, headers={"HX-Request": "true"}).text
    bottom = min(rows, key=lambda r: float(r["score"]))["score"]
    assert f">{bottom}<" in ascending.split("<tbody>")[1].split("</tr>")[0]


def test_sort_rows_puts_numbers_first_then_text_then_blanks():
    rows = [{"v": "b"}, {"v": ""}, {"v": "10"}, {"v": "9.5"}, {"v": "a"}]
    assert [r["v"] for r in runs_mod.sort_rows(rows, "v")] == ["9.5", "10", "a", "b", ""]
    assert [r["v"] for r in runs_mod.sort_rows(rows, "v", descending=True)] == ["", "b", "a", "10", "9.5"]
    assert runs_mod.sort_rows(rows, None) is rows


def test_headerless_portfolio_tables_get_their_columns(client):
    html = client.get(f"/runs/{NEWEST}/portfolio_after").text
    assert ">symbol<" in html and ">quantity<" in html
    for sym, qty in _positions(EXPECTED / "portfolio_after.csv").items():
        assert f">{sym}<" in html and f">{qty}<" in html


def test_a_table_not_written_yet_shows_its_columns_and_says_so(client):
    html = client.get(f"/runs/{ABANDONED}/exits").text
    assert "not written yet" in html
    for column in EXIT_COLUMNS:
        assert f">{column}<" in html, column


def test_log_and_meta_tabs(client):
    assert "Portfolio value at start: 1175000.00" in client.get(f"/runs/{NEWEST}/log").text
    meta = client.get(f"/runs/{NEWEST}/meta").text
    assert "equity_after" in meta and "1250000.0" in meta


def test_unknown_runs_tables_and_path_tricks_are_not_found(client):
    assert client.get("/runs/2026-09-09/999999-paper").status_code == 404
    assert client.get(f"/runs/{NEWEST}/nonsense").status_code == 404
    assert client.get("/runs/..%2F..%2F/100000-paper").status_code == 404
    assert client.get("/runs/2026-09-09/..%2F..%2Fcash_ledger.csv").status_code == 404
    assert client.get("/research/..%2F..").status_code == 404


def test_a_host_outside_loopback_is_refused(client):
    assert client.get("/runs", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/runs").status_code == 200


# ── the account ──────────────────────────────────────────────────────────────
def test_account_page_reads_the_newest_booking_run_and_the_ledgers(client):
    html = client.get("/account").text
    assert f"/runs/{NEWEST}" in html and "465,996.51" in html and "1,250,000.00" in html
    for sym, qty in _positions(EXPECTED / "portfolio_after.csv").items():
        assert f">{sym}<" in html and f">{qty}<" in html
    # the equity series: the two completed paper runs; the plan and the failed live run stay out
    data = _chart_data(html)
    assert data["labels"] == ["paper"]
    assert data["series"] == [[1180000.0, 1250000.0]]
    assert data["x"] == [runs_mod.epoch_seconds("2026-09-02"), runs_mod.epoch_seconds("2026-09-09")]
    # the ledgers and the state file as they are on disk
    assert ">deposit<" in html and ">HOLDFAST<" in html and ">-240480.00<" in html
    assert "read-only" in html and "2026-09-09" in html


def test_account_page_before_the_first_booking_run(make_settings, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    client = TestClient(create_app(make_settings(state_files=False, runs_dir=empty), allowed_hosts=["testserver"]))
    html = client.get("/account").text
    assert "No completed booking run" in html
    assert client.get("/runs").status_code == 200 and client.get("/research").status_code == 200


# ── research ─────────────────────────────────────────────────────────────────
def test_research_compares_the_backtests_and_pins_the_note(client):
    html = client.get("/research").text
    assert html.index("Read first") < html.index("Summary side by side")
    assert "Relative comparison only" in html
    assert ">slope<" in html and ">blend<" in html
    assert ">199327.58<" in html and ">210000.0<" in html
    data = _chart_data(html)
    assert set(data["labels"]) == {"slope", "blend"} and len(data["x"]) == 3
    assert html.count('class="bull"') == 2 and html.count('class="bear"') == 1
    only = client.get("/research", params={"select": "2022-07-13_2026-09-09-slope"}).text
    assert _chart_data(only)["labels"] == ["slope"]
    assert ">210000.0<" not in only


def test_backtest_page_shows_the_weekly_table_the_trades_and_the_params(client):
    url = "/research/2022-07-13_2026-09-09-slope"
    html = client.get(url).text
    assert ">ELECON<" in html and ">new_position<" in html and ">rank_cutoff<" in html
    assert ">153<" in html and "&#34;score&#34;: &#34;slope&#34;" in html
    partial = client.get(url, params={"sort": "cash_delta", "desc": "true"}, headers={"HX-Request": "true"}).text
    assert ">1796.40<" in partial.split("<tbody>")[1].split("</tr>")[0]


# ── settings ─────────────────────────────────────────────────────────────────
def test_settings_page_is_redacted_and_marks_what_differs_from_the_defaults(client):
    html = client.get("/settings").text
    assert "test-key" not in html and "test-secret" not in html
    assert "KITE_API_KEY" not in html and "KITE_API_SECRET" not in html
    assert '<tr class="changed"><th>RUNS_DIR</th>' in html  # the tmp path is not the default
    assert '<tr class="changed"><th>KITE_OPEN_BROWSER</th>' in html  # the fixture turns it off
    assert '<tr class=""><th>TRADING_WEEKDAY</th>' in html  # untouched
    assert '<tr class=""><th>PORTFOLIO_FILE</th>' in html  # derived from RUNS_DIR, so at its default


# ── the command ──────────────────────────────────────────────────────────────
def test_parse_args_defaults_to_the_console_port():
    assert parse_args([]).port == 8766
    assert parse_args(["--port", "9000"]).port == 9000


def test_main_refuses_a_bad_environment_with_the_settings_error(monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "")
    monkeypatch.setenv("KITE_API_SECRET", "secret")
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


# ── stage two: the Today page, the launcher, the streamed log ────────────────
STUB = """
import json, os, pathlib
mode = "plan" if os.environ.get("PLAN_ONLY") == "1" else "paper"
print("Log in to Kite:")
print("  https://kite.zerodha.com/connect/login?api_key=KEY123&v=3")
plan, allow = os.environ.get("PLAN_ONLY"), os.environ.get("ALLOW_KITE_EXECUTION")
print("PLAN_ONLY=%s ALLOW_KITE_EXECUTION=%s" % (plan, allow))
d = pathlib.Path(os.environ["RUNS_DIR"]) / "2026-09-14" / ("120500-" + mode)
d.mkdir(parents=True)
meta = {"started": "2026-09-14T12:05:00+05:30", "finished": "2026-09-14T12:06:00+05:30", "status": "completed",
        "mode": mode, "equity_after": 5.0, "positions_after": {}}
(d / "run.json").write_text(json.dumps(meta))
"""
SLOW = "import time; time.sleep(30)"


def _launcher(runs_tree: Path, *, allow: bool = False, command=None) -> Launcher:
    env = {"RUNS_DIR": str(runs_tree), "ALLOW_KITE_EXECUTION": "1" if allow else "0"}
    return Launcher(command or [sys.executable, "-c", STUB], env=env, now=lambda: NOW)


@pytest.fixture
def launched(make_settings, runs_tree):
    """``launched(allow=..., kill=..., command=...)`` -> (client, launcher) over a stub child, killed at teardown."""
    made: list[Launcher] = []

    def make(*, allow: bool = False, kill: bool = False, command=None) -> tuple[TestClient, Launcher]:
        settings = make_settings(state_files=False, runs_dir=runs_tree, allow_kite_execution=allow, kill_switch=kill)
        launcher = _launcher(runs_tree, allow=allow, command=command)
        made.append(launcher)
        app = create_app(settings, now=lambda: NOW, allowed_hosts=["testserver"], launcher=launcher, token="tok")
        return TestClient(app), launcher

    yield make
    for launcher in made:
        child = launcher.current
        if child is not None and child.running:
            child.process.kill()
            child.wait(10)


def _start(client: TestClient, **fields):
    return client.post("/runs/start", data={"_token": "tok", **fields}, follow_redirects=False)


def test_launcher_overlays_only_the_mode_and_captures_the_output(runs_tree):
    launcher = _launcher(runs_tree)
    child = launcher.start("plan")
    assert child.wait(20) == 0
    assert "PLAN_ONLY=1 ALLOW_KITE_EXECUTION=0" in child.lines
    assert (runs_tree / "2026-09-14" / "120500-plan" / "run.json").exists()
    assert not launcher.busy
    with pytest.raises(ValueError):
        launcher.start("live")


def test_launcher_runs_one_child_at_a_time(runs_tree):
    launcher = _launcher(runs_tree, command=[sys.executable, "-c", SLOW])
    child = launcher.start("book")
    try:
        assert launcher.busy
        with pytest.raises(LauncherBusy):
            launcher.start("plan")
    finally:
        child.process.kill()
        child.wait(10)
    assert not launcher.busy and child.returncode != 0


def test_today_page_shows_the_weekday_guard_the_session_and_the_newest_run(launched, tmp_path):
    client, _ = launched()
    record = SessionRecord(
        api_key="test-key", access_token="SECRET-TOKEN", user_id="KW1234", issued_at=NOW - timedelta(hours=1)
    )
    (tmp_path / "kite_session.json").write_text(record.to_json())
    html = client.get("/").text
    assert "Monday" in html and "TRADING_WEEKDAY=2 (Wednesday)" in html and "weekday guard" in html
    assert "live for" in html and "KW1234" in html and "SECRET-TOKEN" not in html
    assert "2026-09-14/113000-paper" in html
    assert 'value="plan"' in html and "Book: paper" in html and "Book: LIVE" not in html
    assert 'name="_token" value="tok"' in html


def test_today_page_without_a_session(launched):
    client, _ = launched()
    assert "none cached" in client.get("/").text


def test_a_plan_is_started_from_the_page_and_the_run_it_made_appears(launched):
    client, launcher = launched()
    response = _start(client, mode="plan")
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert launcher.current is not None and launcher.current.wait(20) == 0
    html = client.get("/").text
    assert "PLAN_ONLY=1 ALLOW_KITE_EXECUTION=0" in html and "exited 0" in html
    assert "2026-09-14/120500-plan" in html
    # the login URL is a link, never text
    assert 'href="https://kite.zerodha.com/connect/login?api_key=KEY123&amp;v=3"' in html
    assert html.count("api_key=KEY123") == 1
    assert "open the Kite login" in html
    assert 'hx-trigger="every 2s"' not in html  # nothing left to poll


def test_a_post_without_the_token_or_from_another_host_starts_nothing(launched):
    client, launcher = launched()
    assert client.post("/runs/start", data={"mode": "plan"}).status_code == 403
    assert client.post("/runs/start", data={"mode": "plan", "_token": "wrong"}).status_code == 403
    assert (
        client.post("/runs/start", data={"mode": "plan", "_token": "tok"}, headers={"host": "evil.example"}).status_code
        == 400
    )
    assert _start(client, mode="kill").status_code == 400
    assert launcher.current is None
    # the header is the other way to present it
    ok = client.post("/runs/start", data={"mode": "plan"}, headers={"X-Console-Token": "tok"}, follow_redirects=False)
    assert ok.status_code == 303 and launcher.current is not None


def test_a_live_booking_run_needs_the_word_typed(launched):
    client, launcher = launched(allow=True)
    page = client.get("/").text
    assert "Book: LIVE" in page and "sends orders to Kite" in page
    assert _start(client, mode="book").status_code == 403 and launcher.current is None
    assert _start(client, mode="book", confirm="live").status_code == 403 and launcher.current is None
    assert _start(client, mode="book", confirm="LIVE").status_code == 303
    assert launcher.current is not None and launcher.current.wait(20) == 0
    assert "PLAN_ONLY=0 ALLOW_KITE_EXECUTION=1" in launcher.current.lines


def test_a_paper_booking_run_needs_no_word_and_a_kill_switch_gets_no_button(launched):
    client, launcher = launched()
    assert _start(client, mode="book").status_code == 303
    assert launcher.current is not None and launcher.current.wait(20) == 0
    assert "PLAN_ONLY=0 ALLOW_KITE_EXECUTION=0" in launcher.current.lines
    killer, kill_launcher = launched(kill=True)
    page = killer.get("/").text
    assert "does not start one" in page and 'value="book"' not in page and 'value="plan"' in page
    assert _start(killer, mode="book").status_code == 409 and kill_launcher.current is None


def test_a_second_start_while_a_child_runs_is_refused_and_the_panel_polls(launched):
    client, launcher = launched(command=[sys.executable, "-c", SLOW])
    assert _start(client, mode="plan").status_code == 303
    assert launcher.current is not None
    try:
        refused = _start(client, mode="plan")
        assert refused.status_code == 409 and "already going" in refused.text
        html = client.get("/").text
        assert 'hx-trigger="every 2s"' in html and "status-running" in html
        assert "<form" not in html.split('id="launch"')[1]
        partial = client.get("/launch").text
        assert "<html" not in partial and 'id="launch"' in partial
    finally:
        launcher.current.process.kill()
        launcher.current.wait(10)


def test_the_log_stream_sends_the_lines_then_done(client):
    with client.stream("GET", f"/runs/{NEWEST}/log/stream") as response:
        body = "".join(response.iter_text())
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: 2026-09-09 10:00:00 INFO pipeline:448 Portfolio value at start: 1175000.00\n\n" in body
    assert body.rstrip().endswith("event: done\ndata: end")
    with client.stream("GET", f"/runs/{NEWEST}/log/stream", params={"skip": 1}) as response:
        body = "".join(response.iter_text())
    assert "Portfolio value" not in body and "data: ... done" in body


def test_the_rail_polls_while_a_run_is_running_and_rests_after(client):
    partial = client.get("/runs/2026-09-14/113000-paper/rail", params={"tab": "log"}).text
    assert "<html" not in partial and 'hx-trigger="every 3s"' in partial and 'class="active"' in partial
    page = client.get("/runs/2026-09-14/113000-paper/log").text
    assert 'hx-get="/runs/2026-09-14/113000-paper/rail?tab=log"' in page
    assert "new EventSource('/runs/2026-09-14/113000-paper/log/stream?skip=1')" in page
    done = client.get(f"/runs/{NEWEST}/log").text
    assert "hx-trigger" not in done and "EventSource" not in done


# ── stage three: promote and the cashflow row, the only two file writes ──────
WRITE_CALLS = {
    "open",
    "write_text",
    "write_bytes",
    "copy",
    "copyfile",
    "copy2",
    "move",
    "rename",
    "unlink",
    "rmtree",
    "mkdir",
    "utime",
    "append_cashflow",
    "_append_row",
    "save_state",
    "write_portfolio",
}


def _callee(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _open_mode(node: ast.Call) -> str:
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        return str(node.args[1].value)
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return "r"


def test_every_file_write_under_ui_lives_in_the_writes_module():
    """ADR-031: promote and the cashflow row are the only file writes, and the trades ledger is never named there."""
    for py in sorted(UI_DIR.rglob("*.py")):
        if py.name == "writes.py":
            continue
        for node in ast.walk(ast.parse(py.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name == "open":
                assert not set(_open_mode(node)) & set("wax+"), f"{py.name} opens a file for writing"
            else:
                assert name not in WRITE_CALLS, f"{py.name} calls {name}()"
    writes_source = (UI_DIR / "writes.py").read_text()
    assert "trades" not in writes_source.lower()


def _ledger_bytes(runs_tree: Path) -> tuple[bytes, bytes]:
    return (runs_tree / "trades_ledger.csv").read_bytes(), (runs_tree / "cash_ledger.csv").read_bytes()


@pytest.fixture
def settled(runs_tree) -> Path:
    """The fixture tree with the running booking run finished, and current older than next, so a promote is due."""
    running = runs_tree / "2026-09-14" / "113000-paper" / "run.json"
    meta = json.loads(running.read_text())
    meta.update(status="aborted:empty_universe", finished="2026-09-14T11:31:00+05:30")
    running.write_text(json.dumps(meta))
    old = (NOW - timedelta(days=7)).timestamp()
    os.utime(runs_tree / "current_portfolio.csv", (old, old))
    return runs_tree


def test_promote_page_shows_the_diff_and_the_orders_of_the_newest_booking_run(client, settled):
    html = client.get("/promote").text
    current = _positions(GOLDEN / "portfolio_before.csv")
    following = _positions(EXPECTED / "portfolio_after.csv")
    for sym in set(current) | set(following):
        assert f"<td>{sym}</td>" in html
    closed = [s for s in current if s not in following]
    new = [s for s in following if s not in current]
    assert closed and new
    assert html.count('class="change-closed"') == len(closed)
    assert html.count('class="change-new"') == len(new)
    assert html.count('class="change-same"') == len([s for s in current if following.get(s) == current[s]])
    assert "FAKE-0001" in html and ">COMPLETE<" in html  # the run's orders beside the diff
    assert 'action="/promote"' in html and "Promote: copy next over current" in html
    assert f"/runs/{NEWEST}" in html


def test_promote_copies_the_bytes_once_and_then_has_nothing_to_offer(client, settled):
    before = _ledger_bytes(settled)
    response = client.post("/promote", data={"_token": "tok"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/promote?done=true"
    assert (settled / "current_portfolio.csv").read_bytes() == (settled / "next_portfolio.csv").read_bytes()
    assert _ledger_bytes(settled) == before
    html = client.get("/promote", params={"done": "true"}).text
    assert "Promoted." in html
    assert "nothing new to promote" in html and 'action="/promote"' not in html
    assert client.post("/promote", data={"_token": "tok"}).status_code == 409


def test_promote_is_refused_without_the_token_or_while_a_booking_run_is_going(client, runs_tree):
    original = (runs_tree / "current_portfolio.csv").read_bytes()
    assert client.post("/promote", data={}).status_code == 403
    # the fixture tree has a paper run still running: not offered, and the POST is refused
    page = client.get("/promote").text
    assert "a booking run is going" in page and 'action="/promote"' not in page
    assert client.post("/promote", data={"_token": "tok"}).status_code == 409
    assert (runs_tree / "current_portfolio.csv").read_bytes() == original


def test_promote_is_refused_when_next_is_not_what_the_newest_run_wrote(client, settled):
    (settled / "next_portfolio.csv").write_text("ANCHOR,1\n")
    page = client.get("/promote").text
    assert "is not what the newest completed booking run" in page
    assert client.post("/promote", data={"_token": "tok"}).status_code == 409
    assert (settled / "current_portfolio.csv").read_bytes() == (GOLDEN / "portfolio_before.csv").read_bytes()


def test_a_cashflow_row_is_appended_through_the_ledger_module(client, runs_tree):
    trades_before, cash_before = _ledger_bytes(runs_tree)
    response = client.post(
        "/account/cashflow",
        data={"_token": "tok", "amount": "25,000", "note": "  September deposit ", "day": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/account"
    trades_after, cash_after = _ledger_bytes(runs_tree)
    assert trades_after == trades_before
    assert cash_after == cash_before + b"2026-09-14,25000.00,September deposit\r\n"
    dated = client.post(
        "/account/cashflow",
        data={"_token": "tok", "amount": "-1500.5", "note": "withdrawal", "day": "2026-09-12"},
        follow_redirects=False,
    )
    assert dated.status_code == 303
    assert (runs_tree / "cash_ledger.csv").read_bytes().endswith(b"2026-09-12,-1500.50,withdrawal\r\n")
    html = client.get("/account").text
    assert ">September deposit<" in html and ">-1500.50<" in html and 'value="2026-09-14"' in html


def test_a_bad_cashflow_is_refused_and_writes_nothing(client, runs_tree):
    before = _ledger_bytes(runs_tree)
    for fields in (
        {"amount": "abc", "note": "x"},
        {"amount": "0", "note": "x"},
        {"amount": "100", "note": "   "},
        {"amount": "100", "note": "x", "day": "yesterday"},
        {"amount": "inf", "note": "x"},
    ):
        response = client.post("/account/cashflow", data={"_token": "tok", **fields})
        assert response.status_code == 400, fields
        assert "Not recorded" in response.text
    assert client.post("/account/cashflow", data={"amount": "100", "note": "x"}).status_code == 403
    assert _ledger_bytes(runs_tree) == before


def test_append_cashflow_creates_the_ledger_with_its_header(tmp_path):
    path = tmp_path / "cash_ledger.csv"
    append_cashflow(str(path), date(2026, 9, 14), 250000, "seed")
    assert path.read_bytes() == b"date,amount,note\r\n2026-09-14,250000.00,seed\r\n"
