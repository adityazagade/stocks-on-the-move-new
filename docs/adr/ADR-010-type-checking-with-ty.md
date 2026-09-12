# ADR-010: Static type checking with ty

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

The code is annotated throughout, and nothing reads the annotations. A
checker would have reported the first item on the rough-edges list:
`kite_call` is used as if it returns a value on every path, yet falls off
the end of its retry loop and returns `None`. Once ADR-008 gives the broker
boundary real types, a checker also guards every call that today receives
`Any` from `kiteconnect`, which has no type information.

Three candidates are current on PyPI as of 2026-09-12:

- **ty** 0.0.80 (Astral). Its README states: "ty is currently in beta" and
  "ty uses 0.0.x versioning. ty does not yet have a stable API; breaking
  changes, including changes to diagnostics, may occur between any two
  versions." A pre-commit hook exists at `astral-sh/ty-pre-commit` and
  resolves the project's dependencies from `pyproject.toml`.
- **pyright** 1.1.414 (Microsoft). Mature, stable diagnostics, the reference
  for the typing spec. The PyPI package downloads a Node runtime on first
  run.
- **mypy** 2.3.1. Mature; does not check the bodies of unannotated
  functions by default, which would have hidden the `kite_call` defect.

The project already depends on Astral for packaging (uv, ADR-002) and
lint (ruff). ruff's `UP` rules have already modernised the annotations.

## Decision

Adopt **ty** as the project's type checker, pinned to an exact version, and
make it blocking from the first commit.

- `ty` is a dev dependency with an exact pin (`ty==0.0.80` at the time of
  writing). Version bumps arrive through ADR-013 as reviewable pull requests
  and are declined if a bump only changes diagnostics.
- Configuration lives in `pyproject.toml` under `[tool.ty]`, next to ruff's.
  `archive/` is excluded, as it is for ruff.
- `astral-sh/ty-pre-commit` at the matching `rev` joins
  `.pre-commit-config.yaml`; CI (ADR-011) runs the same hook.
- The implementation commit fixes every diagnostic in `src/` and `tests/`.
  A suppression comment is allowed only where the checker is wrong, must
  name the rule, and must carry a one-line reason.
- `kiteconnect` is untyped; its results are `Any` until ADR-008 introduces
  typed return shapes at the broker boundary. No stubs are written for it.
- `pandas-stubs` is added only if a trial shows it removes more `Unknown`s
  than it adds false positives; it must match the pandas major (ADR-003).

**Fallback clause.** If two consecutive ty releases each require changes to
this codebase for diagnostic churn alone, or a false positive cannot be
suppressed on one line, the checker is switched to pyright by a superseding
ADR. The configuration and hook are structured so that switch is one file
and one line.

## Consequences

### Positive

- The `kite_call` class of defect is caught at commit time.
- Same vendor and config file as the rest of the toolchain; check time is
  negligible for a codebase this size.
- Once ADR-008 lands, the broker boundary is where most runtime surprises
  come from, and it becomes typed.

### Negative

- ty is beta. Diagnostics may change between versions; the exact pin
  contains this to deliberate upgrades, but each upgrade may cost a fix-up
  commit that adds nothing to the strategy.
- The initial fix-up will touch many lines: dict-shaped Kite responses, the
  module globals, `Optional` returns that callers ignore. Some of this is
  real (the `None` return), some is annotation debt.
- A second Astral beta in the toolchain. `uv_build` is the first.

### Neutral

- Runtime behaviour is unchanged; the checker never runs in production.
- `# type: ignore` comments from the pre-ty era, if any, are converted to
  ty's syntax or removed.

## Alternatives Considered

### Option 1: Status quo, annotations unchecked

**Pros:**

- Nothing to run.

**Cons:**

- The annotations are documentation that can lie, and did.

### Option 2: pyright

**Pros:**

- Mature, stable diagnostics, strict mode well understood, editor support
  everywhere.

**Cons:**

- Node download on first use; a second toolchain vendor and config style.
  Named as the fallback, so choosing ty first costs little if it fails.

### Option 3: mypy

**Pros:**

- Pure Python, the longest track record.

**Cons:**

- `check_untyped_defs` is off by default and the codebase has untyped
  functions in the exact place the defect lives. Slower; separate plugin
  ecosystem for pandas.

### Option 4: Run a checker in CI only, advisory, non-blocking

**Cons:**

- Advisory checks are ignored within a month. Blocking or nothing.

## Implementation Plan

1. **Trial**, no commit: `uvx ty check src tests` on the current tree; count
   diagnostics by rule; decide on `pandas-stubs`.
2. **Adopt**, one commit: dev dependency, `[tool.ty]`, pre-commit hook, every
   diagnostic fixed or suppressed with a reason. The `kite_call` fix itself
   belongs to ADR-008; here it is suppressed with a reference to that ADR if
   ADR-008 has not landed.
3. **Validation before Implemented.** Introduce a deliberate type error in a
   scratch branch and confirm both the pre-commit hook and CI reject it.

## References

- ty: https://github.com/astral-sh/ty and https://github.com/astral-sh/ty-pre-commit
- pyright: https://github.com/microsoft/pyright
- ADR-002 (Astral toolchain), ADR-008 (typed broker boundary),
  ADR-011 (CI runs the hook), ADR-013 (version bumps as pull requests)
- `ONBOARDING.md`, rough edge 2

## Implementation Status

Implemented on 2026-09-12. Plan step 3 passed in full: the hook rejected a
deliberate type error locally; pull request #8 ran green with the `ty` hook
in the `pre-commit hooks` job; scratch pull request #9, carrying the same
deliberate error, went red on that job (`invalid-assignment`) with both
`pytest` jobs green; `main` is green after the merge (`d28dbe2`).

- **Step 1, the trial.** ty 0.0.80 on `src/` and `tests/` after ADR-008:
  17 diagnostics, all real: 14 `invalid-argument-type`, 1
  `invalid-method-override`, 1 `invalid-assignment`, 1 `unresolved-attribute`.
  With `pandas-stubs` 2.3.3 installed in a scratch environment: 19, the same
  17 plus two false positives (`np.isclose` overload, `.date()` on a wide
  union) and no `Unknown` removed. `pandas-stubs` is not adopted.
- **Step 2, adopt.** `ty==0.0.80` in the dev group; `[tool.ty]` in
  `pyproject.toml` (`src.include = ["src", "tests"]`, `src.exclude =
  ["archive"]`, `environment.python-version = "3.12"`);
  `astral-sh/ty-pre-commit` at `v0.0.80` in `.pre-commit-config.yaml`, so
  CI (ADR-011) runs it by construction. Every diagnostic fixed: keyword
  unpacking of a metrics dict into a dataclass replaced by a closure with
  typed locals; `Side` literals on the order helpers; the
  `BaseHTTPRequestHandler.log_message` override keeps the base parameter
  name; the bind address is unpacked with explicit `str`/`int`; the
  redirect wait loop narrows `listener` instead of a `type: ignore`; the
  test helper for `AuthSettings` takes explicit keywords; the fake-client
  factory parameter is `Callable[..., Any]`. One suppression remains, in the
  test that deliberately passes an invalid side to `Order`, on one line,
  naming the rule, with its reason. `# type: ignore` comments are gone.
- **Step 3.** Locally, a scratch module with a deliberate type error made
  the `ty` hook fail with `invalid-assignment` and exit 1; in CI the same
  error on a scratch branch made the `pre-commit hooks` job red while both
  `pytest` jobs stayed green. Results above; the scratch branch is deleted.

## Notes

- **ty 0.0.80 delegates `ty check` to `uv check`.** The `ty` wheel's binary
  looks for a `uv` executable next to itself and fails without one; the
  official hook is literally `uv check --preview-features=check-command
  --ty-version=0.0.80` (its environment pins `uv==0.12.12`). So that
  `uv run ty check` works from the project environment, `uv` joins the dev
  group as `uv>=0.12.9,<0.13`, bounded at the next minor because uv is
  pre-1.0 and its minors carry breaking changes. The alternative, telling
  developers to type the `uv check --preview-features...` incantation, was
  rejected as a documentation burden for the same result.
- `[tool.ty.environment].python-version` would be inferred from
  `requires-python` anyway; it is written down so the target is visible next
  to the rules.
- The ADR's Context lists pyright 1.1.414 and mypy 2.3.1 as of 2026-09-12;
  those numbers were not re-checked at implementation time. ty's was:
  0.0.80 is still the latest release (2026-09-09).
