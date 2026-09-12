"""The symbols the strategy may hold (ADR-020).

NSE series codes and their parsing, the instrument filter that turns a list
of base symbols into Kite instruments, and the NSE archives fetch for the
index constituents.
"""

from __future__ import annotations

import logging
import time

import pandas as pd

from stocks_on_the_move.broker import Instrument
from stocks_on_the_move.context import RunContext
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
