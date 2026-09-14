"""Reading ``runs/backtests/`` (ADR-023) for the research page. Read-only."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stocks_on_the_move.ui.runs import Table, aligned_chart, read_csv

BACKTEST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SUMMARY_FIELDS = [  # the display order of summary.json (ADR-023); the note is pinned above the table
    "from",
    "to",
    "weeks",
    "start_equity",
    "end_equity",
    "cagr",
    "annual_volatility",
    "return_over_volatility",
    "max_drawdown",
    "max_drawdown_peak",
    "max_drawdown_trough",
    "avg_positions",
    "max_positions",
    "avg_exposure",
    "trades",
    "trades_per_year",
    "turnover_per_year",
]


@dataclass(frozen=True)
class Backtest:
    path: Path
    name: str
    summary: dict[str, Any]
    modified: float

    @property
    def label(self) -> str:
        return str(self.summary.get("label") or self.name)

    @property
    def note(self) -> str:
        return str(self.summary.get("note") or "")


def backtests_dir(runs_dir: Path) -> Path:
    return runs_dir / "backtests"


def load_backtest(runs_dir: Path, name: str) -> Backtest | None:
    if not BACKTEST_RE.match(name):
        return None
    path = backtests_dir(runs_dir) / name
    summary_path = path / "summary.json"
    if not summary_path.is_file():
        return None
    try:
        summary = json.loads(summary_path.read_text())
    except ValueError:
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    return Backtest(path=path, name=name, summary=summary, modified=summary_path.stat().st_mtime)


def list_backtests(runs_dir: Path) -> list[Backtest]:
    """Every result directory with a summary, newest first by the summary's modification time, as ``compare`` sorts."""
    root = backtests_dir(runs_dir)
    if not root.is_dir():
        return []
    found = [load_backtest(runs_dir, p.name) for p in root.iterdir() if p.is_dir()]
    return sorted((b for b in found if b is not None), key=lambda b: b.modified, reverse=True)


def equity_curve(bt: Backtest) -> dict[str, float]:
    curve: dict[str, float] = {}
    for row in read_csv(bt.path / "equity.csv").rows:
        try:
            curve[row["date"]] = float(row["equity"])
        except (KeyError, ValueError):
            continue
    return curve


def weekly(bt: Backtest) -> Table:
    return read_csv(bt.path / "weekly.csv")


def trades(bt: Backtest) -> Table:
    return read_csv(bt.path / "trades.csv")


def params(bt: Backtest) -> str:
    path = bt.path / "params.json"
    return path.read_text() if path.is_file() else ""


def regime_ribbon(bt: Backtest) -> list[tuple[str, bool]]:
    """One (date, bull) per run date of the replay, from ``weekly.csv``."""
    return [(row.get("date", ""), row.get("bull", "") == "true") for row in weekly(bt).rows]


def comparison(bts: list[Backtest]) -> list[tuple[str, list[str]]]:
    """The side-by-side rows: a field and one cell per backtest, the summary's fields first, any extra after."""
    known = set(SUMMARY_FIELDS) | {"note", "label"}
    extra = sorted({k for b in bts for k in b.summary} - known)
    return [(f, [_cell(b.summary.get(f)) for b in bts]) for f in [*SUMMARY_FIELDS, *extra]]


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def chart_data(bts: list[Backtest]) -> dict[str, Any]:
    return aligned_chart({b.label: equity_curve(b) for b in bts})
