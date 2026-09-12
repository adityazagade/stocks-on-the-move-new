"""Tests for where the universe comes from (ADR-020): the static source and the NSE archives with a last-good copy."""

from __future__ import annotations

import logging
from datetime import date

from stocks_on_the_move.settings import Settings
from stocks_on_the_move.universe import STALE_DAYS, NseArchives, StaticUniverse

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
