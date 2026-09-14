"""The symbols the strategy may hold, and what the exchange lets it open (ADR-020, ADR-034).

NSE series codes and their parsing, the instrument filter that turns a list
of base symbols into Kite instruments, the ``UniverseSource`` protocol with
its two implementations — a fixed set, and the NSE archives fetch with a
last-good copy under the cache directory — and, on the same pattern, NSE's
daily price-band list, which says which names may be opened (ADR-034).
"""

from __future__ import annotations

import csv
import io
import logging
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Protocol

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

# Trade-for-trade because the issuer failed its listing obligations, not because the exchange put it under
# surveillance: BZ on the main board, SZ on the SME platform (ADR-034). Never opened; BE is not in this set.
NON_COMPLIANT_SERIES = {"BZ", "SZ"}


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

    def saved_symbols(self) -> set[str] | None:
        """The last-good copy's members without a fetch, for tools that must not go online (ADR-023)."""
        saved = self._load()
        return None if saved is None else saved[1]

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


# ── the price bands: what the exchange lets us open (ADR-034) ────────────
BandSource = Literal["fresh", "copy", "fallback", "static"]
BandTable = dict[tuple[str, str], float]


@dataclass(frozen=True)
class PriceBands:
    """The operative daily price band per ``(symbol, series)``, in percent, from NSE's ``sec_list`` (ADR-034).

    ``math.inf`` is "No Band", the F&O names. ``as_of`` is the date of the file
    the bands came from and ``source`` the rung that produced them: ``fresh``
    from the archive, ``copy`` from the last-good file under the cache
    directory, ``fallback`` when neither could be had and the rules stand in
    with the series, or ``static`` for a lookup a test or a backtest supplied.
    """

    bands: Mapping[tuple[str, str], float]
    as_of: date | None
    source: BandSource

    @property
    def fallback(self) -> bool:
        return self.source == "fallback"

    def band_of(self, tradingsymbol: str) -> float | None:
        """The band for a Kite tradingsymbol, matched on base symbol and series; ``None`` when unknown.

        When the exact pair is missing — Kite and the list can disagree on the
        series for a day around a transfer — the symbol alone decides if it
        names exactly one row.
        """
        symbol = base_symbol(tradingsymbol)
        exact = self.bands.get((symbol, series_of(tradingsymbol)))
        if exact is not None:
            return exact
        matches = [band for (sym, _), band in self.bands.items() if sym == symbol]
        return matches[0] if len(matches) == 1 else None


NO_BANDS = PriceBands({}, None, "static")  # permissive: nothing is banded and nothing falls back to the series

BAND_URL = "https://nsearchives.nseindia.com/content/equities/sec_list_{ddmmyyyy}.csv"
BAND_WALKBACK_DAYS = 7  # the run date's file appears after the day: a Monday reaches Friday, a long weekend further
BAND_STALE_DAYS = 10  # one weekly surveillance review plus a run's grace: the file is daily and the run weekly
_FETCH_TIMEOUT = 15.0
# NSE's archive holds a connection from Python's default agent open indefinitely; a browser's returns at once.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
_BAND_HEADER = "# as of "


def fetch_text(url: str) -> str:
    """The body at ``url`` as text; ``urllib.error.HTTPError`` on a 404, ``URLError`` or ``OSError`` on transport."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response:
        return response.read().decode("utf-8-sig")


def parse_bands(text: str) -> BandTable:
    """``(symbol, series) -> band`` from the list's CSV; "No Band" is ``inf``, an unreadable band skips its row."""
    out: BandTable = {}
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
        symbol, series, band = row.get("Symbol", "").upper(), row.get("Series", "").upper(), row.get("Band", "")
        if not symbol or not series:
            continue
        if band.lower().startswith("no band"):
            out[(symbol, series)] = math.inf
            continue
        try:
            out[(symbol, series)] = float(band)
        except ValueError:
            logger.debug("Price-band row for %s-%s has no readable band (%r); skipped", symbol, series, band)
    return out


def fetch_price_bands(today: date, *, get: Callable[[str], str] = fetch_text) -> tuple[date, BandTable] | None:
    """The most recent ``sec_list`` within ``BAND_WALKBACK_DAYS`` of ``today``, with its date; ``None`` when none came.

    A 404 is a weekend, a holiday or a file not yet published, and the walk
    goes on to the day before. Any other failure is the network, not the
    date, so the walk stops there and the caller falls back to its copy.
    """
    for back in range(BAND_WALKBACK_DAYS + 1):
        day = today - timedelta(days=back)
        url = BAND_URL.format(ddmmyyyy=day.strftime("%d%m%Y"))
        try:
            text = get(url)
        except urllib.error.HTTPError as exc:
            logger.debug("No price-band file for %s (HTTP %s)", day, exc.code)
            continue
        except Exception as exc:
            logger.warning("Price-band fetch for %s failed (%s: %s)", day, type(exc).__name__, exc)
            return None
        bands = parse_bands(text)
        if bands:
            return day, bands
        logger.warning("Price-band file for %s held no readable rows", day)
    return None


class NsePriceBands:
    """NSE's daily price-band list, with a last-good copy under the cache directory (ADR-034).

    A fetch that succeeds writes the list to ``<CACHE_DIR>/price-bands.csv``
    with the file's own date on its first line. A fetch that fails uses that
    copy with a WARNING naming its age, and a second WARNING past
    ``BAND_STALE_DAYS``. With no copy either, the result is the ``fallback``
    rung: the rules stand in with the series and the run goes on.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        fetch: Callable[[date], tuple[date, BandTable] | None] = fetch_price_bands,
        today: Callable[[], date] = lambda: ist_now().date(),
    ) -> None:
        self._settings = settings
        self._fetch = fetch
        self._today = today

    @property
    def copy_path(self) -> Path:
        return Path(self._settings.cache_dir) / "price-bands.csv"

    def bands(self) -> PriceBands:
        today = self._today()
        fresh = self._fetch(today)
        if fresh is not None:
            as_of, bands = fresh
            self._save(as_of, bands)
            logger.info("Price bands: %d names as of %s", len(bands), as_of)
            return PriceBands(bands, as_of, "fresh")
        saved = self._load()
        if saved is None:
            logger.error(
                "NSE price bands unreachable and no saved copy at %s; the series stands in for the band this run",
                self.copy_path,
            )
            return PriceBands({}, None, "fallback")
        as_of, bands = saved
        age = (today - as_of).days
        logger.warning(
            "NSE price bands unreachable; using the %d names saved as of %s (%d days old)", len(bands), as_of, age
        )
        if age > BAND_STALE_DAYS:
            logger.warning("That copy is more than %d days old; a name banded since is not in it", BAND_STALE_DAYS)
        return PriceBands(bands, as_of, "copy")

    def _save(self, as_of: date, bands: Mapping[tuple[str, str], float]) -> None:
        try:
            self.copy_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"{_BAND_HEADER}{as_of.isoformat()}", "symbol,series,band"]
            lines += [f"{symbol},{series},{band}" for (symbol, series), band in sorted(bands.items())]
            self.copy_path.write_text("\n".join(lines) + "\n")
        except OSError as exc:
            logger.warning("Could not save the price-band copy to %s (%s)", self.copy_path, exc)

    def _load(self) -> tuple[date, BandTable] | None:
        try:
            text = self.copy_path.read_text()
        except OSError:
            return None
        head, _, body = text.partition("\n")
        if not head.startswith(_BAND_HEADER):
            logger.warning("Ignoring the price-band copy at %s: no date line", self.copy_path)
            return None
        try:
            as_of = date.fromisoformat(head[len(_BAND_HEADER) :].strip())
        except ValueError:
            logger.warning("Ignoring the price-band copy at %s: unreadable date line", self.copy_path)
            return None
        bands: BandTable = {}
        for row in csv.DictReader(io.StringIO(body)):
            try:
                bands[(row["symbol"], row["series"])] = float(row["band"])
            except (KeyError, TypeError, ValueError):
                continue
        return as_of, bands
