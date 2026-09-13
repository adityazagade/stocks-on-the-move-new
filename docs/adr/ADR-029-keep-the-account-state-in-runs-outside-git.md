# ADR-029: Keep the account state at the root of `runs/`, outside git

- **Status**: Implemented
- **Date**: 2026-09-13
- **Last Updated**: 2026-09-13
- **Author**: Aditya Zagade

## Context

Five files are the whole state of the account: `current_portfolio.csv`,
`next_portfolio.csv`, `cash_ledger.csv`, `trades_ledger.csv` and, since
ADR-027, `strategy_state.json`. ADR-004 tracked the first four in git for
history, diff-after-run and backup, on the stated assumption that the
repository was private, and named the escape hatch: "if the repository
ever becomes public or is shared beyond the owner, flipping those lines and
rewriting history is the first step". ADR-006 turned the same reasoning
into a rule, "state the code reads to run is versioned; diagnostics it
produces are not", and put everything the code produces under `runs/`,
which is git-ignored.

The repository is public. The owner does not want files that every
execution rewrites committed to it, and asked for them to live at the root
of `runs/`, beside the per-run directories. What is in the public history
today is harmless: the five tracked files are headers only, 0 to 66 bytes,
one commit each, and the state file holds a single date. No account data
has been published, so the history stays as it is.

The "starting point before the first execution" needs no committed seed.
`read_portfolio` returns no positions when its file is missing, the two
ledgers are created with headers on first use, cash reconstructs as
`STARTING_CASH`, and an absent `strategy_state.json` makes the first size
rebalance due. The one real seed is existing holdings, which the operator
writes into the portfolio file once. What is missing is only that nothing
creates the directory those files are written to.

## Decision

- **The five state files live at the root of `RUNS_DIR`** (default `runs/`):
  `runs/current_portfolio.csv`, `runs/next_portfolio.csv`,
  `runs/cash_ledger.csv`, `runs/trades_ledger.csv`,
  `runs/strategy_state.json`. Each of the five settings keeps its name and
  stays overridable; its default is derived from the validated `RUNS_DIR`,
  so `RUNS_DIR=/tmp/sotm` moves the state files and the run directories
  together, and `TRADES_LEDGER_FILE=...` alone still moves one file.
- **The ledger writers create the directory** they write into, so a fresh
  checkout's first run works with no `runs/` present: it starts from
  `STARTING_CASH`, no positions, a due rebalance, and leaves the five files
  behind. Plan mode (ADR-022) creates nothing but its run directory.
- **Nothing under `runs/` is versioned.** The five root files leave the
  index with `git rm --cached`; the working copies move into `runs/`;
  `.gitignore` says why and carries root-anchored guards for the five names
  in case a setting is pointed back at the checkout root. The past commits
  stay: they hold headers and one date.
- **Backup becomes an operator habit, not code.** With this ADR the five
  files are the account's only copy, and cash is reconstructed from them
  with no broker reconciliation. ADR-004's audit trail and `git revert` are
  given up in the public repository. The operator keeps the checkout, or
  `RUNS_DIR`, on a volume Time Machine or a sync client covers; to keep a
  per-run diff and revert, `git init` inside `runs/` with a private remote
  and commit the five root files after each weekly run. The outer
  repository ignores `runs/` whole, so a nested repository is invisible to
  it. ADR-004's single-machine assumption stands.
- **The cut-over is one commit.** The code that reads from `runs/` and the
  move of the working copies land together, and the operator confirms
  `ls runs/` shows the five files before the next weekly run. If the code
  landed with the files still at the root, the next run would bootstrap an
  empty account under `runs/`, reconstruct cash as `STARTING_CASH` with no
  positions, and buy a whole portfolio again.
- **`CLAUDE.md` rule 4** becomes: `runs/` is the account, the four ledgers
  and `strategy_state.json` at its root and the run diagnostics beneath;
  none of it is versioned; the repository is code only; the owner backs
  `runs/` up outside git. Rule 5's scratch-path advice becomes "point
  `RUNS_DIR` at a scratch directory". ADR-004 is Superseded; ADR-006's rule
  loses its first half and its Notes say so; ADR-027's Notes record that
  its fifth file moved.

## Consequences

### Positive

- The public repository holds code, decisions and synthetic fixtures, and
  nothing about the account.
- One setting, `RUNS_DIR`, moves everything a run touches; the paper-run
  recipe collapses to two lines and no `mkdir`.
- A fresh checkout runs without ceremony and leaves its state where the
  operator expects it.

### Negative

- The account's book of record is no longer backed up or diffed by the
  repository. The habit above replaces it; nothing enforces the habit.
- ADR-004's `git log` on the trades ledger is gone unless `runs/` becomes
  its own private repository.
- A move of live state files, done by hand once, with a hazard if the code
  and the files were to land apart.

### Neutral

- The seeded `strategy_state.json` of ADR-027 moves with the rest; the
  2026-09-16 run still rebalances as parity would have.
- The golden test, the backtest harness and the pipeline tests pass
  explicit paths and do not move.

## Alternatives Considered

### Option 1: Status quo, the five files tracked at the root (ADR-004)

**Pros:**

- History, a diff after every run, and a backup, for free.

**Cons:**

- ADR-004 assumed a private repository; the repository is public, and the
  owner does not want execution-rewritten files in it.

### Option 2: Keep them tracked, encrypted with git-crypt

**Pros:**

- History and backup stay in the same remote.

**Cons:**

- A key to manage, a tool to install on every machine, and encrypted
  blobs in a public history. ADR-004 named it as the fallback only for a
  repository that must stay shared.

### Option 3: A separate private state repository or submodule

**Pros:**

- Clean separation, with history.

**Cons:**

- Two repositories to keep in step for one person. It is available as the
  backup habit above, inside `runs/`, without becoming the design.

### Option 4: Rewrite history to remove the tracked files

**Cons:**

- Nothing to remove: headers and one date. A force-push and re-clone for no
  gain. Declined by the owner.

## Implementation Plan

1. **This ADR**, one commit, Proposed. Accepted by the owner's approval of
   the plan on 2026-09-13.
2. **One commit** for everything else: the five settings deriving their
   defaults from `RUNS_DIR`, with `.env.example` regenerated to show them;
   the ledger writers creating their directory; `git rm --cached` of the
   five root files and their move into `runs/`; `.gitignore`; `CLAUDE.md`
   rules 4, 5 and 7; `ONBOARDING.md` sections 1, 2, 4 and 5 and the
   glossary; `README.md`; ADR-004 Superseded, ADR-006 and ADR-027 Notes;
   tests for the derived defaults, the overrides, the environment, the
   first run on an empty `RUNS_DIR` in live-paper and plan mode; this ADR
   set to Implemented.
3. **Validation before Implemented.** The suite green with the golden files
   untouched; `settings --check` shows the five paths under `runs/`;
   `git ls-files` shows none of the five; `ls runs/` shows all five with the
   seeded state file intact; a paper run against `RUNS_DIR=/tmp/sotm-fresh`
   creates the ledgers and starts from `STARTING_CASH`; a plan run there
   leaves only its run directory.

## References

- ADR-004 (superseded), ADR-006 (the rule this ADR halves), ADR-007 (the
  settings), ADR-014 (`CLAUDE.md` rules 4 and 5 change here), ADR-022 (plan
  mode writes nothing), ADR-027 (the fifth file)
- `src/stocks_on_the_move/settings.py`, `src/stocks_on_the_move/ledger.py`,
  `.gitignore`

## Implementation Status

Implemented on 2026-09-13, two commits, golden expected files untouched.

- `settings.py`: `runs_dir` is declared first in the Files section; the
  five state-file fields keep their names and `str` type and take their
  default from a `default_factory` over the validated `runs_dir`, the idiom
  `starting_cash` already used; the example file shows `runs/<name>` for
  each. `RUNS_DIR=/tmp/sotm` moves all five; any one `*_FILE` still wins.
- `ledger.py`: `_ensure_parent` creates the directory in `write_portfolio`,
  `_ensure_csv` and `save_state`. Readers are unchanged; a missing file was
  already an empty account.
- The five root files left the index and moved into `runs/`; `.gitignore`
  explains `runs/` and anchors the five names at the root.
- Docs: `CLAUDE.md` rules 4, 5 and 7; `ONBOARDING.md` sections 1, 2, 4, 5
  and the glossary; `README.md`. ADR-004 Superseded; ADR-006 and ADR-027
  Notes.
- Tests: defaults under `runs/` and following `runs_dir`, including `~`;
  an explicit path wins while the others follow; `RUNS_DIR` in the
  environment moves them and a `*_FILE` variable beats it; the first
  paper run on an absent `RUNS_DIR` creates the ledgers with headers, the
  state file and the snapshot and starts from `STARTING_CASH`; a plan run
  there leaves only its run directory. The fixtures keep explicit paths.
