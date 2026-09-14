# ADR-033: Rank the whole universe and qualify names for entry

- **Status**: Accepted
- **Date**: 2026-09-14
- **Last Updated**: 2026-09-14
- **Author**: Aditya Zagade

## Context

Step 6 filters and then ranks. `evaluate` walks the chain — enough history,
above the 100-day average, volume, ATR, the gap rule — and the first rule
that stops a name returns an `Evaluation` with no `RankItem`. `rank` keeps
only the names that carry one. Everything downstream divides by the length
of that list.

Two things follow from the order, neither of them chosen.

**The cut-off's width is market breadth.** `CUT_OFF_PCT` is documented as
"the top fraction of the universe", but the universe it divides is the
passing names, not the 500. In the run of 2026-09-14: 500 instruments, 200
ranked, 286 stopped at `below_ma100`, 14 at `gap`. `0.20` meant "hold and
buy the top 40". In a narrower week, when 120 names are above their 100-day
average, the same `0.20` means the top 24, and every holding ranked 25th to
40th is sold — not because its own momentum decayed, but because the count
of names above a moving average fell. The number that decides how many
positions the strategy will carry is a breadth statistic.

**Every entry filter is an exit rule by construction.** A held name that
trips any filter leaves the ranking, and a holding that is not in the
ranking is sold: `unranked:volume`, `unranked:atr_pct`, `unranked:gap`,
`unranked:below_ma100` (ADR-025). Of those four, two were written as exits
and two were not. The 100-day average is the book's exit and is already an
explicit rule in `exit_check`. The gap rule is the book's exit too, and
ADR-025 rejected making it entry-only on exactly that ground. But the
volume floor and the ATR ceiling were chosen to keep illiquid and wild
names out of a *new* position. Neither was ever argued for as a reason to
sell a position that is working; they became one because of where they sit
in the chain.

The book ranks the whole index by momentum and treats the trend and gap
rules as separate conditions on top of the ranking. This port folded the
two operations together.

The obvious repair — leave the disqualified names in the list but append
them to the end — is worse than the status quo. A name placed last is
beyond `CUT_OFF_PCT` by arithmetic, so `rank_cutoff` fires on it; the
exits table would then report a momentum verdict on a name whose position
was assigned by the filter chain, not measured. It also pads the
denominator with positions that carry no information.

## Decision

Rank every name that has a momentum score, in score order, whatever it
failed. Carry the qualification as a flag beside the rank. Buy only the
qualified.

- `evaluate`'s chain splits in two. **Unrankable**: there is no score to
  place the name by — `error:<type>`, `history`, `insufficient_data`. These
  get no `RankItem`, exactly as today, and a holding in this state still
  exits as `unranked:<cause>`. **Rankable**: a score exists. The name
  always gets a `RankItem`, and the entry rules it failed are recorded
  rather than used to drop it.
- `RankItem` gains `qualified: bool` and `reason: str | None`, the first
  entry rule the name failed among `below_ma100`, `volume`, `atr_pct`,
  `gap`. `Evaluation` keeps `reason` for `universe.csv`.
- `rank` sorts every rankable item by score, qualified or not. `pct_rank`
  divides by that count: the universe less the unrankable, near 500 rather
  than near 200, and steady from week to week.
- `ranking.csv` gains `qualified` and `reason`, and holds every universe
  name: the rankable in score order, then the unrankable at the end with
  `rank`, `pct_rank` and `score` left **blank**, their `reason` filled in.
  Blank, not zero — the number was not measured, and the count the
  denominator uses excludes them. `universe.csv` keeps its columns;
  `status` reads `ranked`, `ranked:disqualified` or `excluded`.
- `buy_candidates` skips a disqualified name with
  `SKIP:disqualified:<reason>` and keeps walking. The skip consumes no
  `max_positions` slot and does not stop the walk, so the buys are the
  qualified names in score order, as they are today.
- The exit rules are stated on their own rather than inherited from the
  ranking: `unranked:<cause>` for an unrankable holding, `rank_cutoff`
  against the new denominator, `below_ma100`, the trailing stop, and
  **`gap` as an explicit rule in `exit_check`**, which keeps ADR-025's
  behaviour and its evidence once the gap name is ranked instead of
  dropped. The volume floor and the ATR ceiling become entry-only: a
  position is no longer sold because its 20-day volume dipped or its ATR
  widened.

`CUT_OFF_PCT` keeps its value and changes meaning: `0.20` becomes the top
100 of about 500 rather than the top 40 of however many passed. That is a
material loosening of the `rank_cutoff` exit and is what the gate decides.

**Acceptance gate.** This ADR moves from Proposed to Accepted only on the
comparison below, run with the ADR-023 harness over at least four years of
the cached history, all other parameters equal. Following ADR-028, the code
this produces before the gate is the switch and nothing else:
`StrategyParams.rank_scope: Literal["qualified", "universe"]`, defaulting
to `"qualified"`, so a live run and the golden files are unchanged until
the gate passes.

1. **A**: the status quo, filter then rank.
2. **B**: `rank_scope=universe` at `cut_off_pct=0.20`, the cut-off's value
   held and its width allowed to widen to about 100 names.
3. **C**: `rank_scope=universe` at `cut_off_pct=0.08`, the cut-off retuned
   so the band stays near today's 40 names and the change is the
   qualification flag alone.

The new scheme is adopted if B or C is not worse than A on return over
volatility and on maximum drawdown, where "not worse" is within two percent
of A's figure or better; if both pass, the higher return over volatility
wins. If neither passes, this ADR is marked **Rejected**, the switch stays
at its default, and the three summaries go in Notes regardless of the
outcome, so the next person to ask why the ranking is scoped the way it is
can read the numbers.

## Consequences

### Positive

- The cut-off divides a stable denominator. The top fifth of the universe
  is the same width every week, and the number of positions the strategy
  is willing to carry stops tracking market breadth.
- Entry rules and exit rules become two lists that can be argued about and
  changed separately. Today adding an entry filter silently adds an exit.
- A holding is sold for something true of the holding. The case this ADR
  starts from — a name pushed past the cut-off by where the filter chain
  put it rather than by its momentum — cannot arise.
- `ranking.csv` shows every scoreable name with its true momentum position
  and, where it is not buyable, why. "Where did my stock go" is one column
  in one file instead of a join against `universe.csv`.
- No extra computation: `Snapshot.from_candles` already scores every
  instrument, and the score of a name that fails a filter is computed and
  thrown away today.

### Negative

- A strategy change, not a reporting change. Every holding is re-ranked
  against a different denominator on the first run after it lands; the
  first run must be a `PLAN_ONLY=1` run, read before the real one (ADR-022).
- Under B the `rank_cutoff` exit loosens from about 40 names to about 100,
  and holdings that exit on rank today are held longer. Under C the band is
  held at today's width and the loosening does not occur; the gate exists
  to choose.
- The volume floor and the ATR ceiling stop being exits. A holding whose
  liquidity dries up is held until another rule fires. This is deliberate —
  neither was chosen as an exit — but it is a real loosening and the
  backtest covers it.
- `ranking.csv` grows from the passing names to every universe name, about
  500 rows, and the console's Rank table with it. Its `rank`, `pct_rank`
  and `score` are blank on the unrankable tail, so anything reading the
  file has a column that can be empty where it never was before.
- The golden expected files move: `ranking.csv` gains rows and two columns,
  and `exits.csv` and `candidates.csv` follow. Regenerated once with the
  diff reviewed and described in the commit body (ADR-009).

### Neutral

- `universe.csv` keeps its shape and its reasons; only `status` gains a
  value.
- ADR-025 is not superseded. The gap rule stays an entry filter and an exit;
  only the mechanism moves, from "dropped from the ranking, and therefore
  sold" to an explicit rule in `exit_check`. ADR-025's own Option 4, the
  gap rule at entry only, stays rejected.

## Alternatives Considered

### Option 1: Status quo, filter then rank

**Pros:**

- Known behaviour, encoded in the golden files. No holding can survive a
  filter it fails, which is the conservative direction.

**Cons:**

- The cut-off's width is a breadth statistic, and every entry filter is an
  exit whether or not anyone argued for it as one.

### Option 2: Append the disqualified names to the end of the ranking

The literal reading of the request: keep the list complete by putting the
excluded names after the passing ones.

**Cons:**

- A name is then beyond the cut-off because of where it was placed, so
  `rank_cutoff` fires and `exits.csv` records a momentum verdict that was
  never measured. It also inflates the denominator with positions that mean
  nothing, which makes `pct_rank` worse, not better.

### Option 3: Show everything in `ranking.csv`, change no decision

Append the disqualified names to the artifact only; keep `rank` and every
denominator as they are.

**Pros:**

- Zero strategy risk; answers the reporting question on its own.

**Cons:**

- Leaves both real problems untouched. The cut-off still moves with
  breadth and a held name still exits because it failed an entry filter.

### Option 4: Score the unrankable names 0 so they sort to the end

Give a name with no score a score of `0`, so it takes a rank like any other
and needs no special case.

**Pros:**

- One list, one type, no blank columns and no second path through
  `exit_check`.

**Cons:**

- `0` is not the end of the list. Momentum scores go negative: in the run
  of 2026-09-14 six of the 200 *passing* names already score below zero,
  and zero sits near rank 194 of 200. Once the 286 `below_ma100` names are
  ranked too, most of them negative, a fabricated `0` lands around rank 194
  of 500 — above some three hundred names whose decline was measured.
- Worse, that position is not fixed. Zero falls wherever the positive-score
  line falls that week: near rank 350 of 500 in a broad bull market, near
  rank 51 in a bear one. A `cut_off_pct` of `0.20` holds the top 100, so
  the same holding is exited in one market and kept in the other, on a
  number nobody measured.
- For these names `rank_cutoff` is the only exit that can fire. A
  nan-filled snapshot makes `below_ma100` compare `nan <= nan`, which is
  false, and `trailing_stop` returns early on `snap.error` or too few rows.
  Removing `unranked:<cause>` in favour of a fabricated rank therefore
  leaves a holding whose candles failed to fetch with no dependable exit,
  in exactly the market where the failure is most likely to matter.
- It repeats, in a less visible form, the objection that rules out Option
  2: a position in the ranking assigned rather than measured.

### Option 5: Keep filtering, but divide `pct_rank` by the universe size

**Pros:**

- Fixes the moving denominator in one line.

**Cons:**

- A disqualified holding is still absent from the list and still exits as
  `unranked`, so the exit half of the problem survives, and `pct_rank`
  would then describe a position in a list the name is not in.

## Implementation Plan

1. **Prerequisites**: ADR-021 (snapshot and params) and ADR-023 (harness)
   at Implemented.
2. **The switch**, one commit, before the gate: `rank_scope` on
   `StrategyParams` defaulting to `"qualified"`, read by `evaluate` and
   `rank`. A live run is unchanged and the golden files do not move; the
   commit proves both.
3. **The comparison**: the three runs, the summaries into Notes, the owner
   sets Accepted or Rejected.

       python -m stocks_on_the_move.backtest run --from 2022-01-05 --to 2026-09-09 --label scope-qualified
       python -m stocks_on_the_move.backtest run --from 2022-01-05 --to 2026-09-09 --label scope-universe \
           --set rank_scope=universe
       python -m stocks_on_the_move.backtest run --from 2022-01-05 --to 2026-09-09 --label scope-universe-tight \
           --set rank_scope=universe --set cut_off_pct=0.08
       python -m stocks_on_the_move.backtest compare scope-qualified scope-universe scope-universe-tight

4. **The change**, if Accepted, one commit: the qualification through
   `Evaluation` and `RankItem`, the two `ranking.csv` columns, the
   `SKIP:disqualified:<reason>` decision in `buy_candidates`, the `gap`
   exit rule in `exit_check`, the `CUT_OFF_PCT` description in
   `settings.py` with `.env.example` regenerated, unit tests on a
   disqualified name that ranks and is never bought and on a held
   disqualified name that is not sold for the volume or ATR rule, and the
   golden update with its reviewed diff.
5. **Docs**: `ONBOARDING.md` section 3, both the filter list and the exit
   list, which currently state the coupling this ADR removes; the
   deviations in section 1.
6. **Validation before Implemented.** Suite green; a `PLAN_ONLY=1` run
   read by hand, checking that no row in `candidates.csv` with
   `qualified = false` reached a BUY, that `ranking.csv` holds every
   scoreable name, and that the reasons in `exits.csv` are the four
   intended rules and nothing else.

## References

- ADR-021 (the snapshot and the pure rules), ADR-022 (the plan before the
  first live run), ADR-023 (the harness), ADR-024 (the moving averages)
- ADR-025 (the gap filter; ADR-025's Option 4 stays rejected, and its exit moves
  from the exclusion to an explicit rule)
- ADR-028 (the acceptance-gate precedent and the switch-before-the-gate
  pattern), ADR-009 (the golden update)
- Clenow, *Stocks on the Move*: the whole index is ranked; the trend and
  gap rules are conditions on top of the ranking, not inputs to it
- `ONBOARDING.md` section 3, filters and scoring, and the exit rules
- `src/stocks_on_the_move/rules.py` (`evaluate`, `rank`, `exit_check`),
  `pipeline.py` (`decide_exits`, `buy_candidates`), `reporting.py`

## Implementation Status

Accepted by the owner on 2026-09-14. The Decision is implemented behind
`rank_scope`, which defaults to `"qualified"`: the live run's trading is
unchanged and the gate below still decides whether the default flips.

- `StrategyParams.rank_scope` (`"qualified"` | `"universe"`), read by `rank`.
  `--set rank_scope=universe` selects it in a backtest.
- `evaluate` no longer stops at the first failing rule. A name with no score —
  `error:<type>`, `history`, `insufficient_data` — is excluded and carries no
  `RankItem`, as before. Any name that has a score gets one, with `qualified`
  and the first entry rule it failed (`disqualification`) recorded on it.
  Every metric is measured whatever failed, so `universe.csv` is filled in for
  a disqualified name where it used to be blank, and `status` reads `ranked`,
  `disqualified` or `excluded`.
- `ranking.csv` gains `qualified` and `reason`, and ends with the unrankable
  names, their rank, `pct_rank` and score blank (`unrankable`, `ranking_rows`).
  `rank_step` writes it, next to the `universe.csv` it already wrote.
- `buy_candidates` skips a disqualified name with
  `SKIP:disqualified:<reason>`, consuming no `max_positions` slot and not
  stopping the walk.
- `exit_check` states the gap rule itself, so a gapped name that now holds a
  rank still exits on it. The volume floor and the ATR ceiling are not exits.
- Tests: the qualification through the chain, a disqualified name ranked on
  its momentum rather than pushed to the end, the two scopes, the unrankable
  tail's blanks, the gap exit and the knob that disables it, volume and ATR
  not exiting a holding, and the `SKIP:disqualified` walk.
- **The golden files were not regenerated.** They were already failing on the
  commit this work started from (`4d5c7ab`, `e5fe55e`), so a regeneration now
  would fold three behaviour changes into one unreviewable diff, which is the
  thing ADR-009 exists to prevent. This change's own effect on them was
  isolated by regenerating on both trees and diffing those against each other:
  only `ranking.csv` and `universe.csv` move, in the two ways described above.
  `exits.csv`, `sizing.csv`, `candidates.csv`, `trades.csv`, `orders.csv` and
  `portfolio_after.csv` are byte-identical — no trade changed.
- **Still to do**: the three backtest runs of the gate, and then the owner's
  decision on the default and on `CUT_OFF_PCT`, whose description in
  `settings.py` is left alone until the denominator actually changes.
