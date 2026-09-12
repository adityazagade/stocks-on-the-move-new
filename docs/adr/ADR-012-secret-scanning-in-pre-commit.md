# ADR-012: Secret scanning in pre-commit and CI

- **Status**: Accepted
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

The repository directory holds a Kite API key and secret in `.env`, and from
ADR-005 onward a daily access token under the home directory. `.env` is
git-ignored and has never been committed (`git log --all -- .env` is empty).
The protections are one ignore line and attention.

On 2026-09-12 the API key and secret were pasted into a chat session while
discussing PyCharm configuration. The secret was rotated. The incident is
evidence that the credentials are handled by hand often enough to slip, and
ADR-004 makes weekly commits from this directory routine.

Nothing scans staged content for secrets today.

## Decision

Add secret scanning to the pre-commit configuration, and therefore to CI
(ADR-011).

- **gitleaks** via its official hook: repository
  `https://github.com/gitleaks/gitleaks`, hook id `gitleaks`, which runs
  `gitleaks git --pre-commit --redact --staged --verbose`. Its language is
  `golang`; pre-commit 3.0 and later bootstraps a Go toolchain if none is
  installed, so no manual setup. The version is pinned by `rev`.
- **`detect-private-key`** from `pre-commit/pre-commit-hooks`, already a
  dependency of the configuration.
- **A repository rule file**, `.gitleaks.toml`, extending the default rules
  with one for this project: any assignment of the form
  `KITE_API_KEY=<8+ non-space characters>` or `KITE_API_SECRET=<...>`, plus
  the `KITE_SESSION_FILE` contents shape from ADR-005 (`"access_token": "..."`).
  The generic rules catch high-entropy strings; the project rule catches a
  low-entropy key next to a name that says what it is.
- **Allowlist**: `.env.example` (values are empty), `docs/adr/` and
  `ONBOARDING.md` (they name variables without values). Nothing else. The
  ledgers contain no secrets and are not allowlisted.
- **One-time history scan** as part of adoption: `gitleaks git` over the
  full history. The result, expected clean, is recorded in Notes.
- The hook runs with `--redact`, so a caught secret is not echoed into the
  terminal or a CI log.

## Consequences

### Positive

- A credential in a staged file stops the commit locally and, if the local
  hook was skipped, fails CI before anyone pulls.
- The rule that matters most here is explicit and named, not left to
  entropy heuristics.
- Redaction means the scanner does not itself become a leak.

### Negative

- First `pre-commit run` after adoption downloads a Go toolchain and builds
  gitleaks; a minute, once per machine and once per CI cache miss.
- False positives on high-entropy test data are possible. The fix is a
  fingerprint in `.gitleaksignore` with a comment, never a blanket allowlist.
- Scanning catches secrets in files being committed. It does nothing for a
  secret pasted into a chat, a screenshot, or a log. Rotation remains the
  answer to those.

### Neutral

- Commit time grows by well under a second.
- `.gitleaks.toml` and possibly `.gitleaksignore` join the repository root.

## Alternatives Considered

### Option 1: Status quo, `.gitignore` and care

**Pros:**

- Has worked so far.

**Cons:**

- One forced add or one renamed file away from a live credential on GitHub,
  with no detection.

### Option 2: `detect-secrets` (Yelp)

**Pros:**

- Python, installs into the pre-commit environment without Go; a baseline
  file suppresses known findings.

**Cons:**

- The baseline workflow is designed for large repositories with existing
  findings; here there are none. Weaker default rules than gitleaks and no
  project-rule story as direct as a TOML pattern.

### Option 3: TruffleHog

**Pros:**

- Verifies candidate secrets against the issuing service.

**Cons:**

- Verification means sending candidate secrets to third-party endpoints,
  which is not something a brokerage credential should do on every commit.

### Option 4: GitHub secret scanning and push protection only

**Pros:**

- No local setup.

**Cons:**

- Detection after the push, not before the commit; pattern coverage for a
  small Indian broker's key format is unlikely; push protection for private
  repositories requires a paid plan.

## Implementation Plan

1. **Adopt**, one commit: hooks, `.gitleaks.toml`, allowlist; `ONBOARDING.md`
   section 6 gains one sentence.
2. **History scan**: `uv run pre-commit run gitleaks --all-files` and a full
   `gitleaks git` over the history; record the outcome in Notes.
3. **Validation before Implemented.** Stage a file containing
   `KITE_API_SECRET=abcdefgh12345678` on a scratch branch; the commit must be
   refused with a redacted finding. Delete the branch.

## References

- gitleaks: https://github.com/gitleaks/gitleaks
- pre-commit language support (Go bootstrap since 3.0): https://pre-commit.com/
- ADR-004 (weekly commits from this directory), ADR-005 (token file shape),
  ADR-011 (CI runs the same hooks)

## Implementation Status

Code complete on 2026-09-12; awaiting the CI half of the validation in the
pull request.

- **Step 1, adopt.** `gitleaks/gitleaks` at `v8.30.1` (hook id `gitleaks`)
  and `detect-private-key` in `.pre-commit-config.yaml`; `.gitleaks.toml`
  extends the default rules with `kite-credential-assignment` and
  `kite-access-token-json`; allowlisted paths are `.env.example`,
  `docs/adr/*.md` and `ONBOARDING.md`, nothing else. The onboarding guide's
  tooling section gained a paragraph.
- **Step 2, the history scan**, over all 37 commits with the first draft of
  the rules: 5 findings, none a credential. Four came from the project rule
  matching `kite_api_key: SecretStr` (a type annotation in `settings.py`)
  and `kite_api_key="test-key"` (a test keyword argument); the rule now
  matches the environment form only, `KITE_API_KEY=...` in upper case with
  `=`, which is how a credential appears in `.env`, a shell, a run
  configuration or YAML. One came from the default `generic-api-key` rule on
  the request-token constant in the ADR-005 tests; the constant is now a
  low-entropy value and its historical occurrence is one fingerprint in
  `.gitleaksignore` with a comment. The full-history scan is then clean:
  "no leaks found". A filesystem scan of the working directory still finds
  the real credentials in the git-ignored `.env`, as it should; the hook
  scans staged content and never sees that file.
- **Step 3, the refusal.** On a scratch branch, a staged file containing
  `KITE_API_SECRET=abcdefgh12345678` was refused: hook `Detect hardcoded
  secrets` failed, rule `kite-credential-assignment`, the secret shown as
  `REDACTED`, the value absent from the whole output. Branch deleted.
- **CI.** The hook runs `gitleaks git --pre-commit --staged`; in CI nothing
  is staged, so through pre-commit alone the scan would pass trivially and
  the Consequence "fails CI before anyone pulls" would be false. The CI
  `checks` job therefore also runs `gitleaks/gitleaks-action` (v3.0.0,
  pinned by SHA) over the pushed commits with the same `.gitleaks.toml`, the
  same gitleaks version and PR comments off so the job keeps
  `contents: read`. Its first run happens with this pull request.

## Notes

- Two rule details differ from the Decision's wording. Six characters, not
  eight, is the minimum value length, because a real Kite API key is seven
  characters. The rule is case-sensitive and requires `=`, not `:`, for the
  reason under step 2; a real secret in a Python literal is still covered
  by the default entropy rules.
- gitleaks reads `.gitleaks.toml` from the repository root on its own; the
  CI step passes it explicitly all the same.
