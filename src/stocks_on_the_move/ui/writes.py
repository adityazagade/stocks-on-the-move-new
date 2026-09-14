"""The console's two file writes (ADR-031): promote the next portfolio and record a cashflow. Nothing else writes.

Everything else in the package reads. ``tests/test_ui.py`` scans the package
for calls that write and expects them in this module alone, so no path that
was not meant to can touch the ledger cash is reconstructed from, the
strategy's own state file or a run directory; the ledger of fills is not even
named here, and the test checks that too.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from stocks_on_the_move.ledger import append_cashflow
from stocks_on_the_move.ui.runs import PORTFOLIO_COLUMNS, RunInfo, newest_booking_run, read_csv


def _portfolio(path: Path) -> dict[str, str]:
    return {row["symbol"]: row["quantity"] for row in read_csv(path, PORTFOLIO_COLUMNS, headerless=True).rows}


def portfolio_diff(current_path: Path, next_path: Path) -> list[dict[str, str]]:
    """One row per symbol in either file: its current and next quantity and the kind of change."""
    current, following = _portfolio(current_path), _portfolio(next_path)
    rows = []
    for symbol in sorted(set(current) | set(following)):
        before, after = current.get(symbol, ""), following.get(symbol, "")
        if before == after:
            change = "same"
        elif not before:
            change = "new"
        elif not after:
            change = "closed"
        else:
            change = "changed"
        rows.append({"symbol": symbol, "current": before, "next": after, "change": change})
    return rows


@dataclass(frozen=True)
class Promotion:
    """Whether the copy is offered, and the reason when it is not."""

    allowed: bool
    reason: str


def promotion_check(current_path: Path, next_path: Path, runs: Iterable[RunInfo]) -> Promotion:
    """The copy is offered only for the file the newest completed booking run wrote, with no booking run going."""
    runs = list(runs)
    if any(r.books and r.display_status == "running" for r in runs):
        return Promotion(False, "a booking run is going; it rewrites the next portfolio when it finishes")
    newest = newest_booking_run(runs)
    if newest is None:
        return Promotion(False, "no completed booking run yet")
    if not next_path.is_file():
        return Promotion(False, f"{next_path} does not exist")
    if "portfolio_after.csv" in newest.files and _portfolio(newest.path / "portfolio_after.csv") != _portfolio(
        next_path
    ):
        return Promotion(False, f"{next_path.name} is not what the newest completed booking run ({newest.id}) wrote")
    if current_path.is_file() and current_path.stat().st_mtime >= next_path.stat().st_mtime:
        return Promotion(False, f"{current_path.name} is newer than {next_path.name}: nothing new to promote")
    return Promotion(True, f"copies {next_path} over {current_path}")


def promote(current_path: Path, next_path: Path) -> None:
    """Copy next over current: the operator's act between step 12 and the next run's step 2, the same bytes."""
    current_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(next_path, current_path)


def record_cashflow(path: Path, day: date, amount: str, note: str) -> float:
    """Append one deposit (+) or withdrawal (-) row through the ledger module; the amount as typed, validated here."""
    try:
        value = float(amount.replace(",", "").strip())
    except ValueError:
        raise ValueError(f"amount {amount!r} is not a number") from None
    if not math.isfinite(value) or value == 0:
        raise ValueError("amount must be a non-zero number: positive for a deposit, negative for a withdrawal")
    if not note.strip():
        raise ValueError("a note is required; it is the row's only explanation")
    append_cashflow(str(path), day, value, note.strip())
    return value
