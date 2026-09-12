"""Kite Connect session handling: cache, redirect capture and manual paste.

Implements ADR-005. ``authenticate`` obtains a working ``KiteConnect`` client
in three tiers, tried in order:

1. reuse the session an earlier run cached today, after confirming it is
   still live with one ``profile()`` call;
2. wait for Kite to redirect the browser to a one-shot listener on
   ``127.0.0.1`` and read the request token from the query string;
3. accept the request token, or the whole redirected URL, pasted at a prompt.

Tiers 2 and 3 wait at the same time when a terminal is present, so a redirect
URL that is not (yet) pointed at the listener costs nothing: the paste still
works. Nothing in this module logs an access token or a request token.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import socketserver
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import IO, Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
# Kite invalidates every access token at this wall-clock time on the day after it was issued.
TOKEN_EXPIRY_IST = dtime(6, 0)

LOGIN_TIMEOUT_SEC = 300.0
_POLL_SEC = 0.5
_BARE_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,}")


def ist_now() -> datetime:
    return datetime.now(IST)


@dataclass(frozen=True)
class AuthSettings:
    """The ADR-005 knobs. ``momentum.authenticate`` builds one from ``Settings`` (ADR-007)."""

    session_file: Path
    redirect_port: int  # 0 disables the listener
    open_browser: bool
    forget_session: bool = False
    login_timeout: float = LOGIN_TIMEOUT_SEC


# ═════════════════════════════════════════════════════════════════════════
# Tier 1: the cached session
# ═════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SessionRecord:
    api_key: str
    access_token: str = field(repr=False)  # keep the bearer token out of repr() and hence out of logs
    user_id: str = ""
    issued_at: datetime = field(default_factory=ist_now)
    source: str = "unknown"  # "redirect" or "manual"

    def to_json(self) -> str:
        data = {
            "api_key": self.api_key,
            "access_token": self.access_token,
            "user_id": self.user_id,
            "issued_at": self.issued_at.isoformat(timespec="seconds"),
            "source": self.source,
        }
        return json.dumps(data, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> SessionRecord:
        data = json.loads(text)
        issued_at = datetime.fromisoformat(data["issued_at"])
        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=IST)
        return cls(
            api_key=str(data["api_key"]),
            access_token=str(data["access_token"]),
            user_id=str(data.get("user_id", "")),
            issued_at=issued_at,
            source=str(data.get("source", "unknown")),
        )


def load_session(path: Path) -> SessionRecord | None:
    """The cached record, or None when there is none or it is unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return SessionRecord.from_json(text)
    except (ValueError, KeyError, TypeError) as exc:  # json.JSONDecodeError is a ValueError
        logger.warning("Ignoring unreadable Kite session file %s (%s)", path, type(exc).__name__)
        return None


def save_session(path: Path, record: SessionRecord) -> None:
    """Write the record with mode 0600 in a directory we own with mode 0700."""
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
        os.chmod(parent, 0o700)  # mkdir's mode is masked by the umask
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(record.to_json())
    os.chmod(path, 0o600)  # tighten a pre-existing, looser file as well


def forget_session(path: Path) -> bool:
    """Delete the cached record. Returns True if there was one."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def last_expiry_boundary(now: datetime) -> datetime:
    """The most recent 06:00 IST at or before ``now`` (which must be timezone-aware)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(IST)
    boundary = local.replace(hour=TOKEN_EXPIRY_IST.hour, minute=TOKEN_EXPIRY_IST.minute, second=0, microsecond=0)
    if boundary > local:
        boundary -= timedelta(days=1)
    return boundary


def session_is_live(record: SessionRecord, now: datetime) -> bool:
    """A cached token is a candidate only if it was issued after the latest 06:00 IST boundary.

    Stricter than Kite's wording on purpose: a token issued at 05:50 is treated as
    dying at 06:00 the same day, which errs toward a fresh login.
    """
    return record.issued_at > last_expiry_boundary(now)


# ═════════════════════════════════════════════════════════════════════════
# Tier 2: capture the login redirect on 127.0.0.1
# ═════════════════════════════════════════════════════════════════════════
class _RedirectHandler(BaseHTTPRequestHandler):
    server: RedirectListener

    def do_GET(self) -> None:  # noqa: N802 (name fixed by http.server)
        query = parse_qs(urlsplit(self.path).query)
        token = query.get("request_token", [""])[0].strip()
        status = query.get("status", ["success"])[0]
        if token and status == "success":
            self.server.deliver(token)
            self._reply(200, "Login received. You can close this tab and return to the terminal.")
        else:
            self._reply(400, "Stocks on the Move login listener: no request_token in this request.")

    def _reply(self, code: int, text: str) -> None:
        body = f"<!doctype html><title>Stocks on the Move</title><p>{text}</p>\n".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # The stock implementations print the request line, whose query string carries the
    # request token. Log the path only, and only at DEBUG.
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        logger.debug("redirect listener: %s %s -> %s", self.command, urlsplit(self.path).path, code)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("redirect listener: " + fmt, *args)


class RedirectListener(HTTPServer):
    """One-shot HTTP listener on 127.0.0.1 that captures Kite's login redirect.

    ``RedirectListener(port).start()`` serves on a daemon thread; ``wait`` returns the
    first request token delivered; ``close`` stops the thread and frees the port.
    Port 0 asks the OS for a free port (used by the tests).
    """

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _RedirectHandler)
        self._token: str | None = None
        self._received = threading.Event()
        self._thread: threading.Thread | None = None

    def server_bind(self) -> None:
        # HTTPServer.server_bind resolves the host with getfqdn(), which can stall on DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def redirect_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> RedirectListener:
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.2}, name="kite-redirect-listener", daemon=True
        )
        self._thread.start()
        return self

    def deliver(self, token: str) -> None:
        if self._token is None:  # first successful redirect wins
            self._token = token
        self._received.set()

    def wait(self, timeout: float) -> str | None:
        self._received.wait(timeout)
        return self._token

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self.shutdown()
            self._thread.join(timeout=2)
        self.server_close()


# ═════════════════════════════════════════════════════════════════════════
# Tier 3: the pasted token, and the shared wait loop
# ═════════════════════════════════════════════════════════════════════════
def parse_request_token(text: str) -> str | None:
    """Extract a request token from a bare token, a redirected URL or a query string."""
    text = text.strip()
    if not text:
        return None
    if "request_token=" in text:
        parts = urlsplit(text)
        query = parts.query or text.lstrip("?")
        values = parse_qs(query).get("request_token", [])
        token = values[0].strip() if values else ""
        return token or None
    return text if _BARE_TOKEN.fullmatch(text) else None


def _stdin_is_interactive(stream: IO[str] | None) -> bool:
    if stream is None or getattr(stream, "closed", False):
        return False
    try:
        if stream.isatty():
            return True
    except (AttributeError, ValueError):
        return False
    # PyCharm's run console feeds stdin through a pipe, so isatty() is False there even
    # though somebody is typing. PyCharm marks its console with this variable.
    return bool(os.environ.get("PYCHARM_HOSTED"))


def _readline_within(stream: IO[str], timeout: float) -> str | None:
    """One line if it arrives within ``timeout`` seconds, None if not; "" means end of input."""
    try:
        ready, _, _ = select.select([stream], [], [], timeout)
    except (OSError, ValueError):  # no usable file descriptor: fall back to a blocking read
        return stream.readline()
    return stream.readline() if ready else None


def wait_for_request_token(
    listener: RedirectListener | None,
    stdin: IO[str] | None,
    timeout: float,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, str]:
    """``(request_token, source)`` from the listener or a pasted line, whichever comes first.

    ``source`` is ``"redirect"`` or ``"manual"``. Raises ``TimeoutError`` at the deadline.
    """
    deadline = clock() + timeout
    while True:
        if listener is not None and (token := listener.wait(0)) is not None:
            return token, "redirect"
        remaining = deadline - clock()
        if remaining <= 0 or (listener is None and stdin is None):
            break
        step = min(_POLL_SEC, remaining)
        if stdin is None:
            listener.wait(step)  # type: ignore[union-attr]  (listener is not None here)
            continue
        line = _readline_within(stdin, step)
        if line is None:
            continue
        if line == "":  # end of input: nobody is typing, keep waiting on the listener only
            stdin = None
            continue
        token = parse_request_token(line)
        if token is not None:
            return token, "manual"
        print("That is neither a request token nor a redirected URL. Try again ➜ ", end="", flush=True)
    raise TimeoutError(f"no request token within {timeout:.0f} s")


def _obtain_request_token(login_url: str, settings: AuthSettings, stdin: IO[str] | None) -> tuple[str, str]:
    listener: RedirectListener | None = None
    if settings.redirect_port:
        try:
            listener = RedirectListener(settings.redirect_port).start()
        except OSError as exc:
            logger.warning(
                "Cannot listen on 127.0.0.1:%d for the Kite redirect (%s); paste the token instead",
                settings.redirect_port,
                exc,
            )
    interactive = _stdin_is_interactive(stdin)
    if listener is None and not interactive:
        raise RuntimeError(
            "no Kite session and no terminal to log in from "
            "(run from a terminal, or set KITE_REDIRECT_PORT so the login redirect can be captured)"
        )
    try:
        print(f"Log in to Kite:\n  {login_url}")
        if listener is not None:
            print(f"Waiting for the redirect on {listener.redirect_url} (the app's redirect URL in the Kite console)")
        if interactive:
            print("Or paste the request token / redirected URL here ➜ ", end="", flush=True)
        if settings.open_browser:
            webbrowser.open(login_url)
        try:
            return wait_for_request_token(listener, stdin if interactive else None, settings.login_timeout)
        except TimeoutError:
            hints = []
            if listener is not None:
                hints.append(f"the app's redirect URL in the Kite developer console must be {listener.redirect_url}")
            if interactive:
                hints.append("or paste the request token at the prompt")
            raise RuntimeError(
                f"Kite login did not complete within {settings.login_timeout:.0f} s; " + ", ".join(hints)
            ) from None
    finally:
        if listener is not None:
            listener.close()


# ═════════════════════════════════════════════════════════════════════════
# The three tiers together
# ═════════════════════════════════════════════════════════════════════════
def _direct_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return fn(*args, **kwargs)


def _usable_cached_session(path: Path, api_key: str, now: datetime) -> SessionRecord | None:
    record = load_session(path)
    if record is None:
        return None
    if record.api_key != api_key:
        logger.info("Cached Kite session at %s belongs to a different API key; ignoring it", path)
        return None
    if not session_is_live(record, now):
        logger.info(
            "Cached Kite session issued %s has passed the 06:00 IST expiry; logging in again",
            record.issued_at.isoformat(timespec="seconds"),
        )
        forget_session(path)
        return None
    return record


def _session_is_alive(kite: KiteConnect, call: Callable[..., Any]) -> bool:
    try:
        profile = call(kite.profile)
    except TokenException:
        logger.info("Cached Kite session was invalidated (master logout or API); logging in again")
        return False
    if profile is None:  # the throttled wrapper gives up silently after sustained rate limiting
        logger.warning("Could not confirm the cached Kite session with Kite; continuing with it anyway")
    return True


def authenticate(
    api_key: str,
    api_secret: str,
    *,
    settings: AuthSettings,
    kite_factory: Callable[..., KiteConnect] = KiteConnect,
    call: Callable[..., Any] = _direct_call,
    now: Callable[[], datetime] = ist_now,
    stdin: IO[str] | None = None,
) -> KiteConnect:
    """Return a ``KiteConnect`` with a live access token, logging in only when needed.

    ``call`` wraps the one liveness request (pass the throttled ``kite_call``);
    ``kite_factory``, ``now`` and ``stdin`` exist for the tests.
    """
    stdin = sys.stdin if stdin is None else stdin
    path = settings.session_file

    if settings.forget_session and forget_session(path):
        logger.info("KITE_FORGET_SESSION set: removed the cached Kite session at %s", path)

    kite = kite_factory(api_key=api_key)

    record = _usable_cached_session(path, api_key, now())
    if record is not None:
        kite.set_access_token(record.access_token)
        if _session_is_alive(kite, call):
            logger.info(
                "Reusing Kite session for %s issued %s via %s (cache: %s)",
                record.user_id,
                record.issued_at.isoformat(timespec="seconds"),
                record.source,
                path,
            )
            return kite
        forget_session(path)
        kite.set_access_token(None)

    request_token, source = _obtain_request_token(kite.login_url(), settings, stdin)
    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
    except TokenException as exc:
        logger.error(
            "Kite rejected the request token (%s). Request tokens expire within minutes; run again "
            "and finish the login promptly.",
            exc,
        )
        raise
    access_token = session["access_token"]
    kite.set_access_token(access_token)
    record = SessionRecord(
        api_key=api_key,
        access_token=access_token,
        user_id=str(session.get("user_id", "")),
        issued_at=now(),
        source=source,
    )
    save_session(path, record)
    logger.info("Kite session established for %s via %s; cached at %s until 06:00 IST", record.user_id, source, path)
    return kite
