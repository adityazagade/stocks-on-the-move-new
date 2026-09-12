"""Tests for the run directory: naming, symlink, tables, run.json, redaction, failure handling (ADR-006)."""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stocks_on_the_move import artifacts as art
from stocks_on_the_move.artifacts import NoArtifacts, RunArtifacts

IST = ZoneInfo("Asia/Kolkata")
STARTED = datetime(2026, 9, 16, 10, 0, 0, tzinfo=IST)


def create(tmp_path, settings=None, mode="paper", started=STARTED) -> RunArtifacts:
    return RunArtifacts.create(tmp_path / "runs", started=started, mode=mode, settings=settings)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


# ── directory and run.json ───────────────────────────────────────────────


def test_create_names_the_directory_and_points_latest_at_it(tmp_path, settings):
    run = create(tmp_path, settings)

    assert run.path == tmp_path / "runs" / "2026-09-16" / "100000-paper"
    latest = tmp_path / "runs" / "latest"
    assert latest.is_symlink()
    assert os.readlink(latest) == "2026-09-16/100000-paper"  # relative, so the tree can move
    assert (latest / "run.json").exists()

    meta = read_json(run.path / "run.json")
    assert meta["started"] == "2026-09-16T10:00:00+05:30"
    assert (meta["status"], meta["finished"], meta["mode"]) == ("running", None, "paper")
    assert meta["version"] == art.package_version()


def test_same_second_reruns_get_a_suffix_and_latest_follows(tmp_path, settings):
    first = create(tmp_path, settings)
    second = create(tmp_path, settings)
    third = create(tmp_path, settings)
    assert (first.path.name, second.path.name, third.path.name) == ("100000-paper", "100000-paper-2", "100000-paper-3")
    assert os.readlink(tmp_path / "runs" / "latest") == "2026-09-16/100000-paper-3"


def test_settings_snapshot_drops_credentials_and_uses_env_names(settings):
    snap = art.settings_snapshot(settings)
    assert "KITE_API_KEY" not in snap and "KITE_API_SECRET" not in snap
    assert not any("KEY" in k or "SECRET" in k or "TOKEN" in k for k in snap)
    assert snap["TRADING_WEEKDAY"] == 2
    assert snap["ALLOW_KITE_EXECUTION"] is False
    assert isinstance(snap["CACHE_DIR"], str)
    assert "test-secret" not in json.dumps(snap) and "test-key" not in json.dumps(snap)


def test_record_merges_into_run_json_and_finish_stamps_the_outcome(tmp_path, settings):
    run = create(tmp_path, settings)
    run.record(universe_size=500, regime={"bull": True})
    run.record(ranked_count=120)
    meta = read_json(run.path / "run.json")
    assert (meta["universe_size"], meta["regime"], meta["ranked_count"]) == (500, {"bull": True}, 120)
    assert meta["settings"]["TRADING_WEEKDAY"] == 2

    run.finish("aborted:empty_universe")
    meta = read_json(run.path / "run.json")
    assert meta["status"] == "aborted:empty_universe"
    assert meta["finished"] is not None


# ── tables ───────────────────────────────────────────────────────────────


def test_write_table_formats_cells_and_writes_a_header_when_empty(tmp_path):
    run = create(tmp_path)
    run.write_table(
        "ranking",
        ["rank", "symbol", "score", "held", "note"],
        [
            {"rank": 1, "symbol": "TCS", "score": 0.05123456789, "held": True, "extra": "dropped"},
            {"rank": 2, "symbol": "INFY", "score": float("nan"), "held": False, "note": None},
        ],
    )
    assert (run.path / "ranking.csv").read_text().splitlines() == [
        "rank,symbol,score,held,note",
        "1,TCS,0.051235,true,",
        "2,INFY,,false,",
    ]
    run.write_table("sizing", ["symbol", "qty"], [])
    assert (run.path / "sizing.csv").read_text() == "symbol,qty\n"


def test_write_rows_keeps_the_portfolio_format_without_a_header(tmp_path):
    run = create(tmp_path)
    run.write_rows("portfolio_before", sorted({"TCS": 5, "INFY": 10}.items()))
    assert (run.path / "portfolio_before.csv").read_text().splitlines() == ["INFY,10", "TCS,5"]


# ── log ──────────────────────────────────────────────────────────────────


def test_attach_log_copies_lines_until_finish(tmp_path):
    run = create(tmp_path)
    target = logging.getLogger("sotm.test.artifacts")
    target.setLevel(logging.INFO)
    run.attach_log(target)
    target.info("ranked %d symbols", 42)
    run.finish("completed")
    target.info("after finish")

    text = (run.path / "run.log").read_text()
    assert "INFO     ranked 42 symbols" in text
    assert "after finish" not in text
    assert target.handlers == []


# ── failure handling ─────────────────────────────────────────────────────


def test_write_failures_warn_and_never_raise(tmp_path, caplog):
    run = create(tmp_path)
    (run.path / "run.json").chmod(stat.S_IRUSR)  # run.json read-only: record() and finish() fail
    run.path.chmod(stat.S_IRUSR | stat.S_IXUSR)  # directory read-only: new files fail
    try:
        with caplog.at_level(logging.WARNING, logger=art.logger.name):
            run.write_table("exits", ["symbol"], [{"symbol": "TCS"}])
            run.write_rows("portfolio_after", [("TCS", 1)])
            run.record(status_note="still fine")
            run.finish("completed")
    finally:
        run.path.chmod(stat.S_IRWXU)
        (run.path / "run.json").chmod(stat.S_IRUSR | stat.S_IWUSR)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 4
    assert all("Could not write artifact" in r.getMessage() for r in warnings)
    assert not (run.path / "exits.csv").exists()


def test_a_real_latest_directory_is_left_alone_with_a_warning(tmp_path, caplog):
    (tmp_path / "runs" / "latest").mkdir(parents=True)
    (tmp_path / "runs" / "latest" / "keep.txt").write_text("mine")
    with caplog.at_level(logging.WARNING, logger=art.logger.name):
        run = create(tmp_path)
    assert run.path.exists()
    assert (tmp_path / "runs" / "latest" / "keep.txt").read_text() == "mine"
    assert "not a symlink" in caplog.text


def test_no_artifacts_is_a_silent_no_op(tmp_path):
    n = NoArtifacts()
    n.record(a=1)
    n.write_table("x", ["a"], [{"a": 1}])
    n.write_rows("y", [("a", 1)])
    n.finish("completed")
    assert n.path is None
    assert list(tmp_path.iterdir()) == []


def test_git_commit_and_version_are_optional_strings():
    assert art.package_version() in (None, art.version("stocks-on-the-move"))
    commit = art.git_commit()
    assert commit is None or (isinstance(commit, str) and 7 <= len(commit) <= 40)


@pytest.mark.parametrize(
    ("value", "cell"),
    [(None, ""), (1.5, "1.500000"), (float("nan"), ""), (True, "true"), (7, 7), ("x", "x")],
)
def test_cell_formatting(value, cell):
    assert art._cell(value) == cell
