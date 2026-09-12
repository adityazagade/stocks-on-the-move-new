# Developer Onboarding

This guide is for someone about to change the code. It explains how the
strategy runs, the decisions baked into the module, the workflow we follow,
and the things that catch newcomers. Setup and day-to-day commands are in
[README.md](README.md); every environment variable is listed in
[.env.example](.env.example).

## 1. What you are working on

A weekly momentum-rotation strategy for NSE equities, after Andreas Clenow's
*Stocks on the Move*. Once a week it:

1. checks whether the market is in a bull regime (NIFTY 50 above its 200-day EMA),
2. ranks the NIFTY 500 by a momentum score,
3. sells holdings that fell out of the top of the ranking, dropped below their
   100-day EMA, or hit a trailing stop,
4. every second week, resizes what is left toward an ATR-based risk target,
5. if bullish and there is cash, buys down the ranking until it runs out of
   cash or slots.

Orders go through Zerodha Kite Connect. State lives in four CSV files in the
working directory. There is no database, no scheduler and no UI. The whole
strategy is one module, `src/stocks_on_the_move/momentum.py`, run top to
bottom by `run()` once `main()` has assembled a `RunContext`. Beside it sit
`broker.py` and `candles.py` (the broker boundary and the candle cache,
ADR-008), `artifacts.py` (the per-run directory, ADR-006), `settings.py`
(every knob, ADR-007) and `kite_auth.py` (the Kite login, ADR-005).

Where this port deviates from the book, and it matters when you read the code:

- The ranking score is not the book's "annualised regression slope x R²".
  It is a weighted blend of trailing returns, `0.6*R5 + 0.3*R15 + 0.1*R45`,
  multiplied by the R² of a 90-day log-linear fit. The annualised slope is
  computed and logged but does not affect the order.
- The lookbacks are 5, 15 and 45 trading days (`LOOKBACK_SHORT/MID/LONG`,
  weights `WEIGHT_SHORT/MID/LONG`), shortened from the book's 21/63/126 before
  version control for a reason nobody recorded. Changing them again is a
  strategy ADR with the golden test as its evidence (ADR-016).
- Regime and trend filters use EMAs where the book uses simple moving averages.

## 2. Your first hour

```sh
uv sync                      # Python 3.13 + all deps into .venv
uv run pytest                # the whole suite, a few seconds
uv run pre-commit install    # hooks: ruff, uv-lock, whitespace
cp .env.example .env         # add KITE_API_KEY / KITE_API_SECRET
```

Then do a paper run. Two things to know before you press enter:

- **The weekday guard.** `main()` exits immediately unless today, in IST, is
  `TRADING_WEEKDAY` (default 2, Wednesday). Override it to today.
- **Paper mode still writes the ledgers.** `ALLOW_KITE_EXECUTION=0` skips
  `place_order` but still appends to the trades ledger and rewrites
  `next_portfolio.csv`. Point those at scratch files or you will corrupt the
  cash reconstruction for the real account (see section 4).

Create `.env.paper` with overrides and layer it over `.env`:

```sh
cat > .env.paper <<'ENV'
ALLOW_KITE_EXECUTION=0
OUT_FILE=/tmp/sotm/next_portfolio.csv
CASH_LEDGER_FILE=/tmp/sotm/cash_ledger.csv
TRADES_LEDGER_FILE=/tmp/sotm/trades_ledger.csv
ENV
mkdir -p /tmp/sotm
TRADING_WEEKDAY=$(( $(TZ=Asia/Kolkata date +%u) - 1 )) \
  uv run --env-file .env --env-file .env.paper stocks-on-the-move
```

Leave `CACHE_DIR` alone: the candle cache is plain market data, sharing it
saves a few thousand API calls, and the code repairs it if it is stale.

The first run of the day logs in to Kite (ADR-005). It prints the login URL,
opens it in your browser, and listens on `http://127.0.0.1:8765/` for the
redirect that Kite sends after you approve. For that to land, the redirect
URL of your app in the Kite developer console must be exactly that address.
If it is something else, paste the request token, or the whole redirected
URL, at the prompt instead; both work. The access token is then cached in
`~/.config/stocks-on-the-move/kite_session.json` (mode 0600) and reused
until it dies at 06:00 IST, so a retry or a second paper run the same day
needs no login. `KITE_FORGET_SESSION=1` discards the cache; the knobs are in
`.env.example`. Paper mode still needs this login because it pulls live
prices and history.

A full run against the NIFTY 500 with a cold cache takes a while: the
throttle holds Kite calls to `KITE_RPS` (default 2 per second) and sleeps
`CANDLE_SLEEP_SEC` after each history download.

## 3. The pipeline, step by step

`main()` is numbered in comments. This is the same list with the functions
behind each step.

| # | Step | Functions | Notes |
| --- | --- | --- | --- |
| 1 | Weekday guard, log in | `main`, `authenticate` | In `main()`, before the login, so a non-trading day costs nothing. `authenticate` returns a `KiteBroker`; paper mode wraps it in `PaperBroker` |
| 2 | Load state | `read_portfolio`, `init_cash_balance` | First step of `run(ctx)`. Cash is reconstructed from ledgers, never stored |
| 3 | Token cache | `build_token_cache` | One `instruments("NSE")` call into `ctx.tokens`, then everything is a dict lookup |
| 3.5 | Kill switch | `liquidate_all` | Sells everything, writes `OUT_FILE`, returns |
| 4 | Universe | `nse_universe_symbols`, or `ctx.universe` | Public CSVs from NSE archives, no auth; tests inject a set. Empty universe aborts the run |
| 5 | Regime | `index_trend` | Index close vs 200-day EMA. Only gates buys, never sells |
| 6 | Rank | `get_universe`, `rank_universe`, `_composite_momentum` | Filters then scores; see section 4 |
| 7 | Exits | `prune_portfolio`, `should_exit`, `_trailing_stop_hit` | Runs every week, bull or bear |
| 8 | Raise cash | `raise_cash_if_needed` | Only when a withdrawal drove cash negative |
| 9 | Resize | `resize_positions`, `target_shares` | Even ISO weeks only, or `FORCE_RESIZE=1` |
| 10 | Mark to market | `live_value` | Batched `ltp()` |
| 11 | Buys | `target_shares`, `safe_buy` | Bull regime and cash > 0 only. Every order waits for the broker's verdict (`await_fill`, ADR-019) and books what filled |
| 12 | Snapshot | `write_portfolio` | Writes `OUT_FILE`; the human promotes it to `PORTFOLIO_FILE` |

### Filters and scoring (step 6)

A stock is ranked only if it passes all of these, in order:

1. Kite lists it as `instrument_type == "EQ"` in segment `NSE` and its base
   symbol is in the NIFTY 500 list.
2. Enough daily candles: `max(MA_FILTER_100, LOOKBACK_LONG + 1, REG_LOOKBACK + 1)` rows.
3. Last close above the 100-day EMA.
4. 20-day average volume at least `MIN_VOLUME`.
5. ATR(20) no more than `MAX_ATR_PCT` of price.

Every instrument's verdict, `ranked` or `excluded` with the rule that
stopped it (`history`, `below_ema100`, `volume`, `atr_pct`,
`insufficient_data`, `error:<type>`), is a row in the run's
`universe.csv` (ADR-006), so a symbol disappearing from the ranking is a
file open, not a re-run at DEBUG.

### Exit rules (step 7)

A holding is sold when any of these hold:

- it is not in the ranking at all (it failed a filter above),
- its percentile rank is worse than `CUT_OFF_PCT` (default top 20 %),
- its close is at or below its 100-day EMA,
- trailing stop: close is more than `EXIT_MULTIPLE` ATRs below the 40-day
  rolling maximum close.

Sold symbols go into `ctx.portfolio.sold` and are not bought back in the
same run. A holding that should be sold but cannot be priced (no quote, no
last price) stays exactly as it was, with a WARNING and a `SKIP:no_price`
row in `exits.csv`: a position changes only when a trade was placed, in
every path (ADR-017). If the ranking is empty the prune step is skipped
entirely, as a guard against liquidating everything on a bad data day.

### Sizing (steps 9 and 11)

`target_shares` = `account_equity * RISK_FACTOR / ATR`, capped so no
position exceeds `MAX_WEIGHT` of equity. Equity is cash plus mark-to-market
value and is recomputed after every buy. When a buy is unaffordable the code
buys as many shares as the cash covers rather than skipping the name.

## 4. Design decisions you should not undo

**Every broker call goes through the `Broker` protocol** in `broker.py`
(ADR-008). `KiteBroker` spaces requests to `KITE_RPS` with jitter, retries
`429 / too many requests` with exponential backoff and raises `BrokerError`
when the retries run out. Never touch `KiteConnect` outside that adapter;
`momentum.py` does not import `kiteconnect`.

**Instrument tokens come from one `instruments()` download per run.**
`token_of` is a lookup in `ctx.tokens` with a single fallback scan, and
`KiteBroker` memoises `instruments()` per exchange. Do not reintroduce
`quote()` for token resolution; it was the main source of rate-limit errors.

**Candles are cached per instrument token in `CACHE_DIR/<token>.csv`.**
`CandleStore.get` (`candles.py`) fetches incrementally and, on every update, re-downloads a
30-day overlap and compares the first overlapping close with the cache. A
mismatch means a split or bonus rewrote history, so the cache file is
deleted and the full history re-fetched. If you change the CSV schema, delete
the cache directory.

**Cash is reconstructed, not stored.** On every run,
`ctx.portfolio.cash = STARTING_CASH + sum(cash_ledger.amount) + sum(trades_ledger.cash_delta)`.
The two ledgers are the source of truth for cash, which means:

- never hand-edit `trades_ledger.csv`,
- to deposit or withdraw, append a row to `cash_ledger.csv` or set
  `ENV_CASHFLOW` for exactly one run. It appends a dated row every time the
  process starts with it set, so never leave it in `.env`,
- if you change `STARTING_CASH` you change the meaning of every historical row.

**A trade is booked only after the broker confirms the fill** (ADR-019).
`_place` sends the order, keeps the id, and `await_fill` polls the status
until it is `COMPLETE`, `REJECTED` or `CANCELLED`, cancelling at the timeout.
What reaches the ledger and the portfolio is the filled quantity at the
broker's average price, never the requested quantity at the price seen. A
`Fill` with zero filled means the order was sent and came to nothing; `None`
from `safe_buy` or `safe_sell` means nothing was sent at all.

**Fees and slippage are booked at trade time.** `record_trade` debits
`price * (1 + FEES_PCT + slippage)` on buys and credits
`price * (1 - FEES_PCT - slippage)` on sells, where `slippage` is
`SLIPPAGE_PCT` for a paper fill and zero for a live one, whose average price
already contains it. The ledger is the model of the account, not a broker
statement.

**Order type depends on the NSE series.** Plain `EQ` names use market orders
priced from a batched `ltp()`. Series in `NO_MARKET_SERIES` (`BE`, `BZ`,
`BT`, ...) trade in a no-market-order segment, so they get limit orders at
the best bid or ask from a `quote()` depth call. `series_of` and
`base_symbol` do the parsing; both are unit tested.

**All scheduling is IST.** Weekday checks and ISO-week parity use
`ist_now()`, the candle window ends on the IST date (ADR-018), and candle
dates are timezone-aware IST.

**Run artifacts are diagnostics, not state.** Every run past the weekday
guard writes `runs/<date>/<time>-<mode>/` (ADR-006): the universe verdicts,
the ranking, the exit reasons, the target sizes, the buy candidates, the
portfolio before and after, this run's trades, the log, and `run.json` with
the settings (credentials removed), the regime and the closing numbers.
`runs/latest` points at the newest. The strategy never reads any of it, the
tree is git-ignored, and a failed write is a WARNING, never an abort. The
rule behind it: state the code reads in order to run (the ledgers) is
versioned; output the code produces is not.

**Configuration is one validated object.** `settings.Settings` (ADR-007) holds
every environment knob with its type, default and, where a wrong value is
dangerous, its range. `main()` loads it once with `Settings.from_env()` and
puts it on the `RunContext`; importing the module reads nothing and
creates nothing. A bad value refuses to start with the variable named;
`uv run --env-file .env python -m stocks_on_the_move.settings --check` shows
what a run would see. Tests build one with `Settings.from_values(...)`; see
`tests/conftest.py`.

**No module-level mutable state.** Everything a run mutates lives on the
`RunContext` (ADR-008): `ctx.portfolio` holds positions, cash and the names
sold this run; `ctx.tokens` the instrument tokens. `run(ctx)` can be called
as often as you like with fresh contexts, which is how the pipeline tests
work.

## 5. Files and their lifecycle

| File | Written by | Read by | Notes |
| --- | --- | --- | --- |
| `current_portfolio.csv` | you | step 2 | `SYMBOL,QUANTITY`, no header |
| `next_portfolio.csv` | step 12 | you | Copy over `current_portfolio.csv`; it already holds the fills the broker confirmed (ADR-019) |
| `cash_ledger.csv` | you, or `ENV_CASHFLOW` | step 2 | `date,amount,note` |
| `trades_ledger.csv` | every confirmed fill | step 2 | Append-only; filled quantity and the broker's average price (ADR-019) |
| `.cache_candles/<token>.csv` | `CandleStore` | `CandleStore` | `date,open,high,low,close,volume`; git-ignored, safe to delete |
| `runs/<date>/<time>-<mode>/` | every step, as it completes | you | Eleven files per run (ADR-006; `orders.csv` since ADR-019); git-ignored; `runs/latest` is a symlink to the newest |

The gap between step 12 and the next run's step 2 is deliberate: promoting
the snapshot is a human act. Since ADR-019 the snapshot records what the
broker confirmed filled, at the broker's average price, so comparing it with
the Kite positions page is a check, not a correction. `orders.csv` in the run
directory has every order id and its verdict if the two disagree.

## 6. How we work

**Tooling.** uv owns the environment; `uv.lock` is committed and the
`uv-lock` pre-commit hook fails the commit if `pyproject.toml` and the lock
disagree. Use `uv add` / `uv add --dev` / `uv remove`, never `pip`.
`uv lock --upgrade && uv sync` refreshes within the bounds in
`pyproject.toml`. pandas is held below 3.0 on purpose: 3.x changes
copy-on-write and string-dtype defaults and the strategy math has not been
re-validated against it. Dependabot (ADR-013) proposes the refresh for you:
every Monday one grouped pull request per ecosystem (`uv`, `github-actions`,
`pre-commit`), commit subjects prefixed `ADR-013:`. Merge by hand, only with
CI green and the pandas and kiteconnect changelogs read; never auto-merge. The
ty hook `rev` must equal the `ty` pin in the dev group and the ruff `rev`
should track the ruff dev dependency, so merge the `uv` and `pre-commit` pull
requests together when both touch those.

**Lint and format.** ruff, configured in `pyproject.toml`, 120-column lines,
rule sets E/W/F/I/UP/B/C4/SIM. `archive/` is excluded and must stay that way.

**Secret scanning.** gitleaks runs on every commit and in CI (ADR-012) with the
default rules plus `.gitleaks.toml`, which names this project's credentials:
a `KITE_API_KEY` or `KITE_API_SECRET` with a value, or an `access_token` in a
session file. Findings are redacted. `.env.example`, `docs/adr/` and this file
are the only allowlisted paths; a false positive on test data gets a
fingerprint in `.gitleaksignore` with a comment, never a wider allowlist.
`detect-private-key` runs alongside. A secret pasted into a chat or a log is
outside what scanning can catch: rotate it.

**Type checking.** ty (ADR-010), pinned exactly in the dev group and at the
same version in `.pre-commit-config.yaml`; `uv run ty check` checks `src/` and
`tests/` against Python 3.12, the lowest version the project promises. The
hook blocks a commit on any diagnostic. Suppress only where the checker is
wrong, on one line, naming the rule with a reason:
`# ty: ignore[rule-name]  (why)`. ty is beta and its diagnostics can change
between versions, so version bumps are deliberate; if two bumps in a row
cost fix-ups for churn alone, ADR-010 names pyright as the replacement.
The pre-commit hooks run ruff on every commit; `uv run pre-commit run
--all-files` runs them by hand. GitHub Actions runs those same hooks, plus
the test suite on Python 3.12 and 3.13, on every push to `main` and every
pull request (`.github/workflows/ci.yml`, ADR-011). Anything added to
`.pre-commit-config.yaml` is in CI by construction. There is no branch
protection: a red run on `main` is a signal to fix, not a block.

**Logging.** Configured in `main()`, never at import, on the
`stocks_on_the_move` logger only (ADR-015): the console shows `LOG_LEVEL`
(default `INFO`) and the run's `run.log` under `runs/` gets everything at
DEBUG with the logger name and source line. So "why did that not print" is
usually "it is in the file". Level policy: WARNING for anything skipped or
swallowed that a person should look at (a symbol dropped from the ranking by
an exception, a sizing error, a rate-limit backoff); INFO for decisions and
totals; DEBUG for per-call detail; never a token, a secret or a Kite response
body. `LOG_LEVEL=DEBUG` gives a verbose console for a live investigation.

**Fills are confirmed, not assumed** (ADR-019). Every order waits, inside
`safe_buy` or `safe_sell`, until the broker reports it `COMPLETE`, `REJECTED`
or `CANCELLED`, polling every `FILL_POLL_SECONDS` for up to
`FILL_TIMEOUT_SECONDS` (default two minutes); past that the order is
cancelled and whatever filled is booked. The ledger row carries the filled
quantity and the broker's average price, with `slippage_pct` zero for a live
fill because the average already contains it; paper rows keep the configured
estimate. A partial fill moves the position by what filled and shows as
`SELL:partial` or `BUY:partial` in the run's tables; a rejection books
nothing and shows as `SKIP:no_fill` with the broker's message in
`orders.csv`. A live run with a few limit orders on `BE` names can therefore
take minutes longer than a paper run, which waits for nothing.

**Tests.** `tests/test_settings.py` covers parsing, ranges and the generated
`.env.example`. `tests/test_kite_auth.py` covers the login module against a
fake client. `tests/test_momentum.py` covers the pure helpers: symbol parsing,
portfolio CSV round-trips, fee arithmetic, ATR and the momentum score.
`tests/test_broker.py` covers the Kite adapter's mapping and backoff and the
paper wrapper; `tests/test_fills.py` the wait for a fill, the booking rules and
the five places a position follows a fill (ADR-019); `tests/test_candles.py` the cache paths; `tests/test_artifacts.py`
the run directory; `tests/test_golden.py` is the golden-file regression test
(below); `tests/test_pipeline.py`
everything above the helpers, including whole `run(ctx)` calls in bull, bear
and kill-switch markets. To test a strategy function, take the `ctx` fixture
(a `RunContext` over `fakes.FakeBroker` with the clock frozen on a Wednesday
in an even ISO week), add instruments with `broker.add_equity(...)` and
prices with `broker.ltps[...]`, then assert on `broker.orders` and
`ctx.portfolio`.

**The golden test.** `tests/test_golden.py` (ADR-009) runs the whole pipeline
against the frozen, synthetic inputs in `tests/fixtures/golden/` twice, on an
even and an odd ISO week, and compares the ADR-006 tables (`universe`,
`ranking`, `exits`, `sizing`, `candidates`, `trades`, `portfolio_after`) with
the committed files under `expected/`. It is the answer to "did the ranking,
the exits or the sizes change" for a strategy edit, a pandas upgrade (ADR-003)
or a Python bump. When a change is meant to alter them, run
`uv run pytest --update-golden`, read the diff of `expected/`, and explain it
in the commit body; a silently regenerated golden is no test at all. The
fixtures are seeded random walks, not market data, because the repository is
public; `make_fixtures.py` documents the role each instrument plays and is
re-run only deliberately, followed by `--update-golden`.

**The assistant contract.** `CLAUDE.md` at the repository root is the
eighty-line summary of the rules a coding assistant must follow here
(ADR-014); Claude Code reads it at the start of every session. It is a
summary, not the source: any ADR that changes a listed rule updates it in
the same commit.

**Decisions.** Every change beyond a typo starts with an Architecture
Decision Record in `docs/adr/`. Draft it as Proposed, commit it on its own,
wait for it to be Accepted, then implement, naming the ADR in every commit.
ADR-001 defines the process and the threshold; `docs/adr/README.md` is the
index.

**Commits.** Small, one concern each, imperative subject, body says why.
Look at `git log` for the house style. Do not create `momentum_v5.py`; the
`archive/` folder exists because that used to be how versions were tracked,
and git now does that job.

**Changing strategy parameters.** Anything a user should be able to tune is a
field on `Settings` in `settings.py`, with a description and, where a wrong
value is dangerous, a range. List it in `EXAMPLE_SECTIONS` and regenerate
`.env.example` with the command printed at the top of that file; a test
fails if the two drift. Fixed strategy constants (lookbacks, EMA lengths)
stay at the top of `momentum.py`. Anything that changes the meaning of past
ledger rows (fees, starting cash) deserves a note in the commit body.

## 7. Known rough edges

Things we know about and have not fixed. Each one needs an ADR before the
fix; see `docs/adr/`. Nothing is open at the moment. The six items this list
carried in September 2026 are closed by ADR-005, 006, 008, 015, 016, 017, 018
and 019, which still refer to them by their old numbers; the last to close,
"limit-order fills are assumed", went with ADR-019's broker-confirmed fills.

## 8. Glossary

- **ATR** Average True Range, here a simple 20-day mean of the true range. Used for sizing and the trailing stop.
- **EMA** Exponential moving average, `pandas.ewm(span=N, adjust=False)`.
- **ISO week parity** `ist_now().isocalendar().week % 2`; even weeks resize.
- **LTP** Last traded price, from Kite's batched `ltp()` endpoint.
- **Series** The NSE suffix on a tradingsymbol (`-BE`, `-BZ`). `EQ` is the normal rolling-settlement series and has no suffix.
- **tradingsymbol / instrument_token** Kite's human-readable name and numeric id for an instrument. Candles are keyed by token, orders by tradingsymbol.
- **CNC** Kite's cash-and-carry (delivery) product type. All orders here are CNC.
