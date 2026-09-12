# ADR-015: LOG_LEVEL, logging configured at entry, and a log file per run

- **Status**: Accepted
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

`logging.basicConfig(level=logging.INFO, ...)` runs at import time in
`momentum.py`. There is no way to change the level without editing code,
and because it configures the root logger on import, any test or tool that
imports the module inherits that configuration.

The messages that explain surprises are below the fixed level. Every
symbol skipped inside `rank_universe` because of an exception, and every
`size calc error`, is logged at DEBUG and therefore never seen. Rate-limit
backoff sleeps in `kite_call` are silent.

Output goes to the terminal only. When a position looks wrong three weeks
later, the run that opened it left nothing behind but ledger rows.

ADR-006 defines a run directory and lists `run.log` among its contents,
deferring level, format and policy to this ADR. ADR-007 moves configuration
reads out of import time; logging configuration should move with them.

## Decision

- **Configure at entry.** `logging` is configured in `main()` (or the
  console-script entry), never at import. Importing
  `stocks_on_the_move.momentum` has no logging side effect.
- **Package logger, not root.** Configuration applies to the
  `stocks_on_the_move` logger hierarchy. The root logger is left at
  WARNING so `urllib3`, `kiteconnect` and friends do not flood the file at
  DEBUG.
- **`LOG_LEVEL`** (default `INFO`) sets the console handler's level. It is a
  field on the settings model (ADR-007), validated against the standard
  level names.
- **A file handler per run, always at DEBUG.** The console shows what
  `LOG_LEVEL` asks for; the file gets everything. Destination:
  - the run directory of ADR-006 (`runs/<date>/<time>-<mode>/run.log`) when
    that ADR is implemented;
  - until then, `LOG_DIR` (default `logs/`, git-ignored) with the same
    `<date>T<time>-<mode>.log` naming. When ADR-006 lands, `LOG_DIR` is
    retired and this ADR's Notes record it.
  - The handler is attached after the weekday guard, so aborted runs write
    no file. Login failures and everything after are captured.
- **Level policy**, applied in the same change:
  - WARNING: anything skipped or swallowed that a human should look at. The
    `except Exception` in `rank_universe` and the `size calc error` handlers
    move from DEBUG to WARNING and include the symbol and the exception
    type. Each backoff sleep in the broker layer logs at WARNING with the
    attempt number.
  - INFO: decisions and totals, as today.
  - DEBUG: per-call and per-symbol detail that is only useful with the file
    open.
  - Never any level: tokens, secrets, or the full contents of a Kite
    response.
- **Format** unchanged for the console. The file adds the logger name and
  the module and line: `%(asctime)s %(levelname)-8s %(name)s %(module)s:%(lineno)d %(message)s`.

## Consequences

### Positive

- The DEBUG trail exists for every run without making the console noisy;
  "why was X skipped" is a search in the run's log.
- Swallowed exceptions become visible at the default level, with enough
  context to act on.
- Importing the module in tests or tools no longer reconfigures logging.
- One `LOG_LEVEL=DEBUG` gives a verbose console for a live investigation.

### Negative

- Two handlers with different levels is a small thing to explain, and a
  common source of "why did that not print" confusion. Documented in
  `ONBOARDING.md`.
- The file at DEBUG for a full-universe run is on the order of a megabyte.
  On disk only, git-ignored; no retention policy, same stance as ADR-006.
- Promoting `rank_universe` skips to WARNING may produce dozens of lines on
  a bad data day. That is the intended signal; if it becomes routine noise,
  the underlying data problem is the thing to fix.

### Neutral

- Until ADR-006 lands there is a `logs/` directory; after, the same file
  lives in the run directory. Either way the naming is identical.
- `LOG_LEVEL` and `LOG_DIR` join `.env.example` through ADR-007's generator.

## Alternatives Considered

### Option 1: Status quo, INFO to the console at import

**Cons:**

- No file, no level control, hidden failures.

### Option 2: A single `LOG_LEVEL` applied to console and file alike

**Pros:**

- One level to understand.

**Cons:**

- Either the console is noisy at DEBUG or the file is useless at INFO. The
  question this ADR answers is "what happened three weeks ago", which needs
  DEBUG on disk regardless of what the operator wanted to watch.

### Option 3: Structured JSON logging

**Pros:**

- Machine-readable; tooling can filter by field.

**Cons:**

- Unreadable in a terminal without a viewer; the structured record of a
  run is ADR-006's job. Revisit if logs are ever shipped anywhere.

### Option 4: `loguru` or another logging library

**Cons:**

- A dependency to replace forty lines of standard-library configuration.

## Implementation Plan

1. **Configure at entry and level policy**, one commit: `logging_setup.py`
   with `configure_logging(settings, run_dir_or_log_dir)`; `basicConfig`
   removed from import; the WARNING promotions; `LOG_LEVEL` and `LOG_DIR` on
   the settings model. Depends on ADR-007 being Implemented, or lands with a
   temporary `os.getenv` for the two new variables if it precedes it.
2. **Docs**: `ONBOARDING.md` section 6 gains a paragraph on the two levels;
   `.gitignore` gains `logs/`.
3. **Validation before Implemented.** A paper run with `LOG_LEVEL=WARNING`:
   the console shows only warnings, the file contains DEBUG lines from
   `rank_universe`, and no line contains the API key.

## References

- `src/stocks_on_the_move/momentum.py`: `logging.basicConfig` (module top),
  `rank_universe` (`except Exception`), `kite_call`
- ADR-006 (run directory holds `run.log`), ADR-007 (settings fields),
  ADR-008 (backoff logging moves into the broker layer)
- `ONBOARDING.md`, rough edge 6

## Implementation Status

Not started.
