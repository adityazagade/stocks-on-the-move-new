"""The symbols the strategy may hold (ADR-020).

NSE series codes and their parsing, the instrument filter that turns a list
of base symbols into Kite instruments, and the ``UniverseSource`` protocol
with its two implementations: a fixed set, and the NSE archives fetch with a
last-good copy under the cache directory.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Protocol

import pandas as pd

from stocks_on_the_move.broker import Instrument
from stocks_on_the_move.context import RunContext, ist_now
from stocks_on_the_move.settings import Settings

logger = logging.getLogger(__name__)


# recognised NSE equity series codes
SERIES_CODES: set[str] = {
    "EQ",
    "BE",
    "BL",
    "BZ",
    "BT",
    "IL",
    "IQ",
    "SM",
    "ST",
    "GC",
    "GS",
    # debt / partly-paid / rights etc.
    "PP",
    "RE",
    "WD",
    "N1",
    "N2",
    "N3",
}


NO_MARKET_SERIES = {"BE", "BZ", "BT", "IL", "IQ", "SM", "ST"}


def series_of(ts: str) -> str:
    """Extract recognised 2-char NSE series code from a tradingsymbol."""
    ts = ts.upper()
    if "-" in ts:
        _, maybe_series = ts.rsplit("-", 1)
        if maybe_series in SERIES_CODES:
            return maybe_series
    return "EQ"


def base_symbol(ts: str) -> str:
    """Remove trailing '-XX' only when XX is a known NSE series code."""
    ts = ts.upper()
    if "-" in ts:
        root, maybe_series = ts.rsplit("-", 1)
        if maybe_series in SERIES_CODES:
            return root
    return ts


def get_universe(ctx: RunContext, symbols: set[str]) -> list[Instrument]:
    """Universe = NSE equity instruments whose base symbols are in *symbols*."""
    return [
        i
        for i in ctx.broker.instruments("NSE")
        if i.instrument_type == "EQ" and i.segment == "NSE" and base_symbol(i.tradingsymbol) in symbols
    ]


def fetch_index_constituents(index: str, retries: int = 3) -> list[str]:
    """Fetch current constituents for a given NSE index from NSE archives.

    Returns upper-cased list of symbols; empty list if all retries fail.
    """
    url = f"https://archives.nseindia.com/content/indices/ind_{index.lower().replace(' ', '')}list.csv"
    for attempt in range(1, retries + 1):
        try:
            df = pd.read_csv(url)
            return df["Symbol"].str.upper().tolist()
        except Exception as exc:
            logger.warning("%s fetch failed (%s) – attempt %d/%d", index, exc, attempt, retries)
            time.sleep(1)
    logger.error("Giving up – empty universe filter")
    return []


def fetch_nifty_constituents(retries: int = 3) -> list[str]:
    """Fetch full NIFTY constituents list from NSE archives."""
    # https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
    url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    for _attempt in range(1, retries + 1):
        try:
            df = pd.read_csv(url)
            return df["SYMBOL"].str.upper().tolist()
        except Exception as exc:
            logger.error("NIFTY full constituents fetch failed (%s) – empty universe", exc)
            time.sleep(1)
    logger.error("Giving up – empty universe")
    return []


def nse_universe_symbols(settings: Settings) -> set[str]:
    """Base symbols the strategy may hold: the NIFTY 500 constituents, or every NSE equity."""
    if settings.use_full_nifty_universe:
        symbols = set(fetch_nifty_constituents())
        logger.info("Using full NIFTY universe: %d symbols", len(symbols))
        return symbols
    return set(fetch_index_constituents("NIFTY 500"))


# ── where the list comes from ────────────────────────────────────────────
class UniverseSource(Protocol):
    def symbols(self) -> set[str]:
        """The base symbols the strategy may hold; empty when none could be found."""
        ...


class StaticUniverse:
    """A fixed set of base symbols: the tests' universe, or a hand-picked list."""

    def __init__(self, members: Iterable[str]) -> None:
        self._members = frozenset(m.upper() for m in members)

    def symbols(self) -> set[str]:
        return set(self._members)


STALE_DAYS = 30
_COPY_HEADER = "# saved "


class NseArchives:
    """The NSE archives list, with a last-good copy under the cache directory (ADR-020).

    A fetch that succeeds writes the list to ``<CACHE_DIR>/universe-<name>.txt``
    with the date on its first line. A fetch that comes back empty falls back to
    that copy with a WARNING naming its age, and a second WARNING past
    ``STALE_DAYS``. With no copy either, the result is empty and the run aborts
    as it always has.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        fetch: Callable[[Settings], set[str]] = nse_universe_symbols,
        today: Callable[[], date] = lambda: ist_now().date(),
    ) -> None:
        self._settings = settings
        self._fetch = fetch
        self._today = today

    @property
    def copy_path(self) -> Path:
        name = "all-nse" if self._settings.use_full_nifty_universe else "nifty500"
        return Path(self._settings.cache_dir) / f"universe-{name}.txt"

    def symbols(self) -> set[str]:
        fresh = self._fetch(self._settings)
        if fresh:
            self._save(fresh)
            return fresh
        saved = self._load()
        if saved is None:
            logger.error("NSE archives unreachable and no saved copy at %s", self.copy_path)
            return set()
        saved_on, members = saved
        age = (self._today() - saved_on).days
        logger.warning(
            "NSE archives unreachable; using the %d symbols saved on %s (%d days old)", len(members), saved_on, age
        )
        if age > STALE_DAYS:
            logger.warning("That copy is more than %d days old; the index may have changed since", STALE_DAYS)
        return members

    def _save(self, members: set[str]) -> None:
        try:
            self.copy_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"{_COPY_HEADER}{self._today().isoformat()}", *sorted(members)]
            self.copy_path.write_text("\n".join(lines) + "\n")
        except OSError as exc:
            logger.warning("Could not save the universe copy to %s (%s)", self.copy_path, exc)

    def _load(self) -> tuple[date, set[str]] | None:
        try:
            text = self.copy_path.read_text()
        except OSError:
            return None
        head, _, body = text.partition("\n")
        if not head.startswith(_COPY_HEADER):
            logger.warning("Ignoring the universe copy at %s: no date line", self.copy_path)
            return None
        try:
            saved_on = date.fromisoformat(head[len(_COPY_HEADER) :].strip())
        except ValueError:
            logger.warning("Ignoring the universe copy at %s: unreadable date line", self.copy_path)
            return None
        return saved_on, {line.strip().upper() for line in body.splitlines() if line.strip()}
