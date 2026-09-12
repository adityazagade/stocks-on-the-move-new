# CLAUDE.md

This repository is a weekly momentum-rotation strategy for NSE equities after
Andreas Clenow's *Stocks on the Move*, trading a real Zerodha account through
Kite Connect. A wrong change here moves real money. The rules below are the
contract for coding assistants working in this checkout; the detail lives in
the documents they point to. This file is a summary and never the source: if
it disagrees with an Accepted ADR, the ADR wins and this file is fixed.

## Non-negotiable rules

1. **Every change beyond a typo starts with an ADR.** Draft it as Proposed in
   `docs/adr/` from `template.md`, add it to `docs/adr/README.md`, commit it
   on its own as `ADR-NNN: <title>`, and implement nothing until the owner
   marks it Accepted (ADR-001). Typo, comment and doc-wording fixes are the
   only exemptions, and they must not change behaviour.
2. **Dependencies only through uv**: `uv add`, `uv remove`, `uv lock`,
   `uv sync`. Never `pip`, never a requirements file. Bounds are
   `>=minimum,<next-major` (ADR-002, ADR-003).
3. **pandas stays below 3.0** until an ADR moves the bound on the strength of
   the golden test (ADR-003, ADR-009). Do not merge a bot PR that widens it.
4. **Never commit `.env`, a Kite access token, or anything under `runs/`.**
   The rule: state the code reads to run (the four ledgers) is versioned;
   diagnostics it produces are not (ADR-004, ADR-005, ADR-006).
5. **Paper mode writes real files.** `ALLOW_KITE_EXECUTION=0` still appends
   to the trades ledger and rewrites `next_portfolio.csv`. Point
   `PORTFOLIO_FILE`, `OUT_FILE`, `CASH_LEDGER_FILE` and `TRADES_LEDGER_FILE`
   at scratch paths before any test run; `ONBOARDING.md` section 2 has the
   recipe.
6. **`archive/` is frozen.** Never edit, lint, format or import it.
7. **Never hand-edit `trades_ledger.csv`.** Cash is reconstructed from it on
   every run; edit it and every later number is wrong.
8. **Commit and push only when the owner asks.** One ADR per commit; name the
   ADR in the subject or body of every implementation commit; the final
   implementation commit sets the ADR to Implemented and updates the index.
9. **Never log or print a credential**: not the API key, not the secret, not
   a request or access token, not the contents of a Kite response.
10. **Strategy code holds no module-level mutable state and touches the
    broker only through the `Broker` protocol** (ADR-008). New behaviour is
    tested against `tests/fakes.FakeBroker`, never against Kite.

## Commands

```sh
uv sync                                   # environment, Python 3.13, all groups
uv run pytest                             # the whole suite incl. the golden test (ADR-009)
uv run pytest --update-golden             # only for an intended behaviour change; review the diff
uv run ruff check --fix . && uv run ruff format .
uv run ty check                           # type checking, blocking (ADR-010)
uv run pre-commit run --all-files         # everything CI runs (ADR-011), secret scan included
uv run --env-file .env python -m stocks_on_the_move.settings --check   # the configuration a run sees
```

The paper-run recipe with scratch ledgers is in `ONBOARDING.md` section 2.
Every push to `main` and every pull request runs the hooks and the tests on
Python 3.12 and 3.13; a red run is a signal to fix, not something to work
around.

## Where to read next

- `ONBOARDING.md`: how the pipeline works step by step, the design decisions
  you should not undo, the file lifecycle, the known rough edges.
- `docs/adr/README.md`: every decision, its status and its alternatives.
- `.env.example`: every setting, generated from `settings.py`; regenerate it
  with `uv run python -m stocks_on_the_move.settings --example`.

## Maintenance

Any ADR that changes a rule listed above updates this file in the same
commit (ADR-014). Keep it under about eighty lines: rules and pointers, not
explanations.
