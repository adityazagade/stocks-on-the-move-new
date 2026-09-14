# ADR-031: A local operator console over `runs/`

- **Status**: Accepted
- **Date**: 2026-09-14
- **Last Updated**: 2026-09-14
- **Author**: Aditya Zagade

## Context

The strategy has no user interface. Running it is a terminal command.
Reading what it did is a file browser over `runs/`. The one human step
the design insists on, promoting `next_portfolio.csv` to
`current_portfolio.csv` after checking the fills against Kite (ADR-019,
`ONBOARDING.md` section 5), is a copy command. A deposit is a row typed
into `cash_ledger.csv`. A backtest comparison is `compare` on the console.
The owner asked for a UI.

The code already produces what a UI would show. Every pipeline step writes
its table the moment it completes, and `run.json` carries the regime and
the closing numbers (ADR-006); the run log lands beside them at DEBUG
(ADR-015); the five state files at the root of `runs/` are the account
(ADR-029); each backtest leaves a summary, an equity series and its trades
(ADR-023); the settings snapshot already drops every credential (ADR-006).
None of it is read back by the strategy, so a reader can be added without
touching a rule.

What the terminal does badly is the human half of the weekly loop. A plan
(ADR-022) is eleven CSV files to open before deciding whether to run for
real. The paper-mode trap, `ALLOW_KITE_EXECUTION=0` writing real ledgers,
has no warning on screen. The first real run on 2026-09-13 started with
`TRADING_WEEKDAY=6` left over from a test, and nothing said so. The newest
run directory today records `status: running` with no finish time and five
of eleven files, and nothing distinguishes that from a run still going.
Backtest variants are compared by reading JSON files side by side.

The constraints the UI inherits. The strategy holds no module-level state
and reaches the broker only through the `Broker` protocol (ADR-008); a UI
must not become a second execution path beside the one executor (ADR-022).
`runs/` is the account and is never versioned; the trades ledger is never
edited by hand, because cash is reconstructed from it (ADR-029, CLAUDE.md
rules 4 and 7). No credential is logged or printed (rule 9). Dependencies
come through uv with major-version bounds (ADR-002, ADR-003); the hooks and
CI run ruff, ty and the tests (ADR-010, ADR-011); one maintainer keeps all
of it current. One user, one machine, one run a week.

## Decision

**A local operator console**: a web application served on the loopback
interface, started as `stocks-on-the-move-ui`, that **reads `runs/` and
launches the existing command**. The strategy process does not know it
exists.

**Boundary.** The console lives in `src/stocks_on_the_move/ui/`. It may
import the column lists (`reporting`, `TRADE_COLUMNS` in `ledger`,
`ORDER_COLUMNS` in `execution` and nothing else from `execution`), the
settings and their redacted snapshot (`settings`, `artifacts`), the ledger
readers and append, `ist_now` from `context`, and the session-record
helpers of `kite_auth`. It imports nothing from `rules`, `indicators`,
`pipeline`, `broker`, `candles`, `universe`, `momentum` or `backtest`, and
a test walks the package's imports to enforce that. Every number on a page
is read from a file a run wrote. A view that needs a number no artifact
carries is a change to the run's output under its own ADR, never
arithmetic in a template.

**Three writes, nothing else.**

1. *Start a run.* The console starts `python -m stocks_on_the_move` as a
   child process with the console's own environment and one overlay, the
   mode: `PLAN_ONLY=1` for a plan, `PLAN_ONLY=0` for a booking run. It
   never sets `ALLOW_KITE_EXECUTION`, `KILL_SWITCH`, `FORCE_RESIZE`,
   `ENV_CASHFLOW` or `TRADING_WEEKDAY`: the environment the console was
   started from decides whether its booking run is paper or live, and the
   console shows which. A live run starts only after the operator types
   `LIVE` into a field the server checks. One child at a time.
2. *Promote.* Copy `OUT_FILE` over `PORTFOLIO_FILE`, the same bytes the
   operator copies today, offered only when the newest booking run
   finished `completed` and the page has shown the diff of the two files
   beside that run's `orders.csv`.
3. *Record a cashflow.* Append one `date,amount,note` row to the cash
   ledger through the ledger module's append, extracted from
   `append_env_cashflow_if_any` so both paths write the same row.

The trades ledger, the state file and every run directory are read-only
everywhere in the console. There is no kill switch in the UI; it stays a
command.

**Pages.**

- *Today*: the mode band that every page carries (plan, paper or live,
  and the `RUNS_DIR` path); today's IST weekday against `TRADING_WEEKDAY`;
  whether the cached Kite session is live until 06:00 IST, from the
  session file's issue time and the existing `session_is_live`, showing
  the user id and the issue time and never the token; the newest run and
  its status; the start buttons.
- *Run*: a twelve-step rail lit by which tables exist, then one tab per
  table: universe verdicts, ranking with held names marked, exits with
  reasons, sizing, candidates, orders with the broker's verdicts, trades,
  the portfolio before and after, `run.json`, and the log streamed as it
  grows. A run whose `run.json` says `running` and whose log has not
  changed for ten minutes is shown as *abandoned*; the file is not
  rewritten.
- *Promote*: current against next, differences highlighted, the run's
  orders beside them, one button.
- *Account*: positions from the newest booking run, cash and equity from
  its `run.json`, the cash and trades ledgers as tables, an equity curve
  from `equity_after` across completed booking runs, one series per mode,
  plan runs excluded; the cashflow form.
- *Runs*: the `runs/<date>/<time>-<mode>/` tree as a list, filterable by
  mode and status. The tree is the index; there is no database.
- *Research*: every `runs/backtests/*/`: `summary.json` side by side,
  `equity.csv` overlaid, the regime from `weekly.csv` as a ribbon,
  `trades.csv` as a table, and the summary's first line, the survivorship
  note, pinned above all of it.
- *Settings*: what a run would see, through `settings_snapshot`, with
  values that differ from the defaults marked. Read-only; `.env` is edited
  by hand.

**Stack.** FastAPI and Jinja2 templates rendered on the server, htmx for
partial updates and polling, server-sent events for the log tail, uvicorn
bound to `127.0.0.1`, port 8766 by default with a `--port` flag; the Kite
redirect listener keeps 8765. Charts by uPlot. htmx and uPlot are vendored
as pinned minified files under `ui/static/vendor/` with their versions and
SHA-256 digests in a `VERSIONS` file that a test checks; no Node toolchain,
no `package.json`, nothing fetched from a CDN at page load. Dependencies in
a `ui` dependency group, listed in uv's default groups so `uv sync` and
CI's `--all-groups` install it: `fastapi>=0.115,<1`, `uvicorn>=0.30,<1`,
`jinja2>=3.1,<4`, `python-multipart>=0.0.9,<1`; `httpx>=0.27,<1` in the
dev group for the test client.

**Safety of the write endpoints.** Loopback binding keeps other machines
out; it does not keep out a web page open in the same browser, which can
post to `localhost`. So: writes are POST only; every page carries a token
generated when the console starts and every POST must present it; a
request whose `Host` is not the loopback address the console listens on is
refused; no CORS headers are sent. The child's stdout is shown as it prints
it, except the login URL, which carries the API key as Kite designed it
(ADR-005) and is rendered as a link, never echoed as text. The console
writes no log file; uvicorn's access log goes to its stdout and holds
paths only.

**Not in scope**, each its own ADR if wanted: remote access or
authentication, a database, editing `.env` from the browser, the kill
switch, scheduling the Wednesday run, showing Kite's live positions for
reconciliation (it would need the broker), candle charts per symbol.

## Consequences

### Positive

- The weekly loop has a screen: mode, weekday, session, plan, run,
  promote, in that order, with the traps of 2026-09-13 visible before the
  run starts.
- Every decision shows its reason on its row, from columns the artifacts
  already carry. The strategy, the golden test and the backtest harness
  are untouched by all three stages.
- The console cannot compute a wrong number: it has none of its own. It
  cannot trade except by starting the same command the operator runs, in
  a mode no higher than the environment allows.
- Python only. ruff, ty, pytest and pre-commit cover the console as they
  cover the strategy; nothing new to keep current except two vendored
  files.

### Negative

- Four runtime dependencies and two vendored JavaScript files. Dependabot
  (ADR-013) covers the four; the vendored files are refreshed by hand,
  with their digests, and a test says when `VERSIONS` and the files
  disagree. FastAPI and uvicorn are below 1.0, so a minor release may
  break; the tests are the guard, as with pandas (ADR-003).
- A long-lived process holding a child process, with the failure modes
  that brings: the console dies, the run continues, and the run directory
  is the only record. That is the same record as today.
- Around a thousand lines of code and templates, tests included, for a
  single user.
- Server-rendered pages with htmx are plainer than a single-page
  application; sorting a 500-row table is a round trip.

### Neutral

- The console reads settings from the same environment, so it starts with
  `uv run --env-file .env stocks-on-the-move-ui` and needs the Kite key
  present even though it never uses it; a bad `.env` refuses to start with
  the variable named, as a run does (ADR-007).
- The Kite login happens in the child as today: the browser opens if
  `KITE_OPEN_BROWSER` says so, and the redirect lands on 8765. The console
  only reports the session's state and links the URL.
- Once ADR-030 lands, `STRATEGY` appears in the mode band from the
  settings snapshot; the refusal to switch strategies on a live portfolio
  stays in the command.
- `--port` is a flag, not a `Settings` field, because it configures the
  console and not a run; `.env.example` is unchanged.
- `ONBOARDING.md` section 1 says "no database, no scheduler and no UI";
  it becomes "no database and no scheduler".

## Alternatives Considered

### Option 1: Status quo, the terminal and a file browser

**Pros:**

- Nothing to build or maintain.

**Cons:**

- The human steps of the weekly loop are the least supported part of the
  system, and the traps of the first real run were invisible.

### Option 2: A terminal UI with Textual

The same boundary and the same three writes, rendered in the terminal;
`textual serve` can put it in a browser.

**Pros:**

- Closest to the repository's culture; one dependency; works over SSH.

**Cons:**

- Charts are the weak point, and the research page is mostly charts. The
  console cannot sit in a browser tab beside the Kite positions page
  during the promote step.

### Option 3: Streamlit or NiceGUI

**Pros:**

- The fastest first page.

**Cons:**

- Streamlit's rerun-on-interaction model fights a long-running child and a
  streamed log, and its layout control is too coarse for a confirmation
  step that moves money. NiceGUI brings a Vue frontend and its own server
  model for no gain over Option 4.

### Option 4: A JSON API with a React or Svelte single-page application

**Pros:**

- The richest tables and charts; the conventional shape.

**Cons:**

- A second toolchain: Node, a lockfile, a second Dependabot ecosystem, a
  second linter and formatter, a build step in CI, none of it covered by
  ruff, ty or the uv contract (ADR-002). For one user, one machine and one
  run a week, the cost is felt every week and the benefit never.

### Option 5: The console inside the strategy process

Import `pipeline.run` and call it from a request handler.

**Cons:**

- A second execution path beside the command (ADR-022); a long-lived
  process holding a `RunContext`, against the spirit of ADR-008; a crash
  in the UI takes the run with it. The child process keeps the one
  executor and the one entry point.

### Option 6: Notebooks for the research page only

**Pros:**

- No server.

**Cons:**

- Answers a third of the need and leaves the weekly loop where it is.

## Implementation Plan

Three pull requests, each naming ADR-031, each leaving the golden test
untouched.

1. **Read-only console.** The package, the boundary test, the `ui` group,
   the vendored files and their digest test, the entry point; the Runs,
   Run (without the live tail), Account (without the form), Research and
   Settings pages; a fixture `runs/` tree built from the golden expected
   tables and hand-written `run.json` files; `README.md` gains a section,
   `ONBOARDING.md` a row in the module table and a paragraph in section 2,
   `CLAUDE.md` the command. The owner points it at the real `RUNS_DIR` and
   checks that every number on screen equals the file it came from.
2. **Launch and watch.** The Today page, the child launcher with the mode
   overlay and the one-child rule, the `LIVE` confirmation, the progress
   rail, the streamed log, the abandoned state, the session indicator; the
   launcher tested against a stub command that writes a run directory and
   exits; the write-endpoint token and `Host` checks, with tests that a
   request without them is refused.
3. **Promote and cashflow.** The diff page and the copy, the extracted
   ledger append and the form, both tested against a scratch `RUNS_DIR`,
   and a test that no code path under `ui/` opens the trades ledger for
   writing.
4. **Validation before Implemented.** One paper run started, watched and
   promoted from the console against a scratch `RUNS_DIR`; one plan run
   against the real one; the owner performs the next real promote through
   it.

## References

- ADR-005 (login and session cache), ADR-006 (run artifacts), ADR-007
  (settings), ADR-008 (broker boundary), ADR-015 (run log), ADR-019
  (orders and fills), ADR-022 (plan mode and the one executor), ADR-023
  (backtest outputs), ADR-027 (the state file), ADR-029 (the account under
  `runs/`), ADR-030 (the strategy setting)
- FastAPI: https://fastapi.tiangolo.com/
- htmx: https://htmx.org/
- uPlot: https://github.com/leeoniya/uPlot
- Jinja: https://jinja.palletsprojects.com/

## Notes

Open before Accepted:

- Whether a booking run should also offer `FORCE_RESIZE` as a checkbox.
  Left out here: `.env` decides, and the mode band shows it.
- Whether the promote step should leave a trace. Today it leaves none and
  this ADR keeps it so, to avoid a sixth state file; the run's
  `orders.csv` and the two portfolio files' modification times are the
  record.
- Dependency group versus optional extra for the `ui` dependencies. A
  group keeps `uv sync` as the one command; an extra would keep the
  trading environment free of a web framework at the cost of a second
  flag. The boundary test guards the import either way.
