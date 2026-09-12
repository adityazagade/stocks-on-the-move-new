# ADR-007: Replace module-level os.getenv calls with a typed settings object

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

Configuration is thirty-odd `os.getenv` calls at the top of `momentum.py`,
each with its own cast. The casts disagree with each other:

```python
KILL_SWITCH = bool(int(os.getenv("KILL_SWITCH", "0")))  # "false" -> ValueError
allow_kite_execution = os.getenv("ALLOW_KITE_EXECUTION", "1").lower() not in ("0", "false", "no", "n", "off")
```

`KILL_SWITCH=false` crashes at import with a `ValueError`. `FORCE_RESIZE` and
`USE_FULL_NIFTY_UNIVERSE` share that parser. `ALLOW_KITE_EXECUTION` has the
opposite failure: any value outside its five recognised falsy strings is
treated as true, so `ALLOW_KITE_EXECUTION=maybe` or a stray trailing space
enables live order placement. A typo in the safety switch fails open.

Every value is read at import time. Importing the module also creates the
cache directory and configures root logging. Tests can only run because
`tests/conftest.py` sets environment variables before the first import, and
no test can construct two different configurations in one process.

`.env.example` is maintained by hand and already lags the code: it documents
`ALLOW_KITE_EXECUTION` with `0` and `1` only, and has no entry for the
`ACCOUNT_VALUE` fallback. ADR-005 and ADR-006 each add variables and will
widen the gap.

## Decision

Introduce `stocks_on_the_move/settings.py` with a single
`Settings(pydantic_settings.BaseSettings)` model, and remove every
`os.getenv` from `momentum.py`.

- **Same names, same defaults.** Every field uses today's environment
  variable name and today's default value, so a working `.env` keeps working.
  Derived values (`STARTING_CASH` falling back to `ACCOUNT_VALUE`, the
  minimum call interval from `KITE_RPS`, the lookback list) become model
  validators or properties.
- **One bool parser.** pydantic's: `1/0`, `true/false`, `yes/no`, `on/off`,
  `t/f`, `y/n`, case-insensitive. Anything else is a validation error at
  startup with the variable named. The safety switch now fails closed.
- **Ranges where a wrong value is silently dangerous.** `TRADING_WEEKDAY` in
  0..6, `MAX_WEIGHT` and `CUT_OFF_PCT` in (0, 1], `RISK_FACTOR`, `KITE_RPS`
  and `FEES_PCT` non-negative, `MAX_POSITIONS` and `ATR_PERIOD` positive.
  These reject values the current code would accept and misbehave on; that is
  the only intended behaviour change.
- **Secrets are `SecretStr`.** `KITE_API_KEY` and `KITE_API_SECRET` never
  appear in `repr`, logs or the settings snapshot ADR-006 writes to
  `run.json`. `model_dump()` with secrets masked is the snapshot.
- **No import-time reads.** `Settings()` is constructed once in `main()` and
  passed to the code that needs it. Until ADR-008 threads a context object
  through the pipeline, the interim is a module-level `SETTINGS` set by
  `main()` and read by functions; import must succeed with no environment at
  all. The cache-directory `mkdir` moves to first use; logging configuration
  moves to `main()` (ADR-015).
- **uv stays the only `.env` loader.** `env_file=None` in the model config.
  Two loaders with different precedence rules is a bug class we do not need.
- **`.env.example` is generated.** `uv run python -m stocks_on_the_move.settings --example`
  renders every field with its description and default. A test asserts the
  committed file equals the generated text, so the two cannot drift.

## Consequences

### Positive

- `KILL_SWITCH=false` works; `ALLOW_KITE_EXECUTION=maybe` refuses to start.
- One place to read to learn every knob, with types and ranges; the example
  file is derived from it, not remembered.
- Tests build a `Settings(...)` with explicit values instead of poking the
  environment before import. Two configurations in one process become
  possible, which the golden test (ADR-009) needs.
- Credential redaction comes from the type system instead of string
  filtering.

### Negative

- A new runtime dependency, `pydantic-settings`, which pulls `pydantic` and
  its compiled core. The project has otherwise stayed on the standard
  library for infrastructure.
- Every function that reads a module constant today changes to read from
  the settings object. Mechanical, but it touches most of the file and lands
  before ADR-008 touches it again.
- Startup now fails on configurations that used to run. That is the point,
  but the first run after upgrading may surface a value nobody knew was junk.

### Neutral

- Variable names and defaults are unchanged; `.env.example` gains
  descriptions and loses hand-written comments.
- The `look_backs` list and `_MIN_INTERVAL` stop being module globals.

## Alternatives Considered

### Option 1: Status quo, `os.getenv` per constant

**Pros:**

- No dependency; everyone understands it.

**Cons:**

- The inconsistent parsers and the fail-open safety switch are the bug.
  Import-time reads block testing.

### Option 2: A `@dataclass` with a hand-written `from_env()`

**Pros:**

- No dependency; the same single-construction benefit.

**Cons:**

- The parsers are hand-written again, which is the class of code that
  produced the inconsistency. No secret type, no range validators, no schema
  to generate the example from without writing that too. Roughly the same
  line count as the model, with none of the guarantees.

### Option 3: A `.toml` or `.yaml` configuration file instead of environment variables

**Pros:**

- Structured, commentable, one file.

**Cons:**

- Breaks every existing `.env`, PyCharm run configuration and the
  `uv run --env-file` workflow for no gain the model does not already give.

## Implementation Plan

1. **Model and example generator**, one commit. `settings.py`, the `--example`
   renderer, the drift test, `pydantic-settings` added with `uv add`.
   `.env.example` regenerated and committed from the generator.
2. **Cut-over**, one commit. `momentum.py` reads from the settings object;
   `conftest.py` stops setting environment variables. All existing tests pass
   unchanged in behaviour.
3. **Validation before Implemented.** A paper run with the owner's actual
   `.env`. Then three deliberate misconfigurations, `KILL_SWITCH=false`,
   `ALLOW_KITE_EXECUTION=maybe`, `TRADING_WEEKDAY=7`: the first must run, the
   other two must refuse to start with the variable named in the error.

## References

- `src/stocks_on_the_move/momentum.py`, configuration block (lines 47 to 131)
- `tests/conftest.py`
- ADR-005 and ADR-006 (add variables that should be born in the model)
- ADR-008 (threads the settings object through the pipeline)
- ADR-015 (moves logging configuration out of import)
- pydantic-settings documentation: https://docs.pydantic.dev/latest/concepts/pydantic_settings/

## Implementation Status

Implemented on 2026-09-12 by the owner's decision. Plan step 3's paper run
with the owner's `.env` was waived; the two must-refuse cases and the
must-run case were exercised against the entry point, and `--check` passes
on the owner's `.env`. The code is merged and CI is green on `main`.

Code complete on 2026-09-12; awaiting plan step 3 by the owner.

- Step 1 landed as `src/stocks_on_the_move/settings.py` with `render_example()`,
  the `--example` and `--check` commands, `tests/test_settings.py` and a
  regenerated `.env.example`. `pydantic-settings>=2.15,<3` added with `uv add`
  (resolves pydantic 2.13.5, pydantic-settings 2.15.0).
- Step 2 landed as the cut-over: `momentum.py` has no `os.getenv`, no
  import-time `mkdir` and no import-time logging configuration; `kite_auth.py`
  lost its own environment reads and receives an `AuthSettings` built from
  `Settings`; `tests/conftest.py` sets no environment variables and instead
  installs `Settings.from_values(...)` per test.
- Step 3 is outstanding: a paper run with the owner's `.env`, then
  `KILL_SWITCH=false` (must run), `ALLOW_KITE_EXECUTION=maybe` and
  `TRADING_WEEKDAY=7` (must refuse with the variable named). The last two
  were exercised locally against the entry point; the paper run needs Kite.
  Status moves to Implemented after that.

## Notes

Refinements made while implementing, all inside the decision above:

- **Credentials are required, not defaulted.** The old placeholders
  `YOUR_API_KEY` / `YOUR_API_SECRET` never produced a working run, so
  `KITE_API_KEY` and `KITE_API_SECRET` are required and non-empty, and a
  missing one is reported at startup like any other bad value.
- **Ranges beyond the listed ones.** In the same spirit as the list in the
  Decision: `SLIPPAGE_PCT`, `MIN_VOLUME`, `CANDLE_SLEEP_SEC`, `ACCOUNT_VALUE`
  and `STARTING_CASH` non-negative; `EXIT_MULTIPLE` and `MAX_ATR_PCT`
  positive; `KITE_MAX_RETRIES` at least 1 (zero made `kite_call` return
  `None` without calling); `KITE_REDIRECT_PORT` in 0..65535.
- **Whitespace is stripped before parsing**, so `ALLOW_KITE_EXECUTION=0 `
  means off rather than failing; junk still refuses to start.
- **`Settings.from_values(**kw)`** builds from explicit values with the
  environment source removed, so tests and the golden test (ADR-009) are
  deterministic whatever the developer's shell holds.
- **`--check`** prints the loaded configuration with secrets masked, or the
  error report and exit code 2. It exists so plan step 3 can be run on any
  weekday without reaching Kite.
- **The interim handle** is `momentum.SETTINGS`, a placeholder object until
  `configure()` runs; any read before that raises a `RuntimeError` naming
  the fix instead of an `AttributeError`.
- **`look_backs` is gone**; its only reader now uses `LOOKBACK_R126`
  directly, which is the same value.
- pydantic reports a derived default it could not compute as its own error
  (`default_factory_not_called`); `describe_errors` drops that line because
  the `ACCOUNT_VALUE` line already says what is wrong.
