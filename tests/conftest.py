"""Test-session fixtures.

Importing ``stocks_on_the_move.momentum`` has no side effects since ADR-007;
configuration is an explicit ``Settings`` object. Every test runs with one
built from explicit values, so the developer's environment cannot leak in,
paper mode is always on, and nothing touches the real candle cache, the real
Kite session file, the real redirect port or a browser.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from stocks_on_the_move import momentum as m
from stocks_on_the_move.settings import Settings


@pytest.fixture
def make_settings(tmp_path) -> Callable[..., Settings]:
    """``make_settings(**overrides)`` -> a Settings that ignores the environment."""

    def make(**overrides) -> Settings:
        values = {
            "kite_api_key": "test-key",
            "kite_api_secret": "test-secret",
            "allow_kite_execution": False,
            "cache_dir": tmp_path / "candles",
            "kite_session_file": tmp_path / "kite_session.json",
            "kite_redirect_port": 0,
            "kite_open_browser": False,
        }
        values.update(overrides)
        return Settings.from_values(**values)

    return make


@pytest.fixture(autouse=True)
def settings(make_settings, monkeypatch) -> Settings:
    """Install default test settings in momentum for the duration of each test."""
    s = make_settings()
    monkeypatch.setattr(m, "SETTINGS", s)
    return s
