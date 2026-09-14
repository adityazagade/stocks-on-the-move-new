"""Reading the runs tree (ADR-006, ADR-029) for the console: run directories, their tables, the account files.

Everything here is read-only. A run directory is ``<runs_dir>/<YYYY-MM-DD>/<HHMMSS>-<mode>[-<n>]/``;
its ``run.json`` is the record, its CSV tables are the decisions, its ``run.log`` is the narrative.
The tree is the index: nothing is cached and there is no database (ADR-031).
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from stocks_on_the_move.context import IST
from stocks_on_the_move.execution import ORDER_COLUMNS
from stocks_on_the_move.ledger import TRADE_COLUMNS
from stocks_on_the_move.reporting import (
    CANDIDATE_COLUMNS,
    EXIT_COLUMNS,
    RANKING_COLUMNS,
    SIZING_COLUMNS,
    UNIVERSE_COLUMNS,
)

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^\d{6}-[a-z]+(-\d+)?$")
ABANDONED_AFTER = timedelta(minutes=10)  # a run still "running" whose log has been silent this long (ADR-031)
BOOKING_MODES = frozenset({"paper", "live", "kill"})  # a plan books nothing (ADR-022)

PORTFOLIO_COLUMNS = ["symbol", "quantity"]  # the header-less SYMBOL,QUANTITY files
TABLE_COLUMNS: dict[str, list[str]] = {
    "universe": UNIVERSE_COLUMNS,
    "ranking": RANKING_COLUMNS,
    "exits": EXIT_COLUMNS,
    "sizing": SIZING_COLUMNS,
    "candidates": CANDIDATE_COLUMNS,
    "orders": ORDER_COLUMNS,
    "trades": TRADE_COLUMNS,
    "portfolio_before": PORTFOLIO_COLUMNS,
    "portfolio_after": PORTFOLIO_COLUMNS,
}
HEADERLESS = frozenset({"portfolio_before", "portfolio_after"})
TABLE_ORDER = [
    "universe",
    "ranking",
    "exits",
    "sizing",
    "candidates",
    "orders",
    "trades",
    "portfolio_before",
    "portfolio_after",
]

# The rail: the pipeline's twelve steps and the evidence each leaves behind (ADR-006). Steps without an
# artifact of their own (3, 8 and 10) take the evidence of the step that follows them.
STEPS: list[tuple[str, str]] = [
    ("Log in", "portfolio_before.csv"),
    ("Load state", "portfolio_before.csv"),
    ("Tokens", "meta:equity_before"),
    ("Universe", "meta:universe_size"),
    ("Regime", "meta:regime"),
    ("Rank", "ranking.csv"),
    ("Exits", "exits.csv"),
    ("Raise cash", "sizing.csv"),
    ("Resize", "sizing.csv"),
    ("Mark to market", "candidates.csv"),
    ("Buys", "candidates.csv"),
    ("Snapshot", "portfolio_after.csv"),
]


@dataclass(frozen=True)
class Table:
    columns: list[str]
    rows: list[dict[str, str]]


@dataclass(frozen=True)
class RunInfo:
    path: Path
    day: str
    name: str
    meta: dict[str, Any]
    display_status: str  # running | abandoned | completed | aborted | failed | unknown
    files: frozenset[str]

    @property
    def id(self) -> str:
        return f"{self.day}/{self.name}"

    @property
    def mode(self) -> str:
        return str(self.meta.get("mode") or self.name.split("-")[1])

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "unknown")

    @property
    def books(self) -> bool:
        return self.mode in BOOKING_MODES

    @property
    def started(self) -> str:
        return str(self.meta.get("started") or "")

    @property
    def finished(self) -> str:
        return str(self.meta.get("finished") or "")

    def has(self, evidence: str) -> bool:
        """``meta:<key>`` for a ``run.json`` field, otherwise a file name in the run directory."""
        if evidence.startswith("meta:"):
            return self.meta.get(evidence[5:]) is not None
        return evidence in self.files

    def rail(self) -> list[tuple[str, bool]]:
        return [(label, self.has(evidence)) for label, evidence in STEPS]

    def tables(self) -> list[str]:
        return [t for t in TABLE_ORDER if f"{t}.csv" in self.files]


def _display_status(meta: dict[str, Any], path: Path, now: datetime) -> str:
    status = str(meta.get("status") or "unknown")
    if status != "running":
        return status.split(":", 1)[0]
    stamps = [p.stat().st_mtime for p in (path / "run.log", path / "run.json") if p.exists()]
    if not stamps:
        return "running"
    silent = now - datetime.fromtimestamp(max(stamps), tz=IST)
    return "abandoned" if silent > ABANDONED_AFTER else "running"


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        data = json.loads((path / "run.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_run(runs_dir: Path, day: str, name: str, now: datetime) -> RunInfo | None:
    """The run at ``<runs_dir>/<day>/<name>``, or None when the names are not a run's or nothing is there."""
    if not DAY_RE.match(day) or not NAME_RE.match(name):
        return None
    path = runs_dir / day / name
    if not path.is_dir():
        return None
    meta = _read_meta(path)
    files = frozenset(p.name for p in path.iterdir() if p.is_file())
    return RunInfo(
        path=path, day=day, name=name, meta=meta, display_status=_display_status(meta, path, now), files=files
    )


def list_runs(runs_dir: Path, now: datetime) -> list[RunInfo]:
    """Every run directory under ``runs_dir``, newest first."""
    runs: list[RunInfo] = []
    if not runs_dir.is_dir():
        return runs
    for day_dir in runs_dir.iterdir():
        if day_dir.is_symlink() or not day_dir.is_dir() or not DAY_RE.match(day_dir.name):
            continue
        for run_dir in day_dir.iterdir():
            if run_dir.is_dir() and NAME_RE.match(run_dir.name):
                run = load_run(runs_dir, day_dir.name, run_dir.name, now)
                if run is not None:
                    runs.append(run)
    runs.sort(key=lambda r: (r.day, r.name), reverse=True)
    return runs


def newest_booking_run(runs: Iterable[RunInfo]) -> RunInfo | None:
    """The newest completed run that booked: what the account looks like now."""
    for run in runs:  # newest first
        if run.books and run.display_status == "completed":
            return run
    return None


# -- tables -------------------------------------------------------------------
def read_csv(path: Path, columns: list[str] | None = None, *, headerless: bool = False) -> Table:
    """A CSV as a ``Table``; ``columns`` names a header-less file's columns and stands in for an absent file."""
    if not path.is_file():
        return Table(columns=list(columns or []), rows=[])
    with path.open(newline="") as f:
        if headerless:
            cols = list(columns or [])
            rows = [dict(zip(cols, r, strict=False)) for r in csv.reader(f) if r]
            return Table(columns=cols, rows=rows)
        reader = csv.DictReader(f)
        rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]
        return Table(columns=list(reader.fieldnames or columns or []), rows=rows)


def read_table(run: RunInfo, name: str) -> Table | None:
    """One of the run's tables by name, or None for a name that is not a table."""
    if name not in TABLE_COLUMNS:
        return None
    return read_csv(run.path / f"{name}.csv", TABLE_COLUMNS[name], headerless=name in HEADERLESS)


def _sort_key(value: str) -> tuple[int, float, str]:
    if value == "":
        return (2, 0.0, "")
    try:
        return (0, float(value), "")
    except ValueError:
        return (1, 0.0, value.lower())


def sort_rows(rows: list[dict[str, str]], column: str | None, *, descending: bool = False) -> list[dict[str, str]]:
    """Rows ordered on one column, numbers as numbers, text after them, blanks last."""
    if not column:
        return rows
    return sorted(rows, key=lambda r: _sort_key(r.get(column, "")), reverse=descending)


def is_number(value: object) -> bool:
    text = str(value)
    if text == "":
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def read_log(run: RunInfo, *, max_lines: int | None = 2000) -> list[str]:
    """The run's log lines, the last ``max_lines`` of them, or all of them for ``None``."""
    path = run.path / "run.log"
    if not path.is_file():
        return []
    lines = path.read_text(errors="replace").splitlines()
    return lines if max_lines is None else lines[-max_lines:]


def epoch_seconds(day: str) -> int:
    """Midnight IST of an ISO date, as the epoch seconds uPlot plots."""
    return int(datetime.combine(date.fromisoformat(day), time.min, tzinfo=IST).timestamp())


# -- the account ------------------------------------------------------------------
@dataclass(frozen=True)
class EquityPoint:
    date: str  # the run's start date
    equity: float
    run: RunInfo


def equity_series(runs: Iterable[RunInfo]) -> dict[str, list[EquityPoint]]:
    """``equity_after`` of every completed booking run, oldest first, one series per mode; plans excluded."""
    series: dict[str, list[EquityPoint]] = {}
    for run in sorted(runs, key=lambda r: (r.day, r.name)):
        equity = run.meta.get("equity_after")
        if not (run.books and run.display_status == "completed" and isinstance(equity, int | float)):
            continue
        point = EquityPoint(date=run.started[:10] or run.day, equity=float(equity), run=run)
        series.setdefault(run.mode, []).append(point)
    return series


def aligned_chart(curves: dict[str, dict[str, float]]) -> dict[str, Any]:
    """uPlot's aligned form: one x axis of epoch seconds, one y series per label, null where a date is missing."""
    dates = sorted({d for curve in curves.values() for d in curve})
    return {
        "labels": list(curves),
        "x": [epoch_seconds(d) for d in dates],
        "series": [[curve.get(d) for d in dates] for curve in curves.values()],
    }
