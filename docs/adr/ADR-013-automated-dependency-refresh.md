# ADR-013: Automated dependency refresh with Dependabot

- **Status**: Accepted
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

ADR-003 sets version bounds and says upgrades happen through deliberate
`uv lock --upgrade` commits. Nothing prompts that command. In practice it
runs when someone remembers, which for a weekly-cadence hobby project means
rarely, and the lockfile drifts behind security and bug fixes inside the
allowed majors.

Three sets of pins exist: `uv.lock`, the `rev` fields in
`.pre-commit-config.yaml`, and the action versions in the CI workflow
(ADR-011). Each ages independently.

Two hosted services do this job. **Dependabot** is built into GitHub and
configured by one file. Its options reference, checked 2026-09-12, lists
`pip`, `github-actions` and `pre-commit` among the `package-ecosystem`
values and mentions `uv` in its dependency-type and cooldown tables, but the
fetched ecosystem list did not show a `uv` entry; GitHub announced uv
support in 2025 and the page may have been truncated. **Renovate** (Mend)
documents uv explicitly: its `pep621` manager supports "uv (including
`uv.lock` files and `uv` workspaces)" and delegates lock updates to the uv
CLI, and it has a `pre-commit` manager. It runs as a GitHub App that must be
installed on the repository.

Automated pull requests are only useful if something judges them; ADR-011
provides that.

## Decision

Use **Dependabot**, configured in `.github/dependabot.yml`, with Renovate as
the fallback if Dependabot's uv support turns out not to exist for this
repository.

- **Ecosystems**: `uv` for `pyproject.toml` and `uv.lock`; `github-actions`
  for the workflow pins; `pre-commit` for hook revisions.
- **Schedule**: weekly, Monday, so a pull request is reviewed before
  Wednesday's run, never on the trading day.
- **Grouping**: one pull request per ecosystem per week. All minor and patch
  updates for Python dependencies arrive together; the body lists resolved
  versions, which is what ADR-003 asks for in a lock commit.
- **Bounds are the policy.** Dependabot proposes only what `pyproject.toml`
  allows. A major bump of a runtime dependency is blocked by the bounds and
  therefore never proposed; it remains a human ADR (ADR-003).
- **Merging is manual, always.** A pull request is merged only when CI
  (ADR-011) is green and the owner has read the changelog entries for
  `pandas` and `kiteconnect`. No auto-merge, not even for dev dependencies:
  a `ty` bump (ADR-010) can change diagnostics and deserves eyes.
- **Verification step first.** The initial commit contains the
  configuration; Dependabot's first run either opens pull requests or logs a
  configuration error in the repository's Insights page. If `uv` is
  rejected, the same commit series replaces `dependabot.yml` with
  `renovate.json` and installs the Renovate app, and this ADR's Notes record
  the switch. The decision to automate stands either way.

## Consequences

### Positive

- Patch and minor updates arrive as reviewable pull requests with a CI
  verdict, weekly, without anyone remembering to run anything.
- Pre-commit hook and action pins stop ageing silently.
- ADR-003's "deliberate upgrade commit" becomes a merge with the resolved
  versions already in the body.

### Negative

- A pull request a week to read or dismiss. For three runtime and three
  dev dependencies this is small but not zero.
- Dependabot's uv support is not confirmed from the documentation fetched
  for this ADR. The fallback costs one extra commit and an app install.
- Pull requests from a bot are pushes to branches on the repository;
  ADR-011 runs CI on them, consuming runner minutes.

### Neutral

- The configuration file is the only artefact; nothing runs locally.
- Renovate would cover the same three ecosystems with more configuration
  options; nothing here depends on features it has and Dependabot lacks.

## Alternatives Considered

### Option 1: Status quo, `uv lock --upgrade` when remembered

**Pros:**

- Nothing to configure.

**Cons:**

- Does not happen. Fixes inside the allowed majors are not received.

### Option 2: Renovate as the first choice

**Pros:**

- uv support is documented in so many words; richer grouping and
  scheduling; lock-file maintenance mode.

**Cons:**

- Requires installing a third-party GitHub App on a repository that holds
  account ledgers, and a configuration schema to learn. Chosen as the
  fallback, not the default, for that reason alone.

### Option 3: A scheduled GitHub Actions job running `uv lock --upgrade` and opening a pull request

**Pros:**

- Full control; no third-party app.

**Cons:**

- Reinvents Dependabot with a token, a pull-request action and a workflow
  to maintain; no coverage of action or hook pins without more YAML.

### Option 4: Auto-merge patch updates when CI is green

**Cons:**

- The golden test (ADR-009) does not exist yet; until it does, green CI does
  not mean the ranking is unchanged. Revisit after ADR-009 if the weekly
  review becomes a burden.

## Implementation Plan

1. **Prerequisite**: ADR-011 at Implemented.
2. **Configure**, one commit: `.github/dependabot.yml` with the three
   ecosystems, schedule and groups.
3. **Verify**: within a week, Dependabot has either opened pull requests or
   reported an error for the `uv` ecosystem. Record the outcome in Notes. If
   rejected, one commit swaps in Renovate.
4. **Validation before Implemented.** The first grouped Python pull request
   has passed CI and been merged by hand.

## References

- Dependabot options reference: https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference
- Renovate pep621 manager: https://docs.renovatebot.com/modules/manager/pep621/
- ADR-003 (bounds are the policy), ADR-011 (CI verdict), ADR-010 (why no
  auto-merge for dev dependencies)

## Implementation Status

Not started.
