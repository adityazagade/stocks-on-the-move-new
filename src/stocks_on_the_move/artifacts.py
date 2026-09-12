"""Per-run artifacts (ADR-006): what a run decided, written as it goes, never read back.

``RunArtifacts.create`` makes ``runs/<YYYY-MM-DD>/<HHMMSS>-<mode>/``, points
``runs/latest`` at it and writes the first ``run.json``. Each pipeline step
then writes its table as soon as it completes, so a crash leaves everything
up to that step on disk. ``attach_log`` copies the run's log lines into
``run.log``; ``finish`` stamps the status.

This module is the one place in the codebase where swallowing an exception is
the intended behaviour: a failed write is logged at WARNING with its path and
the run continues, because diagnostics must never change a trading decision.
Credentials never appear here; the settings snapshot drops every key that
contains KEY, SECRET or TOKEN.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol

from stocks_on_the_move.logging_setup import PACKAGE_LOGGER, file_handler
from stocks_on_the_move.settings import Settings

logger = logging.getLogger(__name__)

_REDACTED_KEY_PARTS = ("KEY", "SECRET", "TOKEN")


class Artifacts(Protocol):
    """What the pipeline calls; ``RunArtifacts`` writes, ``NoArtifacts`` does nothing."""

    @property
    def path(self) -> Path | None: ...

    def record(self, **fields: Any) -> None: ...

    def write_table(self, name: str, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None: ...

    def write_rows(self, name: str, rows: Iterable[Sequence[Any]]) -> None: ...

    def finish(self, status: str) -> None: ...

    def attach_log(self, target: logging.Logger | None = None) -> None: ...


def settings_snapshot(settings: Settings) -> dict[str, Any]:
    """The settings as ``run.json`` records them: env-style names, credentials removed."""
    return {
        name.upper(): value
        for name, value in settings.model_dump(mode="json").items()
        if not any(part in name.upper() for part in _REDACTED_KEY_PARTS)
    }


def package_version() -> str | None:
    try:
        return version("stocks-on-the-move")
    except PackageNotFoundError:
        return None


def git_commit() -> str | None:
    """The short commit of the working directory, if it is a git checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=2, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.6f}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


class NoArtifacts:
    """The default on a ``RunContext``: nothing is written."""

    @property
    def path(self) -> Path | None:
        return None

    def record(self, **fields: Any) -> None:
        pass

    def write_table(self, name: str, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
        pass

    def write_rows(self, name: str, rows: Iterable[Sequence[Any]]) -> None:
        pass

    def finish(self, status: str) -> None:
        pass

    def attach_log(self, target: logging.Logger | None = None) -> None:
        pass


class RunArtifacts:
    """One run directory. Every method that touches the disk warns instead of raising."""

    def __init__(self, path: Path, *, meta: dict[str, Any]) -> None:
        self._path = path
        self._meta = meta
        self._handler: logging.Handler | None = None

    @classmethod
    def create(cls, runs_dir: Path, *, started: datetime, mode: str, settings: Settings | None = None) -> RunArtifacts:
        """Make ``<runs_dir>/<date>/<time>-<mode>/`` (suffix ``-2``, ``-3`` on a collision); point ``latest`` at it."""
        day_dir = runs_dir / started.strftime("%Y-%m-%d")
        base = f"{started.strftime('%H%M%S')}-{mode}"
        path = day_dir / base
        n = 1
        while path.exists():
            n += 1
            path = day_dir / f"{base}-{n}"
        path.mkdir(parents=True)

        meta: dict[str, Any] = {
            "started": started.isoformat(timespec="seconds"),
            "finished": None,
            "status": "running",
            "mode": mode,
            "version": package_version(),
            "commit": git_commit(),
        }
        if settings is not None:
            meta["settings"] = settings_snapshot(settings)
        run = cls(path, meta=meta)
        run._write_meta()
        run._point_latest(runs_dir)
        logger.info("Run artifacts → %s", path)
        return run

    @property
    def path(self) -> Path:
        return self._path

    # -- run.json -----------------------------------------------------------
    def record(self, **fields: Any) -> None:
        """Merge fields into run.json and rewrite it, so a crash leaves the latest state."""
        self._meta.update(fields)
        self._write_meta()

    def finish(self, status: str) -> None:
        """Stamp the outcome (``completed``, ``aborted:<reason>``, ``failed:<type>``) and stop copying the log."""
        self._meta["finished"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self._meta["status"] = status
        self._write_meta()
        self.detach_log()

    def _write_meta(self) -> None:
        self._guarded(self._path / "run.json", lambda p: p.write_text(json.dumps(self._meta, indent=2) + "\n"))

    # -- tables -------------------------------------------------------------
    def write_table(self, name: str, columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
        """``<name>.csv`` with a header row; header only when there are no rows."""

        def write(p: Path) -> None:
            with p.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
                w.writeheader()
                for row in rows:
                    w.writerow({c: _cell(row.get(c)) for c in columns})

        self._guarded(self._path / f"{name}.csv", write)

    def write_rows(self, name: str, rows: Iterable[Sequence[Any]]) -> None:
        """``<name>.csv`` without a header, for the SYMBOL,QUANTITY portfolio format."""

        def write(p: Path) -> None:
            with p.open("w", newline="") as f:
                csv.writer(f).writerows(rows)

        self._guarded(self._path / f"{name}.csv", write)

    # -- log ----------------------------------------------------------------
    def attach_log(self, target: logging.Logger | None = None) -> None:
        """Copy everything the package logs, at DEBUG, into run.log (ADR-015).

        The console shows what LOG_LEVEL asks for; this file gets every record, in a
        format that adds the logger name and the source line.
        """
        target = target or logging.getLogger(PACKAGE_LOGGER)
        try:
            handler = file_handler(str(self._path / "run.log"))
        except OSError as exc:
            logger.warning("Could not open %s for the run log: %s", self._path / "run.log", exc)
            return
        target.addHandler(handler)
        self._handler = handler
        self._target = target

    def detach_log(self) -> None:
        if self._handler is not None:
            self._target.removeHandler(self._handler)
            self._handler.close()
            self._handler = None

    # -- helpers ------------------------------------------------------------
    def _point_latest(self, runs_dir: Path) -> None:
        latest = runs_dir / "latest"
        target = self._path.relative_to(runs_dir)

        def relink(p: Path) -> None:
            if p.is_symlink() or p.exists():
                if p.is_dir() and not p.is_symlink():
                    raise OSError(f"{p} is a real directory, not a symlink")
                p.unlink()
            p.symlink_to(target, target_is_directory=True)

        self._guarded(latest, relink)

    def _guarded(self, path: Path, action) -> None:
        try:
            action(path)
        except Exception as exc:  # the intended swallow: artifacts must never stop a run
            logger.warning("Could not write artifact %s: %s", path, exc)
