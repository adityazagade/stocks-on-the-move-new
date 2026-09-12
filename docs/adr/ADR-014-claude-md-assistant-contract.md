# ADR-014: A CLAUDE.md contract for coding assistants

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

Much of the work on this repository since 2026-09-12 has been done with
Claude Code. The rules it follows here, ADR-first development (ADR-001),
uv-only dependency management, "commit only when asked", the fact that paper
mode still writes the ledgers, live in two places: the assistant's per-machine
memory under the owner's user account, and prose spread across
`ONBOARDING.md` and the ADRs.

The memory does not travel. A session on another machine, another user
account, or after the memory is cleared starts with none of it, and the
first thing it is likely to do is `pip install` something or edit
`momentum.py` without an ADR. Claude Code reads a `CLAUDE.md` at the
repository root automatically at the start of every session, on every
machine, from the checkout itself.

## Decision

Commit a `CLAUDE.md` at the repository root as the assistant's contract for
this project. It is short, imperative, and points to the documents that hold
the detail rather than repeating them.

**Contents**, in this order, under about eighty lines:

1. One paragraph: what the project is and that it moves money.
2. **Non-negotiable rules**:
   - Every change beyond a typo starts with a Proposed ADR in `docs/adr/`;
     implement nothing until the owner marks it Accepted (ADR-001).
   - Dependencies only through `uv add`, `uv remove`, `uv lock`; never pip,
     never a requirements file (ADR-002).
   - pandas stays below 3.0 until ADR-003's validation has run.
   - Never commit `.env`, a Kite token, or anything under `runs/` (ADR-004,
     ADR-005, ADR-006).
   - Paper mode (`ALLOW_KITE_EXECUTION=0`) still appends to the trades
     ledger and rewrites `next_portfolio.csv`; point the ledger paths at
     scratch files before any test run.
   - `archive/` is frozen: never edit, lint, format or import it.
   - Never hand-edit `trades_ledger.csv`; cash is reconstructed from it.
   - Commit and push only when the owner asks. One ADR per commit; name the
     ADR in every implementation commit.
3. **Commands**: `uv sync`, `uv run pytest`, `uv run ruff check --fix .`,
   `uv run ruff format .`, `uv run pre-commit run --all-files`, and the
   paper-run recipe from `ONBOARDING.md` section 2.
4. **Where to read next**: `ONBOARDING.md` for how the pipeline works,
   `docs/adr/README.md` for every decision and its status, `.env.example`
   for every setting.

**Maintenance rule.** Any ADR that changes a rule listed in `CLAUDE.md`
updates `CLAUDE.md` in the same commit. The file is a summary, never the
source; if `CLAUDE.md` and an Accepted ADR disagree, the ADR wins and the
file is fixed.

`.claude/settings.local.json` stays personal and git-ignored; this ADR does
not add a shared `.claude/settings.json`.

## Consequences

### Positive

- The ADR-first rule and the safety rules reach every assistant session on
  every machine, from the checkout, with no dependence on local memory.
- A human contributor gets the same eighty-line summary; it doubles as the
  shortest possible onboarding.
- The rule most likely to cause harm if forgotten, paper mode writing the
  real ledgers, is stated where it will be read before the first command.

### Negative

- One more document that can go stale. The maintenance rule is a process
  rule, enforced by review, not by tooling.
- Some duplication with `ONBOARDING.md` section 6. Kept deliberately
  minimal: rules and pointers, no explanation.
- The filename is tool-specific. Other assistants read `AGENTS.md`; see
  Option 2.

### Neutral

- The assistant's local memory remains useful for preferences that are not
  repository rules; the two do not conflict.

## Alternatives Considered

### Option 1: Status quo, local assistant memory plus ONBOARDING.md

**Pros:**

- Nothing to maintain in the repository.

**Cons:**

- Memory is per machine and per user; the onboarding guide is long and
  written for humans, and an assistant may not read it before acting.

### Option 2: `AGENTS.md` as the canonical file, `CLAUDE.md` importing it

**Pros:**

- Vendor-neutral name read by several assistants.

**Cons:**

- The only assistant in use is Claude Code; a second file today is
  speculation. If another tool is adopted, add `AGENTS.md` containing the
  same rules and make `CLAUDE.md` a one-line pointer to it, by an ADR that
  supersedes this one.

### Option 3: Put the rules in `README.md`

**Cons:**

- The README is for someone deciding whether and how to run the project;
  imperative rules for an automated contributor would clutter it, and Claude
  Code does not load it automatically.

## Implementation Plan

1. **Write and commit** `CLAUDE.md`, one commit, alongside a one-line
   pointer in `ONBOARDING.md` section 6.
2. **Validation before Implemented.** Start a fresh Claude Code session in
   the checkout with memory disabled or on another machine and ask it to
   "add a feature"; it must respond by drafting an ADR rather than editing
   code.

## References

- ADR-001 (the rule that most needs to travel), ADR-002, ADR-003, ADR-004,
  ADR-005, ADR-006 (rules summarised)
- `ONBOARDING.md` sections 2 and 6

## Implementation Status

Implemented on 2026-09-12 by the owner's decision. Plan step 2 (the
fresh-session check) was waived; `CLAUDE.md` is merged on `main`.

Code complete on 2026-09-12; awaiting plan step 2 by the owner.

- `CLAUDE.md` is committed at the repository root, 71 lines: one paragraph on
  what the project is and that it moves money; ten non-negotiable rules; the
  commands; where to read next; the maintenance rule. `ONBOARDING.md`
  section 6 points to it.
- The rule list is the Decision's eight plus two that later ADRs made
  rules: never log or print a credential (ADR-005, ADR-012), and strategy
  code holds no module state and reaches the broker only through the
  `Broker` protocol, tested against the fake (ADR-008). The pandas rule
  names the golden test as the evidence the bound moves on (ADR-009) and
  says not to merge a bot pull request that widens it, after Dependabot's
  first run did exactly that (ADR-013, #13). The commands include the type
  check (ADR-010), the golden-file update flag (ADR-009) and the settings
  check (ADR-007), which did not exist when the ADR was drafted.
- Plan step 2 is the owner's: a fresh Claude Code session, memory disabled
  or on another machine, asked to "add a feature", must answer with a
  Proposed ADR rather than code. Status moves to Implemented after that.

## Notes

The assistant's local memory for this project already held the ADR-first
rule and the uv preference; `CLAUDE.md` now carries both from the checkout,
so a session that has neither still gets them.
