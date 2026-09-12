# ADR-002: Modernise the toolchain around uv

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

Before this decision the project was:

- a `.venv` built on Python 3.9.6 from the Xcode Command Line Tools, managed
  with pip;
- a three-line `requirements.txt` with patch-level `~=` pins and no lockfile,
  so a fresh install could differ from the working one;
- five scripts at the repository root, `test_updated_v0.py` through `v4`,
  serving as manual version control, with v4 the live one;
- no formatter, linter, tests or git repository.

Two forces pushed for change. Python 3.9 was end of life and current numpy
requires 3.12 or newer. The owner wanted uv specifically. The `test_` prefix
on the scripts was also a latent problem: pytest collects any such file as a
test module.

## Decision

- **uv** manages the interpreter, the virtual environment and dependencies.
  `pyproject.toml` (PEP 621) declares the project; `uv.lock` is committed;
  `.python-version` pins 3.13 for development while `requires-python` is
  `>=3.12`.
- **Package layout.** The live script moves to
  `src/stocks_on_the_move/momentum.py`, unchanged in logic, with a
  `stocks-on-the-move` console script and a `__main__.py`. The build backend
  is `uv_build`. Earlier versions move to `archive/` and are never imported,
  linted or formatted.
- **Quality tooling.** ruff for lint and format (rule sets E, W, F, I, UP,
  B, C4, SIM; 120 columns), pytest for the pure helpers, pre-commit running
  ruff, `uv-lock` and whitespace hygiene on every commit.
- **Secrets and settings** come from a git-ignored `.env`, loaded with
  `uv run --env-file .env`, rather than adding a dotenv dependency to the
  code.
- The repository is initialised in git and hosted on GitHub.

## Consequences

### Positive

- One command (`uv sync`) reproduces the exact environment, Python included.
- The lockfile makes dependency drift visible; the pre-commit hook rejects
  a commit whose lock and `pyproject.toml` disagree.
- A real entry point instead of `python test_updated_v4.py`.
- Tests exist for the first time, and cannot accidentally collect the old
  scripts.

### Negative

- Contributors must install uv. It is one Homebrew formula, but it is a
  requirement pip was not.
- Python 3.9 machines can no longer run the project.
- ruff's modernisation pass rewrote type hints and reformatted
  `momentum.py`; the diff against v4 is around 190 lines of cosmetic churn,
  which makes `git blame` on those lines point at the migration.
- The `archive/` files were also rewritten once by the pre-commit hook
  before `archive/` was excluded from ruff, so they are no longer
  byte-identical to the originals. Logic and constants are intact.

### Neutral

- PyCharm keeps working because `.venv/bin/python` is the same path.
- Old `.claude/settings.local.json` permissions for `pip3 install` are now
  dead; `uv` commands prompt instead.

## Alternatives Considered

### Option 1: Status quo, pip and `requirements.txt`, optionally pip-tools

**Pros:**

- Nothing to learn.

**Cons:**

- No lockfile without a second tool; no interpreter management; slow.
- Leaves the `test_` collection problem and the flat layout in place.

### Option 2: Poetry

**Pros:**

- Mature, lockfile, widely known.

**Cons:**

- Slower resolver, its own lock format, and it does not manage Python
  installs. The owner asked for uv.

### Option 3: pyproject.toml only, keep the flat script at the root

**Pros:**

- Smallest diff.

**Cons:**

- No console script without a package; pytest still collects the scripts;
  the archive still sits beside the live code.

### Option 4: python-dotenv in the code for settings

**Pros:**

- Works without uv.

**Cons:**

- A new runtime dependency and import-time file reading, when `uv run`
  already loads env files.

## References

- `pyproject.toml`, `uv.lock`, `.python-version`, `.pre-commit-config.yaml`
- Commits 7c70cab through 1686f10 on `main` (2026-09-12)
- ADR-003 for the dependency bounds chosen inside this layout

## Implementation Status

Implemented in full on 2026-09-12.
