"""Per-instrument daily-candle cache with incremental, self-correcting fetches (ADR-008).

This is ``momentum.candles_df`` as a class that owns its broker, its cache
directory and its pause between downloads, so the strategy no longer reaches
for module state to get candles.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from stocks_on_the_move.broker import Broker

logger = logging.getLogger(__name__)

EXTRA_DAYS_PAD = 75
OVERLAP_DAYS_FOR_CHECK = 30


IST = ZoneInfo("Asia/Kolkata")


def _ist_today() -> date:
    """The date in IST, the one clock the whole program keeps (ADR-018)."""
    return datetime.now(IST).date()


def slice_by_date(df: pd.DataFrame, start_d: date, end_d: date) -> pd.DataFrame:
    if df.empty:
        return df
    d = pd.to_datetime(df["date"], utc=True).dt.tz_convert("Asia/Kolkata").dt.date
    return df[(d >= start_d) & (d <= end_d)].reset_index(drop=True)


class CandleStore:
    """``get(token, days)`` returns about ``days`` daily candles, served from a CSV per token.

    - No cache: fetch the full window and write it.
    - Cache current: no broker call.
    - Cache stale: re-fetch the last 30 cached days plus the gap and compare the
      first overlapping close; a mismatch means a split or bonus rewrote history,
      so the file is deleted and the full window fetched again. Otherwise merge.
    """

    def __init__(
        self,
        broker: Broker,
        cache_dir: Path,
        *,
        sleep_sec: float,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = _ist_today,
    ) -> None:
        self._broker = broker
        self._cache_dir = cache_dir
        self._sleep_sec = sleep_sec
        self._sleep = sleep
        self._today = today

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def _fetch(self, token: int, start_d: date, end_d: date) -> pd.DataFrame:
        df = pd.DataFrame(self._broker.historical_data(token, start_d, end_d, "day"))
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df

    def get(self, token: int, days: int) -> pd.DataFrame:
        end_d = self._today()
        extra = max(days // 2, EXTRA_DAYS_PAD)
        start_d = end_d - timedelta(days=days + extra)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self._cache_dir / f"{token}.csv"

        # --- Path 1: No cache exists ---
        if not cache_file.exists():
            logger.info("No cache for token %d. Fetching full history.", token)
            df = self._fetch(token, start_d, end_d)
            if not df.empty:
                df.to_csv(cache_file, index=False)
            self._sleep(self._sleep_sec)
            return df

        # --- Path 2: Cache exists, load and process ---
        try:
            df_cached = pd.read_csv(cache_file, parse_dates=["date"])
        except pd.errors.EmptyDataError:
            logger.warning("Cache file for token %d is empty. Deleting and re-fetching.", token)
            cache_file.unlink()
            return self.get(token, days)  # Recurse once to handle the re-fetch

        last_cached_date = df_cached["date"].max().date()

        # If cache is already up-to-date, no API call is needed.
        if last_cached_date >= end_d:
            return slice_by_date(df_cached, start_d, end_d)

        # --- Path 3: Validate and update an existing, out-of-date cache ---
        validation_start_d = last_cached_date - timedelta(days=OVERLAP_DAYS_FOR_CHECK)
        df_recent = self._fetch(token, validation_start_d, end_d)

        if df_recent.empty:
            # No new data from the API, return what we have in the cache.
            return slice_by_date(df_cached, start_d, end_d)

        # Merge to find the overlapping data for validation.
        merged = (
            pd.merge(df_cached, df_recent, on="date", suffixes=("_cached", "_fresh"), how="inner")
            .sort_values(by="date")
            .reset_index(drop=True)
        )

        # Validate the first day of the overlap. A mismatch implies a corporate action.
        if not merged.empty:
            first_day_cached = merged.at[0, "close_cached"]
            first_day_fresh = merged.at[0, "close_fresh"]

            if not np.isclose(first_day_cached, first_day_fresh):
                mismatch_date = merged.at[0, "date"].date()
                logger.warning(
                    "Mismatch on %s for token %d (Cached: %.2f vs Fresh: %.2f). "
                    "Invalidating cache due to likely corporate action.",
                    mismatch_date,
                    token,
                    first_day_cached,
                    first_day_fresh,
                )
                cache_file.unlink()
                return self.get(token, days)  # Recurse once to re-fetch full history.

        # --- Path 4: No mismatch found, perform a standard incremental update ---
        df_updated = (
            pd.concat([df_cached, df_recent], ignore_index=True)
            .drop_duplicates(subset="date", keep="last")
            .sort_values(by="date")
            .reset_index(drop=True)
        )
        df_updated.to_csv(cache_file, index=False)
        self._sleep(self._sleep_sec)

        return slice_by_date(df_updated, start_d, end_d)
