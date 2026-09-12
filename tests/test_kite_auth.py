"""Tests for the Kite session cache, redirect listener and paste handling (ADR-005).

Nothing here talks to Kite: the client is a fake, the listener is exercised over
loopback, and "the terminal" is a pseudo-terminal from ``os.openpty``.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest
from kiteconnect.exceptions import TokenException

from stocks_on_the_move import kite_auth as ka

TOKEN = "tok123456abcdef"
REDIRECT_QUERY = f"?request_token={TOKEN}&action=login&status=success"


def ist(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ka.IST)


# ── parsing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (TOKEN, TOKEN),
        (f"  {TOKEN}\n", TOKEN),
        (f"https://example.com/cb{REDIRECT_QUERY}", TOKEN),
        (f"http://127.0.0.1:8765/?action=login&request_token={TOKEN}", TOKEN),
        (REDIRECT_QUERY.lstrip("?"), TOKEN),
        (REDIRECT_QUERY, TOKEN),
        ("", None),
        ("   \n", None),
        ("https://example.com/cb?status=error&action=login", None),
        ("https://example.com/cb?request_token=", None),
        ("not a token", None),
        ("short", None),
    ],
)
def test_parse_request_token(text, expected):
    assert ka.parse_request_token(text) == expected


# ── expiry ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("issued", "now", "live"),
    [
        (ist(2026, 9, 16, 6, 1), ist(2026, 9, 16, 7), True),
        (ist(2026, 9, 16, 5, 59), ist(2026, 9, 16, 7), False),
        (ist(2026, 9, 16, 23), ist(2026, 9, 17, 2), True),  # overnight, boundary not yet reached
        (ist(2026, 9, 16, 23), ist(2026, 9, 17, 6, 30), False),  # boundary passed
        (ist(2026, 9, 16, 5, 59), ist(2026, 9, 17, 2), False),  # died at 06:00 the day before
        (ist(2026, 9, 16, 10), ist(2026, 9, 17, 6), False),  # exactly at the boundary
        (ist(2026, 9, 16, 10), ist(2026, 9, 17, 5, 59), True),
    ],
)
def test_session_is_live_around_the_0600_boundary(issued, now, live):
    record = ka.SessionRecord(api_key="k", access_token="t", issued_at=issued)
    assert ka.session_is_live(record, now) is live


def test_last_expiry_boundary_works_from_other_zones():
    now_utc = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)  # 05:30 IST on the 16th
    assert ka.last_expiry_boundary(now_utc) == ist(2026, 9, 15, 6)
    with pytest.raises(ValueError, match="timezone-aware"):
        ka.last_expiry_boundary(datetime(2026, 9, 16, 7, 0))


# ── the cache file ───────────────────────────────────────────────────────


def test_save_then_load_roundtrip_with_tight_permissions(tmp_path):
    path = tmp_path / "cfg" / "kite_session.json"
    record = ka.SessionRecord("key", TOKEN, "AB1234", ist(2026, 9, 16, 9, 31), "redirect")

    ka.save_session(path, record)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert ka.load_session(path) == record
    assert TOKEN not in repr(record)


def test_save_tightens_a_loose_existing_file(tmp_path):
    path = tmp_path / "kite_session.json"
    path.write_text("{}")
    path.chmod(0o644)
    ka.save_session(path, ka.SessionRecord("key", TOKEN))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_session_missing_or_malformed(tmp_path, caplog):
    path = tmp_path / "kite_session.json"
    assert ka.load_session(path) is None

    path.write_text("not json")
    with caplog.at_level(logging.WARNING, logger=ka.logger.name):
        assert ka.load_session(path) is None
    assert "unreadable" in caplog.text

    path.write_text('{"api_key": "k"}')  # missing fields
    assert ka.load_session(path) is None


def test_load_session_assumes_ist_for_a_naive_timestamp(tmp_path):
    path = tmp_path / "kite_session.json"
    path.write_text('{"api_key": "k", "access_token": "t", "issued_at": "2026-09-16T09:31:04"}')
    record = ka.load_session(path)
    assert record is not None
    assert record.issued_at == ist(2026, 9, 16, 9, 31).replace(second=4)


def test_forget_session(tmp_path):
    path = tmp_path / "kite_session.json"
    assert ka.forget_session(path) is False
    path.write_text("{}")
    assert ka.forget_session(path) is True
    assert not path.exists()


# ── the redirect listener ────────────────────────────────────────────────


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read().decode()


def test_listener_captures_the_redirect_and_ignores_other_requests(caplog):
    listener = ka.RedirectListener(0).start()
    try:
        with caplog.at_level(logging.DEBUG, logger=ka.logger.name):
            with pytest.raises(urllib.error.HTTPError) as err:
                _get(listener.redirect_url + "favicon.ico")
            assert err.value.code == 400
            assert listener.wait(0) is None

            with pytest.raises(urllib.error.HTTPError) as err:
                _get(listener.redirect_url + f"?request_token={TOKEN}&status=error")
            assert err.value.code == 400
            assert listener.wait(0) is None

            status, body = _get(listener.redirect_url + REDIRECT_QUERY)
        assert status == 200
        assert "close this tab" in body
        assert listener.wait(2) == TOKEN
        assert caplog.text  # the listener did log something ...
        assert TOKEN not in caplog.text  # ... but never the token
    finally:
        listener.close()


def test_listener_frees_its_port_on_close():
    listener = ka.RedirectListener(0).start()
    port = listener.port
    listener.close()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))  # would raise if the listener still held it


# ── the shared wait loop ─────────────────────────────────────────────────


@contextlib.contextmanager
def typed_terminal(text: str):
    """A pseudo-terminal whose input already holds ``text``; isatty() is True on it."""
    master, slave = os.openpty()
    os.write(master, text.encode())
    stdin = os.fdopen(slave, "r")
    try:
        yield stdin
    finally:
        stdin.close()
        os.close(master)


def test_wait_reads_a_pasted_url_from_the_terminal():
    with typed_terminal(f"https://example.com/cb{REDIRECT_QUERY}\n") as stdin:
        assert ka.wait_for_request_token(None, stdin, timeout=5) == (TOKEN, "manual")


def test_wait_retries_after_junk_input(capsys):
    with typed_terminal(f"oops\n{TOKEN}\n") as stdin:
        assert ka.wait_for_request_token(None, stdin, timeout=5) == (TOKEN, "manual")
    assert "Try again" in capsys.readouterr().out


def test_wait_times_out_when_input_is_closed():
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        ka.wait_for_request_token(None, io.StringIO(""), timeout=0.3)
    assert time.monotonic() - start < 2


def test_wait_returns_the_redirect_while_the_terminal_is_silent():
    listener = ka.RedirectListener(0).start()
    try:
        threading.Timer(0.2, lambda: _get(listener.redirect_url + REDIRECT_QUERY)).start()
        with typed_terminal("") as stdin:
            assert ka.wait_for_request_token(listener, stdin, timeout=5) == (TOKEN, "redirect")
    finally:
        listener.close()


# ── authenticate() end to end, against a fake client ─────────────────────


class FakeKite:
    def __init__(self, api_key: str, *, dead: bool = False):
        self.api_key = api_key
        self.access_token: str | None = None
        self.dead = dead
        self.profile_calls = 0
        self.request_tokens: list[str] = []

    def login_url(self) -> str:
        return f"https://kite.example/connect/login?api_key={self.api_key}"

    def set_access_token(self, token):
        self.access_token = token

    def profile(self):
        self.profile_calls += 1
        if self.dead:
            raise TokenException("Incorrect `api_key` or `access_token`.")
        return {"user_id": "AB1234"}

    def generate_session(self, request_token, api_secret):
        assert api_secret == "secret"
        self.request_tokens.append(request_token)
        self.access_token = f"fresh-{len(self.request_tokens)}"
        return {"access_token": self.access_token, "user_id": "AB1234", "login_time": "2026-09-16 09:31:04"}


NOW = ist(2026, 9, 16, 10)


def _settings(tmp_path, **overrides) -> ka.AuthSettings:
    defaults = {
        "session_file": tmp_path / "kite_session.json",
        "redirect_port": 0,
        "open_browser": False,
        "login_timeout": 5.0,
    }
    return ka.AuthSettings(**{**defaults, **overrides})


def _authenticate(fake: FakeKite, settings: ka.AuthSettings, stdin) -> FakeKite:
    kite = ka.authenticate(
        "key", "secret", settings=settings, kite_factory=lambda api_key: fake, now=lambda: NOW, stdin=stdin
    )
    assert kite is fake
    return fake


def test_reuses_a_live_cached_session(tmp_path):
    settings = _settings(tmp_path)
    ka.save_session(settings.session_file, ka.SessionRecord("key", "cached", "AB1234", ist(2026, 9, 16, 9), "manual"))

    fake = _authenticate(FakeKite("key"), settings, io.StringIO())

    assert fake.access_token == "cached"
    assert fake.profile_calls == 1
    assert fake.request_tokens == []


def test_invalidated_cached_session_falls_through_to_the_paste(tmp_path):
    settings = _settings(tmp_path)
    ka.save_session(settings.session_file, ka.SessionRecord("key", "cached", "AB1234", ist(2026, 9, 16, 9), "manual"))

    with typed_terminal(f"{TOKEN}\n") as stdin:
        fake = _authenticate(FakeKite("key", dead=True), settings, stdin)

    assert fake.request_tokens == [TOKEN]
    assert fake.access_token == "fresh-1"
    saved = ka.load_session(settings.session_file)
    assert saved is not None
    assert (saved.access_token, saved.source, saved.issued_at, saved.user_id) == ("fresh-1", "manual", NOW, "AB1234")


def test_expired_cached_session_is_dropped_without_asking_kite(tmp_path):
    settings = _settings(tmp_path)
    ka.save_session(settings.session_file, ka.SessionRecord("key", "old", "AB1234", ist(2026, 9, 15, 9), "manual"))

    with typed_terminal(f"{TOKEN}\n") as stdin:
        fake = _authenticate(FakeKite("key"), settings, stdin)

    assert fake.profile_calls == 0
    assert fake.request_tokens == [TOKEN]


def test_cached_session_for_another_api_key_is_ignored(tmp_path):
    settings = _settings(tmp_path)
    ka.save_session(settings.session_file, ka.SessionRecord("other-key", "cached", "AB1234", ist(2026, 9, 16, 9)))

    with typed_terminal(f"{TOKEN}\n") as stdin:
        fake = _authenticate(FakeKite("key"), settings, stdin)

    assert fake.profile_calls == 0
    assert fake.request_tokens == [TOKEN]
    saved = ka.load_session(settings.session_file)
    assert saved is not None
    assert saved.api_key == "key"


def test_forget_session_flag_forces_a_fresh_login(tmp_path):
    settings = _settings(tmp_path, forget_session=True)
    ka.save_session(settings.session_file, ka.SessionRecord("key", "cached", "AB1234", ist(2026, 9, 16, 9)))

    with typed_terminal(f"{TOKEN}\n") as stdin:
        fake = _authenticate(FakeKite("key"), settings, stdin)

    assert fake.profile_calls == 0
    assert fake.request_tokens == [TOKEN]


def test_pycharm_console_counts_as_a_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCHARM_HOSTED", "1")
    read_fd, write_fd = os.pipe()  # a pipe, like PyCharm's console: isatty() is False
    os.write(write_fd, f"{TOKEN}\n".encode())
    os.close(write_fd)
    with os.fdopen(read_fd) as stdin:
        fake = _authenticate(FakeKite("key"), _settings(tmp_path), stdin)
    assert fake.request_tokens == [TOKEN]


def test_no_terminal_and_no_listener_fails_fast(tmp_path):
    with pytest.raises(RuntimeError, match="no terminal to log in from"):
        _authenticate(FakeKite("key"), _settings(tmp_path), io.StringIO())
    assert not (tmp_path / "kite_session.json").exists()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_redirect_capture_end_to_end_without_a_terminal(tmp_path):
    port = _free_port()
    url = f"http://127.0.0.1:{port}/{REDIRECT_QUERY}"

    def browser():  # keeps knocking until the listener is up, like a real redirect would land
        for _ in range(100):
            try:
                _get(url)
                return
            except OSError:
                time.sleep(0.05)

    threading.Thread(target=browser, daemon=True).start()
    fake = _authenticate(FakeKite("key"), _settings(tmp_path, redirect_port=port), io.StringIO())

    assert fake.request_tokens == [TOKEN]
    saved = ka.load_session(tmp_path / "kite_session.json")
    assert saved is not None
    assert saved.source == "redirect"


def test_login_timeout_is_a_clear_error(tmp_path):
    settings = _settings(tmp_path, redirect_port=_free_port(), login_timeout=0.3)
    with pytest.raises(RuntimeError, match="did not complete within 0 s.*redirect URL"):
        _authenticate(FakeKite("key"), settings, io.StringIO())


def test_auth_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KITE_SESSION_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("KITE_REDIRECT_PORT", "0")
    monkeypatch.setenv("KITE_OPEN_BROWSER", "false")
    monkeypatch.setenv("KITE_FORGET_SESSION", "1")
    s = ka.AuthSettings.from_env()
    assert (s.session_file, s.redirect_port, s.open_browser, s.forget_session) == (tmp_path / "s.json", 0, False, True)

    for name in ("KITE_SESSION_FILE", "KITE_REDIRECT_PORT", "KITE_OPEN_BROWSER", "KITE_FORGET_SESSION"):
        monkeypatch.delenv(name)
    s = ka.AuthSettings.from_env()
    assert (s.session_file, s.redirect_port, s.open_browser, s.forget_session) == (
        ka.DEFAULT_SESSION_FILE,
        ka.DEFAULT_REDIRECT_PORT,
        True,
        False,
    )
