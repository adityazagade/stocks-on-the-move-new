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
bottom by `main()`.

Where this port deviates from the book, and it matters when you read the code:

- The ranking score is not the book's "annualised regression slope x R²".
  It is a weighted blend of trailing returns, `0.6*R5 + 0.3*R15 + 0.1*R45`,
  multiplied by the R² of a 90-day log-linear fit. The annualised slope is
  computed and logged but does not affect the order.
- The lookbacks are 5, 15 and 45 trading days. The constants are still named
  `LOOKBACK_R21`, `LOOKBACK_R63`, `LOOKBACK_R126` and the docstring still
  says "R21 + R63 + R126": the values were shortened in the last iteration
  (v3 to v4) and the names were not. Trust the values.
- Regime and trend filters use EMAs where the book uses simple moving averages.

## 2. Your first hour

```sh
uv sync                      # Python 3.13 + all deps into .venv
uv run pytest                # 13 tests, well under a second
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

The run prints a Kite login URL. Open it, log in, and Kite redirects to the
URL registered for your app with `request_token=...` in the query string.
Paste that token at the prompt. Access tokens expire daily, so every run is
interactive. Paper mode still needs this login because it pulls live prices
and history.

A full run against the NIFTY 500 with a cold cache takes a while: the
throttle holds Kite calls to `KITE_RPS` (default 2 per second) and sleeps
`CANDLE_SLEEP_SEC` after each history download.

## 3. The pipeline, step by step

`main()` is numbered in comments. This is the same list with the functions
behind each step.

| # | Step | Functions | Notes |
| --- | --- | --- | --- |
| 1 | Weekday guard | `ist_now` | Skipped when `KILL_SWITCH=1` |
| 2 | Load state, log in | `read_portfolio`, `init_cash_balance`, `authenticate` | Cash is reconstructed from ledgers, never stored |
| 3 | Token cache | `build_token_cache` | One `instruments("NSE")` call, then everything is a dict lookup |
| 3.5 | Kill switch | `liquidate_all` | Sells everything, writes `OUT_FILE`, returns |
| 4 | Universe | `fetch_index_constituents`, `fetch_nifty_constituents` | Public CSVs from NSE archives, no auth. Empty universe aborts the run |
| 5 | Regime | `index_trend` | Index close vs 200-day EMA. Only gates buys, never sells |
| 6 | Rank | `get_universe`, `rank_universe`, `_composite_momentum` | Filters then scores; see section 4 |
| 7 | Exits | `prune_portfolio`, `should_exit`, `_trailing_stop_hit` | Runs every week, bull or bear |
| 8 | Raise cash | `raise_cash_if_needed` | Only when a withdrawal drove cash negative |
| 9 | Resize | `resize_positions`, `target_shares` | Even ISO weeks only, or `FORCE_RESIZE=1` |
| 10 | Mark to market | `live_value` | Batched `ltp()` |
| 11 | Buys | `target_shares`, `safe_buy` | Bull regime and cash > 0 only |
| 12 | Snapshot | `write_portfolio` | Writes `OUT_FILE`; the human promotes it to `PORTFOLIO_FILE` |

### Filters and scoring (step 6)

A stock is ranked only if it passes all of these, in order:

1. Kite lists it as `instrument_type == "EQ"` in segment `NSE` and its base
   symbol is in the NIFTY 500 list.
2. Enough daily candles: `max(MA_FILTER_100, LOOKBACK_R126 + 1, REG_LOOKBACK + 1)` rows.
3. Last close above the 100-day EMA.
4. 20-day average volume at least `MIN_VOLUME`.
5. ATR(20) no more than `MAX_ATR_PCT` of price.

Anything that throws inside the loop is skipped and logged at DEBUG, so a
symbol quietly disappearing from the ranking is normal. Bump `logging` to
DEBUG when you need to see why.

### Exit rules (step 7)

A holding is sold when any of these hold:

- it is not in the ranking at all (it failed a filter above),
- its percentile rank is worse than `CUT_OFF_PCT` (default top 20 %),
- its close is at or below its 100-day EMA,
- trailing stop: close is more than `EXIT_MULTIPLE` ATRs below the 40-day
  rolling maximum close.

Sold symbols go into the module-level `sold_symbols` set and are not bought
back in the same run. If the ranking is empty the prune step is skipped
entirely, as a guard against liquidating everything on a bad data day.

### Sizing (steps 9 and 11)

`target_shares` = `account_equity * RISK_FACTOR / ATR`, capped so no
position exceeds `MAX_WEIGHT` of equity. Equity is cash plus mark-to-market
value and is recomputed after every buy. When a buy is unaffordable the code
buys as many shares as the cash covers rather than skipping the name.

## 4. Design decisions you should not undo

**Every Kite call goes through `kite_call`.** It spaces calls to `KITE_RPS`
with jitter and retries `429 / too many requests` with exponential backoff.
Never call a `KiteConnect` method directly.

**Instrument tokens come from one `instruments()` download per run.**
`token_of` is a dict lookup with a single fallback scan. Do not reintroduce
`quote()` for token resolution; it was the main source of rate-limit errors.

**Candles are cached per instrument token in `CACHE_DIR/<token>.csv`.**
`candles_df` fetches incrementally and, on every update, re-downloads a
30-day overlap and compares the first overlapping close with the cache. A
mismatch means a split or bonus rewrote history, so the cache file is
deleted and the full history re-fetched. If you change the CSV schema, delete
the cache directory.

**Cash is reconstructed, not stored.** On every run,
`CASH_BAL = STARTING_CASH + sum(cash_ledger.amount) + sum(trades_ledger.cash_delta)`.
The two ledgers are the source of truth for cash, which means:

- never hand-edit `trades_ledger.csv`,
- to deposit or withdraw, append a row to `cash_ledger.csv` or set
  `ENV_CASHFLOW` for exactly one run. It appends a dated row every time the
  process starts with it set, so never leave it in `.env`,
- if you change `STARTING_CASH` you change the meaning of every historical row.

**Fees and slippage are booked at trade time.** `record_trade` debits
`price * (1 + FEES_PCT + SLIPPAGE_PCT)` on buys and credits
`price * (1 - FEES_PCT - SLIPPAGE_PCT)` on sells. The ledger is the model of
the account, not a broker statement.

**Order type depends on the NSE series.** Plain `EQ` names use market orders
priced from a batched `ltp()`. Series in `NO_MARKET_SERIES` (`BE`, `BZ`,
`BT`, ...) trade in a no-market-order segment, so they get limit orders at
the best bid or ask from a `quote()` depth call. `series_of` and
`base_symbol` do the parsing; both are unit tested.

**All scheduling is IST.** Weekday checks and ISO-week parity use
`ist_now()`. Candle dates are timezone-aware IST.

**Configuration is read at import time.** Every `os.getenv` sits at module
level, and importing the module also creates `CACHE_DIR` and configures
logging. Environment variables must be set before the first import; see
`tests/conftest.py` for how the tests do it.

**Module-level mutable state.** `CASH_BAL`, `sold_symbols`, `TOKEN_CACHE`,
`NIFTY500_SET` and `NIFTY_FULL_SET` are globals mutated during a run. They
are reset only by starting a new process. Do not call `main()` twice in one
interpreter.

## 5. Files and their lifecycle

| File | Written by | Read by | Notes |
| --- | --- | --- | --- |
| `current_portfolio.csv` | you | step 2 | `SYMBOL,QUANTITY`, no header |
| `next_portfolio.csv` | step 12 | you | Copy over `current_portfolio.csv` once fills are confirmed |
| `cash_ledger.csv` | you, or `ENV_CASHFLOW` | step 2 | `date,amount,note` |
| `trades_ledger.csv` | every `safe_buy` / `safe_sell` | step 2 | Append-only |
| `.cache_candles/<token>.csv` | `candles_df` | `candles_df` | `date,open,high,low,close,volume`; git-ignored, safe to delete |

The gap between step 12 and the next run's step 2 is deliberate: the script
assumes every order filled at the price it used. Confirming fills against the
broker and correcting `current_portfolio.csv` is a manual step today.

## 6. How we work

**Tooling.** uv owns the environment; `uv.lock` is committed and the
`uv-lock` pre-commit hook fails the commit if `pyproject.toml` and the lock
disagree. Use `uv add` / `uv add --dev` / `uv remove`, never `pip`.
`uv lock --upgrade && uv sync` refreshes within the bounds in
`pyproject.toml`. pandas is held below 3.0 on purpose: 3.x changes
copy-on-write and string-dtype defaults and the strategy math has not been
re-validated against it.

**Lint and format.** ruff, configured in `pyproject.toml`, 120-column lines,
rule sets E/W/F/I/UP/B/C4/SIM. `archive/` is excluded and must stay that way.
The pre-commit hooks run ruff on every commit; `uv run pre-commit run
--all-files` runs them by hand.

**Tests.** `tests/test_momentum.py` covers the pure helpers: symbol parsing,
portfolio CSV round-trips, fee arithmetic, ATR and the momentum score.
Nothing that takes a `KiteConnect` is tested. If you add such a test, pass a
small fake object exposing the methods you need, for example an `ltp`
method returning `{"NSE:TCS": {"last_price": 100.0}}`, and monkeypatch
`kite_call` or `candles_df` where the function goes to the network.

**Commits.** Small, one concern each, imperative subject, body says why.
Look at `git log` for the house style. Do not create `momentum_v5.py`; the
`archive/` folder exists because that used to be how versions were tracked,
and git now does that job.

**Changing strategy parameters.** Anything a user should be able to tune
belongs in the configuration block at the top of the module as an
`os.getenv` with a default, and in `.env.example` with the same default.
Anything that changes the meaning of past ledger rows (fees, starting cash)
deserves a note in the commit body.

## 7. Known rough edges

Things we know about and have not fixed. Good first tasks, in rough order of
value.

1. **Stale names and docstring for the lookbacks.** See section 1. Renaming
   the constants to `LOOKBACK_SHORT/MID/LONG` and fixing the
   `_composite_momentum` docstring is a safe first commit.
2. **`kite_call` returns `None` after exhausting retries.** The loop falls
   through instead of raising, so sustained rate limiting surfaces as an
   `AttributeError` on `.items()` somewhere downstream rather than a clear
   error at the call site.
3. **Fills are assumed.** Limit orders on `BE`/`BZ` names may not fill, but
   the ledger and the portfolio snapshot are updated as if they did.
4. **`candles_df` uses the machine's local date** (`datetime.now().date()`)
   for the end of the window while everything else uses IST. Identical on a
   machine set to IST, off by one day otherwise.
5. **`authenticate` needs a TTY.** The daily token handoff blocks unattended
   runs from cron or launchd.
6. **Broad `except Exception` in `rank_universe`** logs at DEBUG, so data
   problems for individual symbols are invisible at the default log level.

## 8. Glossary

- **ATR** Average True Range, here a simple 20-day mean of the true range. Used for sizing and the trailing stop.
- **EMA** Exponential moving average, `pandas.ewm(span=N, adjust=False)`.
- **ISO week parity** `ist_now().isocalendar().week % 2`; even weeks resize.
- **LTP** Last traded price, from Kite's batched `ltp()` endpoint.
- **Series** The NSE suffix on a tradingsymbol (`-BE`, `-BZ`). `EQ` is the normal rolling-settlement series and has no suffix.
- **tradingsymbol / instrument_token** Kite's human-readable name and numeric id for an instrument. Candles are keyed by token, orders by tradingsymbol.
- **CNC** Kite's cash-and-carry (delivery) product type. All orders here are CNC.
