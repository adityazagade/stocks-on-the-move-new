"""The write endpoints' guard (ADR-031): a token minted when the console starts, presented on every POST.

Loopback binding keeps other machines out; it does not keep out a page open in
the same browser, which can post to localhost. Every page carries the token and
every POST must present it, as the form field or the header below; the Host
check lives in the application's middleware.
"""

from __future__ import annotations

import secrets

HEADER = "X-Console-Token"
FIELD = "_token"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_matches(expected: str, presented: str | None) -> bool:
    return presented is not None and secrets.compare_digest(expected, presented)
