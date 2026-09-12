# ADR-005: Cache the Kite session and capture the login redirect locally

- **Status**: Accepted
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

Every run authenticates from scratch. `authenticate()` prints a login URL,
blocks on `input()` for a request token pasted from the browser's address
bar, exchanges it with `generate_session`, and keeps the access token only in
memory:

```python
def authenticate() -> KiteConnect:
    kite = KiteConnect(api_key=API_KEY)
    print("Login URL:\n", kite.login_url())
    rq = input("Paste request-token ➜ ").strip()
    sess = kite.generate_session(rq, api_secret=API_SECRET)
    kite.set_access_token(sess["access_token"])
    return kite
```

It is called at step 2 of `main()`, before any market data is touched, so
paper runs pay the same cost as live ones. Three problems follow:

- **Retries are expensive.** A run that fails twenty minutes into the candle
  download, or a second paper run to check a parameter, needs a full browser
  login again, even though Kite would have honoured the same token.
- **No TTY, no run.** `input()` makes unattended execution from launchd or
  cron impossible. This is item 5 on the known-rough-edges list.
- **The paste is error-prone.** The redirect lands on whatever URL is
  registered in the developer console with `?request_token=...&action=login&status=success`
  appended, and the user has to extract the token by hand.

What the Kite Connect documentation guarantees (checked 2026-09-12):

- "A successful login comes back with a `request_token` as a URL query
  parameter to the redirect URL registered on the developer console."
- The request token's "lifetime is only a few minutes and it is meant to be
  exchanged for an `access_token` immediately after being obtained."
- The access token, "unless this is invalidated using the API, or invalidated
  by a master-logout from the Kite Web trading terminal, ... will expire at
  `6 AM` on the next day (regulatory requirement)."
- `DELETE /session/token` (`invalidate_access_token` in the client)
  destroys a session on demand.
- The documentation does not state whether a loopback URL such as
  `http://127.0.0.1:<port>/` is accepted as a redirect URL.

The access token is a bearer credential that can place orders on a real
account until 6 AM. Storing it anywhere is a security decision. The existing
threat model already accepts the API secret in a plaintext `.env` on a
personal, single-user Mac; the token is shorter-lived than the secret but
usable without a login.

Unattended scheduling itself is out of scope. Running a money-moving script
without a human present raises questions beyond authentication (failure
notification, fill confirmation, the weekday guard) and gets its own ADR.
This ADR only removes the authentication blocker.

## Decision

`authenticate()` obtains a session in three tiers, trying each in order, and
never logs a token.

### Tier 1: reuse a cached session

After a successful `generate_session`, persist a small JSON record:

```json
{"api_key": "...", "access_token": "...", "user_id": "AB1234",
 "issued_at": "2026-09-17T09:31:04+05:30", "source": "redirect"}
```

- **Location:** `~/.config/stocks-on-the-move/kite_session.json`, overridable
  with `KITE_SESSION_FILE`. Directory mode `0700`, file mode `0600`. The path
  is deliberately outside the repository: ADR-004 makes weekly commits from
  this directory routine, and a git-ignored file is one `git add -f` or one
  `.gitignore` edit away from publishing a live token. `.gitignore` still
  gains a `.kite_session*.json` line as a second guard for anyone who
  overrides the path into the tree.
- **Clock check:** the record is a candidate only if `issued_at` is after the
  most recent 06:00 IST boundary. A token issued at 05:50 is treated as
  expiring at 06:00 the same day; this is stricter than Kite's wording and
  errs toward a fresh login.
- **Liveness check:** a candidate token is installed with `set_access_token`
  and verified with one `kite.profile()` call through `kite_call`. A
  `TokenException` means the token was invalidated by a master logout or the
  API; the file is deleted and the next tier runs. Any other exception
  propagates as today.
- **`api_key` mismatch** in the record (a rotated key) is treated as no cache.
- `KITE_FORGET_SESSION=1` deletes the record before authenticating, for the
  case where the operator wants a clean login without finding the file.

### Tier 2: capture the redirect on a local listener

When no live session exists and `KITE_REDIRECT_PORT` is set to a non-zero
port (default `8765`):

- Bind a one-shot `http.server` on `127.0.0.1:<port>`; if the bind fails,
  log at WARNING and fall to Tier 3.
- Print the login URL and, when `KITE_OPEN_BROWSER` is not `0`, open it with
  the standard library's `webbrowser`.
- Wait up to 300 seconds for a single `GET` whose query string carries
  `request_token` and `status=success`. Answer it with a short "you can close
  this tab" page. Anything else (a favicon request, a failed login) is
  answered with 400 and ignored; the listener keeps waiting until the deadline.
- Exchange the token immediately, write the Tier 1 record with
  `"source": "redirect"`, and return.
- **Prerequisite:** the app's redirect URL in the Kite developer console must
  be `http://127.0.0.1:<port>/`. Changing it affects every program that uses
  this API key. Whether the console accepts a loopback URL must be verified
  before this tier is implemented; if it does not, Tier 2 is dropped from
  this ADR in a Notes entry and Tier 1 plus Tier 3 stand alone.

### Tier 3: manual paste, as today, made tolerant

- Only when `sys.stdin.isatty()`. Without a TTY, raise a clear
  `RuntimeError("no Kite session and no terminal to log in from")` and exit
  non-zero rather than hang on `input()`.
- Accept either the bare token or the whole redirected URL; a small
  `parse_request_token(text)` helper extracts it.
- Write the Tier 1 record with `"source": "manual"`.

### Logging and configuration

- Log `user_id`, `issued_at` and `source` at INFO. Never log `access_token`
  or `request_token`, including in exception messages.
- New environment variables, all documented in `.env.example`:
  `KITE_SESSION_FILE`, `KITE_REDIRECT_PORT` (0 disables Tier 2),
  `KITE_OPEN_BROWSER`, `KITE_FORGET_SESSION`.
- No new runtime dependency. `http.server`, `webbrowser`, `json` and
  `urllib.parse` are standard library.

## Consequences

### Positive

- A same-day retry, or a second paper run, costs nothing. A crash during the
  candle download no longer costs a browser login.
- The daily login shrinks to "click the link, approve in the browser", and
  the TTY requirement disappears for every run after the first each day.
  This is the precondition for a scheduling ADR.
- The paste failure mode is gone in Tier 2 and tolerated in Tier 3.
- Parsing and expiry logic live in a new module without Kite calls, so they
  are unit-testable, which the current `authenticate()` is not.

### Negative

- A live bearer token sits on disk for up to 24 hours. Anyone who can read
  the user's home directory can trade on the account until 6 AM. Accepted
  for a personal single-user machine; see Alternative 2 for the upgrade path
  if that changes.
- Session state now lives in `~/.config`, outside the project's
  everything-is-cwd-relative convention. One more place to look, mitigated
  by logging the path at startup.
- Tier 2 changes the API key's registered redirect URL and binds a loopback
  port for up to five minutes per login. Both are small; both are new.
- One extra `profile()` call per run. Negligible under the 2 requests per
  second throttle.
- The 06:00 rule is encoded from documentation. If Zerodha changes it, the
  liveness check still catches a dead token at the cost of one failed call.

### Neutral

- `authenticate()` keeps its signature and call site; the body moves to a
  new module `stocks_on_the_move/kite_auth.py`.
- Paper and live runs benefit identically.
- Four new environment variables join the existing configuration block.

## Alternatives Considered

### Option 1: Status quo, manual login every run

**Pros:**

- Nothing sensitive is written to disk.
- No code.

**Cons:**

- Every retry is a browser round trip; unattended runs are impossible; the
  paste is fragile.

### Option 2: Store the token in the macOS Keychain via `keyring`

**Pros:**

- Encrypted at rest with OS access control; no plaintext file.

**Cons:**

- A new dependency for a value that expires daily.
- Keychain access prompts can themselves block an unattended run, which
  undercuts the goal.
- Harder to inspect when debugging. Kept as the upgrade path if the machine
  is ever shared.

### Option 3: Fully scripted login with username, password and TOTP

**Pros:**

- Zero human interaction.

**Cons:**

- The daily expiry exists as a regulatory two-factor requirement; automating
  around it puts the account password and TOTP seed on disk and breaks
  whenever the login page changes. Rejected outright.

### Option 4: Write `KITE_ACCESS_TOKEN` into `.env` each day

**Pros:**

- Fits the existing configuration style.

**Cons:**

- `.env` lives inside the repository directory; mixes a daily value with
  static settings; carries no expiry metadata; still needs the capture step.

### Option 5: Cache in the repository directory as a git-ignored file

**Pros:**

- Cwd-relative like the ledgers and the candle cache.

**Cons:**

- One forced add away from a live token on GitHub, in a directory that
  ADR-004 commits from weekly. The home-directory path has no such failure
  mode.

### Option 6: A hosted redirect page instead of a local listener

**Cons:**

- The request token transits a third party; another service to run. Rejected.

## Implementation Plan

1. **Tier 1 and Tier 3**, one commit. New module `kite_auth.py` with
   `load_session`, `save_session`, `session_is_live(record, now)`,
   `parse_request_token(text)`, `authenticate()`. Unit tests for the 06:00
   boundary (05:59 and 06:01 on both sides of midnight), `api_key` mismatch,
   the URL-or-token parser, and the `TokenException` path with a fake client.
   `.gitignore`, `.env.example`, ONBOARDING section 2 and rough edge 5 updated.
2. **Verify the redirect URL prerequisite** in the developer console.
   Record the answer in Notes below.
3. **Tier 2**, one commit, only if step 2 passed. Listener tested in pytest
   by issuing a real loopback request from a thread.
4. **Validation before Implemented:** two paper runs on the same day, the
   second without a prompt; then a master logout from Kite Web followed by a
   run that must detect the dead token and fall through to a fresh login.

## References

- Kite Connect user and session documentation:
  https://kite.trade/docs/connect/v3/user/
- `kiteconnect.KiteConnect`: `login_url`, `generate_session`,
  `set_access_token`, `profile`, `invalidate_access_token`;
  `kiteconnect.exceptions.TokenException`
- `src/stocks_on_the_move/momentum.py`, `authenticate()` and its call site in
  `main()`
- `ONBOARDING.md`, section 2 and rough edge 5
- ADR-001 (process), ADR-004 (why the cache lives outside the repository)
- Future ADR: unattended weekly scheduling

## Implementation Status

Code complete on 2026-09-12; awaiting plan step 2 and step 4 by the owner.

- Plan step 1 and step 3 landed together in `src/stocks_on_the_move/kite_auth.py`
  with `tests/test_kite_auth.py` (parser, 06:00 boundary, cache file
  permissions, listener over loopback, wait loop, `authenticate()` against a
  fake client). `momentum.authenticate()` delegates to it and passes
  `kite_call` for the liveness check.
- Step 2 (redirect URL in the developer console) is not done. Tier 2 ships
  anyway because the refinement in Notes makes it harmless when the console
  still points elsewhere: the paste works at the same time.
- Step 4 validation is outstanding: two paper runs on the same day, the
  second without a prompt; then a master logout from Kite Web followed by a
  run that must detect the dead token and log in again. Status moves to
  Implemented after that.

## Notes

Open question for step 2: does the Kite developer console accept
`http://127.0.0.1:8765/` as a redirect URL? The documentation is silent.

Refinements made while implementing, all inside the decision above:

- **Tiers 2 and 3 wait concurrently, not in sequence.** The listener and the
  paste prompt are active at the same time, and whichever produces a request
  token first wins. A strict sequence would cost five minutes whenever the
  console's redirect URL does not point at the listener yet, and the wait
  would end in a paste anyway. The deadline still bounds the whole wait.
- **"Has a terminal" includes PyCharm's run console.** `sys.stdin.isatty()`
  is False there because PyCharm feeds stdin through a pipe, yet a person is
  typing. The check also accepts stdin when `PYCHARM_HOSTED` is set, which
  PyCharm exports. Without this the owner's usual run configuration would
  fail on the first login of the day.
- **The liveness check tolerates a silent `kite_call`.** `kite_call` returns
  `None` after exhausting retries (rough edge 2, ADR-008). A `None` profile
  logs a WARNING and keeps the cached token; the next Kite call fails
  clearly if the token is dead. Raising here would turn rate limiting into a
  forced login that would hit the same limit.
- **Logging discipline.** `SessionRecord` excludes the access token from its
  `repr`, and the listener overrides `log_request` so the request line, whose
  query string carries the request token, is never logged.
