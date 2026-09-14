"""Tests for the universe (ADR-020) and the price bands (ADR-034): static sources, NSE fetches, last-good copies."""

from __future__ import annotations

import logging
import math
import urllib.error
from datetime import date
from email.message import Message

from stocks_on_the_move.settings import Settings
from stocks_on_the_move.universe import (
    BAND_STALE_DAYS,
    NO_BANDS,
    STALE_DAYS,
    NseArchives,
    NsePriceBands,
    PriceBands,
    StaticUniverse,
    fetch_price_bands,
    parse_bands,
)

TODAY = date(2026, 9, 16)
LOG = "stocks_on_the_move.universe"


def source(settings: Settings, fetched: set[str], *, today: date = TODAY) -> NseArchives:
    return NseArchives(settings, fetch=lambda _: set(fetched), today=lambda: today)


def test_static_universe_is_a_fixed_upper_cased_set():
    assert StaticUniverse(["tcs", "INFY"]).symbols() == {"TCS", "INFY"}
    assert StaticUniverse(()).symbols() == set()


def test_a_successful_fetch_is_returned_and_saved_with_its_date(settings):
    nse = source(settings, {"TCS", "INFY"})
    assert nse.symbols() == {"TCS", "INFY"}
    assert nse.copy_path == settings.cache_dir / "universe-nifty500.txt"
    assert nse.copy_path.read_text() == "# saved 2026-09-16\nINFY\nTCS\n"


def test_the_copy_is_named_for_the_list_it_holds(make_settings):
    assert NseArchives(make_settings(use_full_nifty_universe=True)).copy_path.name == "universe-all-nse.txt"


def test_an_empty_fetch_falls_back_to_the_copy_with_its_age(settings, caplog):
    source(settings, {"TCS", "INFY"}, today=date(2026, 9, 13)).symbols()  # saved three days before

    with caplog.at_level(logging.WARNING, logger=LOG):
        got = source(settings, set()).symbols()

    assert got == {"TCS", "INFY"}
    assert "using the 2 symbols saved on 2026-09-13 (3 days old)" in caplog.text
    assert "more than" not in caplog.text


def test_a_stale_copy_earns_a_second_warning(settings, caplog):
    source(settings, {"TCS"}, today=date(2026, 7, 1)).symbols()

    with caplog.at_level(logging.WARNING, logger=LOG):
        got = source(settings, set()).symbols()

    assert got == {"TCS"}
    assert f"more than {STALE_DAYS} days old" in caplog.text


def test_no_fetch_and_no_copy_is_an_empty_universe(settings, caplog):
    with caplog.at_level(logging.ERROR, logger=LOG):
        assert source(settings, set()).symbols() == set()
    assert "no saved copy" in caplog.text


def test_a_copy_without_a_date_line_is_ignored(settings, caplog):
    nse = source(settings, set())
    nse.copy_path.parent.mkdir(parents=True)
    nse.copy_path.write_text("TCS\nINFY\n")
    with caplog.at_level(logging.WARNING, logger=LOG):
        assert nse.symbols() == set()
    assert "no date line" in caplog.text


def test_an_unwritable_cache_directory_is_a_warning_not_a_failure(make_settings, tmp_path, caplog):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("")
    nse = source(make_settings(cache_dir=blocker / "cache"), {"TCS"})
    with caplog.at_level(logging.WARNING, logger=LOG):
        assert nse.symbols() == {"TCS"}
    assert "Could not save the universe copy" in caplog.text


# ── the price bands (ADR-034) ────────────────────────────────────────────
SEC_LIST = """Symbol,Series,Security Name,Band,Remarks
21STCENMGM,EQ,21ST CENTURY MANAGEMENT SERVICES LIMITED,2,"-"
VIJIFIN,BE,VIJI FINANCE LIMITED,2,"-"
KABRAEXTRU,EQ,KABRA EXTRUSION TECHNIK LIMITED,10,"-"
ATHERENERG,EQ,ATHER ENERGY LIMITED,No Band,"-"
ELECTCAST,EQ,ELECTROSTEEL CASTINGS LIMITED,20,"-"
ELECTCAST,W1,ELECTROSTEEL CASTINGS LIMITED,20,"-"
JUNK,EQ,A ROW WITHOUT A BAND,,"-"
"""
FRIDAY = date(2026, 9, 11)
MONDAY = date(2026, 9, 14)


def test_parse_bands_reads_the_band_and_treats_no_band_as_unbounded():
    bands = parse_bands(SEC_LIST)
    assert bands[("VIJIFIN", "BE")] == 2.0 and bands[("KABRAEXTRU", "EQ")] == 10.0
    assert bands[("ATHERENERG", "EQ")] == math.inf
    assert ("JUNK", "EQ") not in bands  # no readable band: skipped, never zero
    assert bands[("ELECTCAST", "EQ")] == bands[("ELECTCAST", "W1")] == 20.0


def test_band_of_matches_the_kite_symbol_on_base_and_series_then_on_the_symbol_alone():
    bands = PriceBands(parse_bands(SEC_LIST), FRIDAY, "static")
    assert bands.band_of("VIJIFIN-BE") == 2.0 and bands.band_of("KABRAEXTRU") == 10.0
    assert bands.band_of("ATHERENERG") == math.inf
    assert bands.band_of("VIJIFIN") == 2.0  # Kite still says EQ around a transfer: the one row for the symbol decides
    assert bands.band_of("ELECTCAST-BE") is None  # two rows for the symbol and neither is BE: unknown
    assert bands.band_of("GHOST") is None and NO_BANDS.band_of("VIJIFIN-BE") is None
    assert not bands.fallback and PriceBands({}, None, "fallback").fallback


def fake_archive(published: dict[str, str]):
    """A ``get`` for ``fetch_price_bands``: the text for a DDMMYYYY the archive has, a 404 for one it does not."""

    def get(url: str) -> str:
        stamp = url.rsplit("_", 1)[1].removesuffix(".csv")
        if stamp in published:
            return published[stamp]
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)

    return get


def test_the_fetch_walks_back_from_the_run_date_to_the_last_published_file():
    got = fetch_price_bands(MONDAY, get=fake_archive({"11092026": SEC_LIST}))  # Monday's, Sunday's, Saturday's 404
    assert got is not None
    as_of, bands = got
    assert as_of == FRIDAY and bands[("VIJIFIN", "BE")] == 2.0


def test_the_fetch_gives_up_past_the_walk_back_and_on_a_transport_error():
    assert fetch_price_bands(MONDAY, get=fake_archive({"01092026": SEC_LIST})) is None  # thirteen days: beyond the walk
    calls: list[str] = []

    def broken(url: str) -> str:
        calls.append(url)
        raise OSError("connection reset")

    assert fetch_price_bands(MONDAY, get=broken) is None
    assert len(calls) == 1  # the network is the problem, not the date: no point asking about the day before


def band_source(settings: Settings, fetched, *, today: date = MONDAY) -> NsePriceBands:
    return NsePriceBands(settings, fetch=lambda _: fetched, today=lambda: today)


def test_a_fresh_list_is_returned_and_saved_with_the_files_date(settings):
    src = band_source(settings, (FRIDAY, parse_bands(SEC_LIST)))
    bands = src.bands()
    assert (bands.source, bands.as_of, bands.band_of("VIJIFIN-BE")) == ("fresh", FRIDAY, 2.0)
    assert src.copy_path == settings.cache_dir / "price-bands.csv"
    text = src.copy_path.read_text()
    assert text.startswith("# as of 2026-09-11\nsymbol,series,band\n")
    assert "ATHERENERG,EQ,inf\n" in text  # No Band round-trips


def test_a_failed_band_fetch_falls_back_to_the_copy_with_its_age(settings, caplog):
    band_source(settings, (FRIDAY, parse_bands(SEC_LIST))).bands()
    with caplog.at_level(logging.WARNING, logger=LOG):
        bands = band_source(settings, None, today=date(2026, 9, 16)).bands()
    assert (bands.source, bands.as_of, bands.band_of("VIJIFIN-BE")) == ("copy", FRIDAY, 2.0)
    assert bands.band_of("ATHERENERG") == math.inf
    assert "saved as of 2026-09-11 (5 days old)" in caplog.text
    assert "more than" not in caplog.text


def test_a_stale_band_copy_earns_the_second_warning_past_ten_days(settings, caplog):
    band_source(settings, (FRIDAY, parse_bands(SEC_LIST))).bands()
    with caplog.at_level(logging.WARNING, logger=LOG):
        bands = band_source(settings, None, today=date(2026, 9, 22)).bands()  # eleven days
    assert bands.source == "copy"
    assert f"more than {BAND_STALE_DAYS} days old" in caplog.text
    assert BAND_STALE_DAYS == 10  # one weekly surveillance review plus a run's grace (ADR-034)


def test_no_band_fetch_and_no_copy_is_the_fallback_rung_not_an_abort(settings, caplog):
    with caplog.at_level(logging.ERROR, logger=LOG):
        bands = band_source(settings, None).bands()
    assert bands.fallback and bands.as_of is None and bands.band_of("VIJIFIN-BE") is None
    assert "no saved copy" in caplog.text and "series stands in" in caplog.text


def test_a_band_copy_without_a_date_line_is_ignored(settings, caplog):
    src = band_source(settings, None)
    src.copy_path.parent.mkdir(parents=True)
    src.copy_path.write_text("symbol,series,band\nVIJIFIN,BE,2.0\n")
    with caplog.at_level(logging.WARNING, logger=LOG):
        assert src.bands().fallback
    assert "no date line" in caplog.text
