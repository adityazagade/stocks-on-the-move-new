# ADR-001: Record architecture decisions

- **Status**: Implemented
- **Date**: 2026-09-12
- **Last Updated**: 2026-09-12
- **Author**: Aditya Zagade

## Context

The project began as a single script copied forward as `test_updated_v0.py`
through `test_updated_v4.py`, with no version control. The reasoning behind
changes was never written down. One visible result: the momentum lookbacks
were shortened from 21/63/126 to 5/15/45 trading days between v3 and v4, but
the constants are still named `LOOKBACK_R21`, `LOOKBACK_R63`, `LOOKBACK_R126`
and the docstring still describes the old formula. Nobody can say today why
the change was made or what it was compared against.

On 2026-09-12 the toolchain was modernised (ADR-002) and several trade-offs
were settled in a chat session: pandas held below 3.0 (ADR-003), account
ledgers tracked in git (ADR-004). That reasoning survives only in commit
messages and the onboarding guide.

This is a trading system. A change to a filter threshold, a fee assumption or
a ledger column alters real or paper money, and the effect outlives the
commit. The owner wants every further change to be a deliberate, reviewable
decision with its alternatives on record. Contributors include AI coding
assistants, which need an explicit contract rather than inferred habits.

## Decision

All development on this repository proceeds through Architecture Decision
Records.

**Where.** `docs/adr/`, one file per decision, named
`ADR-NNN-kebab-case-title.md` with a zero-padded three-digit sequential
number. `docs/adr/template.md` is the format; `docs/adr/README.md` is the
index and must list every ADR with its status.

**What needs an ADR.** Any change to:

- strategy logic or parameters: universe, filters, scoring, sizing, exits,
  regime, cadence, and the defaults of any environment variable;
- data formats: the portfolio CSV, either ledger, the candle cache schema;
- dependencies: adding, removing, changing bounds, or a major-version bump;
- tooling, project layout, or process, including this one;
- any item on the "known rough edges" list in `ONBOARDING.md`.

Exempt: typo, comment and documentation wording fixes, and formatting-only
changes. An exempt change must not alter behaviour. When in doubt, write the
ADR; a short one costs ten minutes.

**Lifecycle.** Draft, Proposed, Accepted, Implemented, with Deprecated and
Superseded as end states. Only the repository owner moves an ADR to Accepted.
Implementation starts only after Accepted. Changing an Accepted decision means
a new ADR that supersedes the old one; the old ADR gets Status Superseded and
a pointer, and is otherwise left as written.

**Commits.** An ADR is committed on its own before implementation, subject
`ADR-NNN: <title>`. Every implementation commit names the ADR in its subject
or body. The final implementation commit sets the ADR's status to Implemented
and updates the index.

**Size.** One decision per ADR. Context and Decision together should fit on a
page. At least two alternatives, one of them the status quo.

**Backfill.** Decisions already taken on 2026-09-12 are recorded as ADR-002
through ADR-004 with Status Implemented so the log has no gap. History before
that date (v0 to v4) is not reconstructed; it is documented as unknown.

## Consequences

### Positive

- The reason for a change is recorded next to the alternatives it beat, in
  the repository, in plain text, forever.
- Changing something that moves money gets a deliberate pause and a written
  validation plan before code changes.
- Coding assistants have a defined stopping point: draft the ADR, wait for
  acceptance, then implement.
- The "known rough edges" list becomes a decision queue instead of a wish
  list.

### Negative

- Overhead on small changes. A one-line parameter tweak now costs a document.
- With a single maintainer, "review" is self-review. The process only works
  if the owner reads their own Proposed ADR the next day rather than
  accepting it in the same sitting.
- Risk of ceremony: ADRs written after the fact to satisfy the rule carry no
  information. The commit-the-ADR-first rule exists to make that visible.

### Neutral

- Commit bodies can get shorter, since the why lives in the ADR; they must
  still name it.
- `ONBOARDING.md` and `README.md` point here; the onboarding guide's
  workflow section is the human-readable summary of this ADR.

## Alternatives Considered

### Option 1: Status quo, reasoning in commit messages and the onboarding guide

Keep writing good commit bodies and maintain the rough-edges list.

**Pros:**

- No extra artefacts or process.

**Cons:**

- Alternatives considered are never recorded, only the winner.
- No pause before implementation; the lookback drift is exactly what this
  produces.
- Rationale is scattered across `git log` and prose that goes stale.

### Option 2: A single `DECISIONS.md` log

One file, one line or paragraph per decision, newest at the bottom.

**Pros:**

- Minimal; one file to read.

**Cons:**

- No room for context, consequences or alternatives without the file
  becoming unreadable.
- No clean way to mark a decision superseded.
- Tends to be edited in place, destroying the history it exists to keep.

### Option 3: GitHub issues and pull request descriptions

Discuss and decide in issues; the merged PR is the record.

**Pros:**

- Threaded discussion, notifications, links to code.

**Cons:**

- The record lives outside the repository and is lost if the project moves.
- A solo developer working on `main` does not open pull requests.
- Not readable offline or from a checkout.

### Option 4: Full RFC process with review periods and sign-off

**Pros:**

- Maximum rigour.

**Cons:**

- Disproportionate for a one-person research project; would not be followed.

## References

- `docs/adr/template.md`, `docs/adr/README.md`
- `ONBOARDING.md`, sections 6 and 7
- ADR-002, ADR-003, ADR-004 (backfilled decisions)
- Michael Nygard, "Documenting Architecture Decisions", 2011:
  https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions
- ADR community resources: https://adr.github.io/

## Implementation Status

Implemented on 2026-09-12 by the owner. The process has been in force since
that date: ADR-002 to ADR-015 were each drafted as Proposed, accepted by the
owner, implemented in commits naming the ADR, and merged through reviewed
pull requests.

## Notes

The threshold for "needs an ADR" and the lifecycle rules above are a
proposal by the drafting assistant. Adjust them before moving this ADR to
Accepted; after that, changing them is itself an ADR.
