# Stocks on the Move (NSE)

[![CI](https://github.com/adityazagade/stocks-on-the-move-new/actions/workflows/ci.yml/badge.svg)](https://github.com/adityazagade/stocks-on-the-move-new/actions/workflows/ci.yml)

Weekly momentum-rotation portfolio for NSE equities, adapted from Andreas
Clenow's *Stocks on the Move*. The strategy ranks the NIFTY 500 by
risk-adjusted momentum, gates new entries on the index trend, sizes positions
by ATR, trades through Zerodha Kite Connect, and keeps CSV ledgers of the
portfolio, cash and trades.

> Research code, run with care. Fills are confirmed against the broker before
> they are booked (ADR-019); everything else about real-money use is on you.

## Requirements

- [uv](https://docs.astral.sh/uv/) (`brew install uv`). It provisions the pinned
  Python from `.python-version` and every dependency. Nothing else to install.
- A Zerodha Kite Connect API key and secret.

## Setup

```sh
uv sync                 # creates .venv (Python 3.13) with runtime + dev deps
cp .env.example .env    # fill in KITE_API_KEY and KITE_API_SECRET
```

PyCharm: point the project interpreter at `.venv/bin/python`.

## Running

```sh
uv run --env-file .env stocks-on-the-move
```

`uv run python -m stocks_on_the_move` is equivalent. A run only proceeds on
the configured trading weekday (`TRADING_WEEKDAY`, default Wednesday, IST) and
exits immediately otherwise. The first run of the day prints a Kite login URL,
opens it in your browser and waits for Kite to redirect to
`http://127.0.0.1:8765/`, which must be the redirect URL of your app in the
Kite developer console; if it is not, paste the request token or the
redirected URL at the prompt. The session is cached under
`~/.config/stocks-on-the-move/` until 06:00 IST, so later runs the same day
log in silently (ADR-005). Configuration comes from the environment and is
validated at startup; a bad value stops the run with the variable named
(ADR-007).

Useful switches (the full list, with defaults, is in `.env.example`):

| Variable | Effect |
| --- | --- |
| `ALLOW_KITE_EXECUTION=0` | Paper mode: runs the whole pipeline and writes ledgers, sends no orders |
| `KILL_SWITCH=1` | Liquidate everything and exit, ignoring the weekday guard |
| `FORCE_RESIZE=1` | Force the position-size rebalance regardless of the fortnightly schedule |
| `ENV_CASHFLOW=<amount>` | Record a deposit (+) or withdrawal (-) before trading |
| `KITE_FORGET_SESSION=1` | Discard the cached Kite session and log in afresh |
| `LOG_LEVEL=DEBUG` | Verbose console; the run's `run.log` under `runs/` is always at DEBUG |

## Files

| Path | Purpose |
| --- | --- |
| `current_portfolio.csv` | Positions going into the run (`SYMBOL,QUANTITY`, no header) |
| `next_portfolio.csv` | Positions after the run; promote to `current_portfolio.csv` before the next one |
| `cash_ledger.csv` | Deposits and withdrawals |
| `trades_ledger.csv` | Every placed or paper trade with fees and slippage |
| `.cache_candles/` | Incremental daily-candle cache per instrument token (git-ignored) |
| `runs/<date>/<time>-<mode>/` | What each run decided: ranking, exits, sizes, candidates, trades, log, `run.json` (git-ignored; `runs/latest` is the newest) |

## Development

```sh
uv run pytest                 # unit tests, whole runs against a fake broker, the golden test
uv run pytest --update-golden # rewrite tests/fixtures/golden/expected/ after an intended change (ADR-009)
uv run ruff check --fix .     # lint: pyflakes, isort, pyupgrade, bugbear, ...
uv run ruff format .          # format
uv run ty check               # type-check src/ and tests/ (ADR-010)
uv run pre-commit install     # run the above automatically on every commit
uv run python -m stocks_on_the_move.settings --check    # the configuration a run would see
uv run python -m stocks_on_the_move.settings --example > .env.example   # after adding a setting
```

Dependency changes go through uv so that `uv.lock` stays authoritative:

```sh
uv add <package>              # runtime dependency
uv add --dev <package>        # dev-only dependency
uv lock --upgrade && uv sync  # refresh everything within the pyproject bounds
```

Dependabot opens one grouped pull request per ecosystem every Monday
(`.github/dependabot.yml`, ADR-013); merging is by hand with CI green.

Every change beyond a typo starts with an Architecture Decision Record in
`docs/adr/`. ADR-001 describes the process.

GitHub Actions runs the same pre-commit hooks and the test suite on Python
3.12 and 3.13 for every push to `main` and every pull request
(`.github/workflows/ci.yml`, ADR-011).

## Layout

```
src/stocks_on_the_move/
  momentum.py     entry point: settings, weekday guard, login, RunContext, pipeline.run
  pipeline.py     the twelve weekly steps and run(ctx) (ADR-020)
  rules.py        regime, filter chain and ranking, exit rules, ATR sizing
  indicators.py   strategy constants and the pure computations on prices
  execution.py    prices, order placement, the wait for a fill, booking (ADR-019)
  universe.py     the symbols the strategy may hold
  ledger.py       the portfolio snapshot and the two ledgers (ADR-004)
  reporting.py    the artifact tables' columns and row builders
  context.py      RunContext, Portfolio, Fill, the token cache
  broker.py       Broker protocol, KiteBroker with backoff, PaperBroker (ADR-008)
  candles.py      per-instrument candle cache with self-correcting fetches (ADR-008)
  artifacts.py    the per-run directory under runs/: tables, run.json, run.log (ADR-006)
  settings.py     every environment knob, validated once at startup (ADR-007)
  kite_auth.py    Kite login: session cache, redirect listener, paste (ADR-005)
  __main__.py     python -m entry point
tests/            pytest suite
docs/adr/         architecture decision records
archive/          earlier iterations v0 to v3, kept for reference, not installed
```

## License

Apache 2.0. See `LICENSE`.
