"""Starting the command as a child process (ADR-031): one child at a time, the mode as the only overlay.

The console never sets ``ALLOW_KITE_EXECUTION``, ``KILL_SWITCH``, ``FORCE_RESIZE``,
``ENV_CASHFLOW`` or ``TRADING_WEEKDAY``. The child inherits the console's
environment and gets ``PLAN_ONLY`` alone, so a booking run is exactly what the
environment says it is, and the Kite login happens in the child as it does
from the shell (ADR-005).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from stocks_on_the_move.context import ist_now

OVERLAY: dict[str, dict[str, str]] = {"plan": {"PLAN_ONLY": "1"}, "book": {"PLAN_ONLY": "0"}}
DEFAULT_COMMAND: tuple[str, ...] = (sys.executable, "-m", "stocks_on_the_move")
KEEP_LINES = 400  # of the child's output, the last this many are shown


class LauncherBusy(RuntimeError):
    """A child is still running; the console starts one at a time."""


@dataclass
class Child:
    mode: str  # plan | book
    started: datetime
    process: subprocess.Popen[str] = field(repr=False)
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=KEEP_LINES))
    pump: threading.Thread | None = field(default=None, repr=False)

    @property
    def returncode(self) -> int | None:
        return self.process.poll()

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def wait(self, timeout: float | None = None) -> int:
        """Block until the child exits and its output is fully copied; the exit code."""
        code = self.process.wait(timeout)
        if self.pump is not None:
            self.pump.join(timeout)
        return code


class Launcher:
    """Starts the command; ``current`` is the child started last, running or finished."""

    def __init__(
        self,
        command: Sequence[str] = DEFAULT_COMMAND,
        *,
        env: Mapping[str, str] | None = None,
        now: Callable[[], datetime] = ist_now,
    ) -> None:
        self._command = list(command)
        self._env = dict(os.environ if env is None else env)
        self._now = now
        self._lock = threading.Lock()
        self.current: Child | None = None

    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.running

    def start(self, mode: str) -> Child:
        overlay = OVERLAY.get(mode)
        if overlay is None:
            raise ValueError(f"mode must be one of {sorted(OVERLAY)}, not {mode!r}")
        with self._lock:
            if self.busy:
                raise LauncherBusy("a run is already going")
            process = subprocess.Popen(
                self._command,
                env={**self._env, **overlay},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            child = Child(mode=mode, started=self._now(), process=process)
            child.pump = threading.Thread(target=_pump, args=(child,), daemon=True, name="console-child-output")
            child.pump.start()
            self.current = child
            return child


def _pump(child: Child) -> None:
    """Copy the child's output into its ring buffer until the pipe closes."""
    assert child.process.stdout is not None
    with child.process.stdout as out:
        for line in out:
            child.lines.append(line.rstrip("\n"))
    child.process.wait()
