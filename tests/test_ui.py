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
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stocks_on_the_move.context import IST
from stocks_on_the_move.reporting import EXIT_COLUMNS
from stocks_on_the_move.ui import runs as runs_mod
from stocks_on_the_move.ui.app import create_app, main, parse_args

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
    return TestClient(create_app(settings, now=lambda: NOW, allowed_hosts=["testserver"]))


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
