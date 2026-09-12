# ADR-011: Continuous integration on GitHub Actions

- **Status**: Accepted
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

The only quality gate is the local pre-commit hook. `git commit --no-verify`
skips it, a fresh clone has not run `pre-commit install`, and a hook that
is never exercised elsewhere can be misconfigured for weeks without anyone
noticing. The lockfile is checked only by that hook.

`requires-python` promises 3.12 and newer; every test so far has run on the
3.13 interpreter pinned in `.python-version`. Nothing has ever imported the
package on 3.12.

The repository is private on GitHub. GitHub-hosted runners are available to
private repositories with a monthly minute allowance that a two-minute
workflow on a weekly cadence will not approach.

ADR-013 (automated dependency pull requests) is pointless without a green or
red signal on each pull request. ADR-010 and ADR-012 add hooks that should
run somewhere other than the author's machine.

## Decision

Add `.github/workflows/ci.yml`, triggered on every push to `main` and every
pull request, with two jobs.

- **`checks`**: one runner, Python 3.13. Installs uv with
  `astral-sh/setup-uv` pinned to its current major (`v10` at the time of
  writing, the action's README recommends pinning by commit SHA and that is
  acceptable too), runs `uv sync --locked --all-groups`, then
  `uv run pre-commit run --all-files`. Running the hooks themselves, rather
  than re-listing ruff and friends in YAML, means CI and the local gate
  cannot drift; anything added to `.pre-commit-config.yaml` (ADR-010,
  ADR-012) is in CI by construction.
- **`tests`**: a matrix over Python 3.12 and 3.13. `uv python install` for
  the matrix version, `uv sync --locked --all-groups`, `uv run pytest`.
  `--locked` fails the job if `uv.lock` is out of date with
  `pyproject.toml`, which is the lockfile's second enforcer.
- `permissions: contents: read`. A `concurrency` group cancels superseded
  runs on the same ref.
- uv's cache is enabled through the action's default (`enable-cache: auto`).
- No branch protection. The owner pushes to `main` directly; the workflow
  reports, it does not block. If ADR-013 lands, its pull requests are merged
  only when this workflow is green, which is a rule of ADR-013.
- A status badge in `README.md`.

Nothing in CI touches the broker, the ledgers or the candle cache. The
workflow needs no secrets.

## Consequences

### Positive

- A second, independent run of every quality check on every push, on a
  clean machine.
- The 3.12 promise is tested.
- Dependency pull requests (ADR-013) get a verdict without a human running
  anything.
- A misconfigured local hook shows up as a red run instead of silence.

### Negative

- A push that fails CI is already on `main`; without branch protection the
  signal is after the fact. Acceptable for a single maintainer; revisit if a
  second contributor arrives.
- One more pinned external dependency, the action, to bump (ADR-013 covers
  `github-actions` as an ecosystem).
- Runner minutes are consumed on a private repository. At a few minutes per
  push this is far inside the free allowance.

### Neutral

- The workflow duplicates the local hooks by design. Duplication is the
  feature.
- `.github/` is a new top-level directory.

## Alternatives Considered

### Option 1: Status quo, local pre-commit only

**Pros:**

- Nothing to configure.

**Cons:**

- Skippable, unverified elsewhere, single interpreter.

### Option 2: List ruff, pytest and uv commands directly in the workflow

**Pros:**

- Slightly faster; no pre-commit bootstrapping in CI.

**Cons:**

- Two lists of checks that drift. Every new hook needs a matching YAML edit.

### Option 3: pre-commit.ci

**Pros:**

- Runs hooks and auto-fixes on pull requests with no YAML.

**Cons:**

- Free tier is for public repositories only; runs hooks but not the test
  matrix.

### Option 4: A self-hosted runner or another CI provider

**Cons:**

- Infrastructure to maintain for a solo project already on GitHub.

## Implementation Plan

1. **Workflow**, one commit: `ci.yml`, badge in README.
2. **Validation before Implemented.** Push; both jobs green on `main`. Then,
   on a scratch branch: commit a deliberate `ruff` violation and a
   deliberate `uv.lock` drift (edit a bound in `pyproject.toml` without
   relocking); confirm `checks` and `tests` fail respectively. Delete the
   branch.

## References

- `astral-sh/setup-uv`: https://github.com/astral-sh/setup-uv
- `.pre-commit-config.yaml`, `pyproject.toml` (`requires-python`)
- ADR-002 (uv and pre-commit), ADR-010 and ADR-012 (hooks that ride along),
  ADR-013 (depends on this)

## Implementation Status

Not started.
