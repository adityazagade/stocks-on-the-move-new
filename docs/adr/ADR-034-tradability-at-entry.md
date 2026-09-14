# ADR-034: Tradability at entry: the price band, the BZ series, and the limit price

- **Status**: Accepted
- **Date**: 2026-09-14
- **Last Updated**: 2026-09-14
- **Author**: Aditya Zagade

## Context

Since ADR-033 the ranking is every scoreable name in the universe and the
entry filters decide only whether step 11 may open a position. Those filters
— the trend average, the volume floor, the ATR ceiling, the gap rule — are
all statements about the *price series*. None of them asks whether the
exchange will let us trade the thing at all.

The run of 2026-09-14 shows what that costs. The universe is every NSE
equity, 2568 instruments, of which 2219 carry a score and 311 are qualified
inside `CUT_OFF_PCT = 0.20`. The buy step walks that list from rank 1:

| rank | symbol | series | band | status |
| --- | --- | --- | --- | --- |
| 1 | VIJIFIN-BE | BE | 2% | ESM Stage II since 2026-08-03 |
| 2 | SUPREMEENG-BZ | BZ | 2% | GSM Stage 0, issuer non-compliant |
| 10 | SANGINITA-BE | BE | 2% | ESM Stage II since 2026-08-03 |
| 13 | BAFNAPH-BE | BE | 2% | — |

The two best-ranked names in the market are both under **ESM Stage II**,
which means a 2% daily price band and trading **only through periodic call
auctions**. There is no continuous order book to fill against.
`execution._place` posts a limit order and polls `order_status` every
`FILL_POLL_SECONDS` for up to `FILL_TIMEOUT_SECONDS`, then cancels what has
not filled. Against a call-auction name that order cannot fill, ever. The
strategy's top pick of the week is a name it cannot buy.

**What the surveillance frameworks actually do.** Four of them reach NSE
equities, and only one of their restrictions matters to how this strategy
trades.

| | Stage | Margin | Price band | Mechanism | Settlement |
| --- | --- | --- | --- | --- | --- |
| ST-ASM | single | 100% | unchanged | continuous | normal |
| LT-ASM | I | 100% | unchanged | continuous | normal |
| | II | 100% | → next lower (20→10) | continuous | normal |
| | III | 100% | → next lower (10→5) | continuous | normal |
| | IV | 100% | 5% | continuous | gross |
| GSM | I | 100% | 5% | daily | normal |
| | II | — | 5% | daily | T2T, 50% ASD |
| | III, IV | — | 5% | **weekly (Mon)** | T2T, 100% ASD |
| ESM | I | 100% | 5% | continuous | T2T |
| | **II** | 100% | **2%** | **periodic call auction** | T2T |

A 100% margin is nothing to us: the strategy buys delivery with cash it
holds. Trade-to-trade is nothing to us either: it forbids selling on the
day of purchase, and a weekly rotation holds for at least a week. A 5% or
10% band is a cost, not a barrier — the book is continuous and a limit
order fills. **Only the periodic call auction removes the mechanism the
pipeline depends on**, and the 2% band is its marker: it is the signature
of ESM Stage II and of nothing else that reaches a main-board equity.

So the rule to write is about the band, not about trade-to-trade. Of the
263 `-BE`/`-BZ` names in the universe, 46 are inside the cut-off, and the
great majority of them are ordinary surveillance T2T at a 5% band, tradable
through the existing limit-order path in `_price_for`. Excluding them all
would forgo 40 buyable names to catch 6.

**Two smaller things fall out of the same look.**

`BZ` is not a surveillance flag. `BE` means the exchange moved a name to
trade-for-trade; `BZ` means the **issuer failed its listing obligations** —
unfiled results, an unmet disclosure. `series_of` already separates them at
no cost. After the band rule one BZ name survives inside the cut-off,
`IL&FSENGG-BZ` at rank 253. A momentum strategy buying a company that has
not filed its accounts is taking a risk that no price series describes.

And having decided to keep buying thin trade-to-trade books, the price we
post into them matters. `_price_for` answers a BUY with a LIMIT at
`best_ask` and nothing checks how far that is from the last trade. In a
thin `-BE` book the top of the offer can sit at the upper circuit, and the
order fills there. When the book is empty it falls back to `last_price`,
which for a BUY is a limit that will very likely not fill and will burn
`FILL_TIMEOUT_SECONDS` finding out. Both were tolerable while T2T names
were a handful of the NIFTY 500; at 39 of them a week they are not.

**Where the data is.** NSE publishes
`https://nsearchives.nseindia.com/content/equities/sec_list_DDMMYYYY.csv`
every trading day, with columns `Symbol, Series, Security Name, Band,
Remarks`. Its `Band` is the *operative* daily band, whatever framework set
it, which is exactly the question worth asking — the effect, not the cause.
It covers all 2568 names of the universe with none missing. It is dated per
trading day and appears **after** that day: at 22:30 IST on Monday
2026-09-14 the latest file was Friday's `sec_list_11092026`, and the
weekend's and Monday's own returned 404. A run therefore never reads the
band for its own date; it reads the previous trading day's, and reaching
back for it is the normal path, not the exception.

## Decision

Three rules, all of them about whether a position can be opened, none of
them a reason to close one.

**1. A minimum price band.** A name may be opened only when its operative
daily price band is at least `MIN_PRICE_BAND_PCT`, a new setting defaulting
to `5.0`. `No Band` — the F&O names, 210 in the universe, which carry a
dynamic band instead of a fixed one — passes as unbounded. `0` disables the
rule. The reason recorded is `price_band`.

The knob is not really about bands. It is how far down the surveillance
ladder the strategy is willing to go, and the ADR should be read that way:

| floor | excludes |
| --- | --- |
| **5 (the default)** | ESM Stage II, the call-auction names, and nothing else |
| 10 | + ESM Stage I, every GSM stage, LT-ASM III and IV (all 5%) |
| 20 | + LT-ASM II (10%) |

**2. A non-compliant issuer.** A name whose series is `BZ` may not be
opened; reason `non_compliant`. `SZ`, the SME equivalent, is named with it
for correctness though the current universe cannot reach it. `BE` is
deliberately **not** excluded: trade-to-trade on its own is compatible with
a weekly delivery rotation, and the limit-order path already handles it.

**3. A sane limit price.** On the LIMIT path, a BUY is not sent when
`best_ask > last_price × (1 + MAX_ENTRY_SLIPPAGE_PCT)`, a new setting
defaulting to `0.03`, nor when the book is empty. A SELL is never blocked
by either: an exit that does not go out leaves the risk on the book, which
is worse than a bad price, so the empty-book fallback to `last_price` stays
for SELL alone. `_sendable` gains a reason so `candidates.csv` reads
`SKIP:spread` and `SKIP:empty_book` rather than the undifferentiated
`SKIP:not_placed` it returns today.

**Where the rules live.** Rules 1 and 2 are entry disqualifications in
`rules.disqualification`, in ADR-033's shape: the name keeps the ranking
place its momentum earns, `qualified` goes false, and only step 11 reads
the flag. They are checked **first** in the chain, before `below_ma100`,
because "the exchange will not trade this" outranks any statement about the
price. Rule 2 is a pure function of `snap.symbol`, which already carries
the series. Rule 1 needs data no candle holds, so `disqualification` and
`evaluate` take a frozen tradability lookup as an argument — still pure
functions of their arguments, ADR-021 intact. The lookup reaches the
pipeline the way the universe does (ADR-020): a field on `RunContext`,
`None` meaning the live NSE source, which the golden test and the fixtures
set to a static, permissive lookup exactly as they set `StaticUniverse`.
**The pipeline's own default is the live source, never the permissive
one.** A live run must not be able to skip the rule by omission; a test
that forgets to inject falls through to the no-copy rung below, disqualifies
`IDEAX-BE` and `ZETA-BE`, and moves the golden files — which is the tripwire
working. Rule 3 needs the live book and belongs in `execution`, not in
`rules`.

**Resize top-ups follow the qualification.** `resize_positions` does not
consult `qualified` today, so a holding that fails an entry filter can
still be bought into every twelfth day. A top-up is new money at new risk
and the entry filters are exactly the rules for that, so a resize
*increase* into a disqualified name is skipped as `SKIP:disqualified` and a
resize *decrease* is always allowed. This reaches further than the three
rules above — it also stops top-ups on `volume` and `atr_pct`, which
ADR-033 made entry-only — and that widening is intended, but it is the part
of this ADR most worth arguing with.

**Fetching the band, and failing well.** A new source, mirroring
`NseArchives` (ADR-020):

- Walk back from the run's IST date to the most recent published
  `sec_list_DDMMYYYY.csv`, at most 7 calendar days. The run date's own file
  is normally absent, a Monday reaches Friday, and a long weekend needs a
  day or two more; 7 covers the longest NSE closure.
- Key it on `(Symbol, Series)`, which is what `base_symbol(ts)` and
  `series_of(ts)` give us, and which resolves the one duplicate in the file
  (`ELECTCAST` appears as both `EQ` and `W1`).
- A fetch that succeeds writes a last-good copy under `CACHE_DIR`, named
  from `CACHE_DIR` as the universe copy is, with the **file's own date** on
  the first line.
- A fetch that fails uses the copy. Age is measured from the file's date in
  either case — the question is how old the band information is, not
  whether the network worked — with a WARNING naming it and a second
  WARNING past **10 days**. Not the 30 that `NseArchives` uses, and
  deliberately so: the two numbers describe different data. Index
  membership changes quarterly; ESM, GSM and ASM are reviewed **weekly**.
  Because the file is published every trading day and the strategy runs
  weekly, a copy older than 10 days means the fetch has failed on two
  consecutive runs, which is an alarm and not a hiccup. The errors a stale
  copy carries are also the safe kind: ESM has a 90-day minimum retention,
  so names rarely leave, and what a stale copy misses is additions. A fresh
  fetch is at most 7 days old and never warns.
- **No copy at all**: fall back to disqualifying every trade-to-trade
  series for that run, from `series_of` against `NO_MARKET_SERIES`,
  needing no network, with the reason written as `price_band:fallback` so
  `universe.csv` shows the stand-in rather than the rule. 37 of the 38
  banded names in the universe are `-BE` or `-BZ`, so the fallback catches
  almost all of them, at the cost of also skipping the benign 5%-band T2T
  names for one week. The run is never aborted and never silently buys
  blind. `run.json` records which of the three rungs was taken and the
  date of the band file it used.

**A holding below the floor is held, not sold, and is reported.** All three
rules are entry-only. For the band this is less a choice than an
observation: a holding that falls into ESM Stage II can only be exited in
the periodic call auction, which the pipeline cannot reach, so there is no
sell to make. What the run owes the operator is to say so — a WARNING and a
column in `exits.csv`, and a count in `run.json`, whenever a holding sits
below `MIN_PRICE_BAND_PCT`. That position has to be handled by hand and
nothing else in the system will say it.

## Consequences

### Positive

- The strategy stops trying to buy names that have no continuous market.
  On the run of 2026-09-14 that is the first and second ranked names in the
  universe, and 6 of the 311 qualified inside the cut-off: VIJIFIN-BE,
  SUPREMEENG-BZ, SANGINITA-BE, BAFNAPH-BE, VIPULLTD-BE, AMANTA-BE.
- It costs almost nothing else. 38 of 2568 names are excluded universe-wide
  and 305 of the 311 qualified names inside the cut-off survive. 39 `-BE`
  names stay buyable, which is the point: the rule is aimed at the 2% band,
  not at trade-to-trade.
- `MIN_PRICE_BAND_PCT` is one dial over four surveillance frameworks, set
  on what they *do* rather than on which of them did it. A name's stage can
  change without the strategy needing to know the framework's name.
- The limit-price guard puts a bound on entry slippage in exactly the books
  where it is unbounded today, and stops spending `FILL_TIMEOUT_SECONDS` on
  a BUY into an empty book.
- `candidates.csv` distinguishes the three ways an intent goes unsent.
  Today they are all `SKIP:not_placed`.

### Negative

- A new network dependency on a dated NSE file, on the critical path of a
  decision that moves money. The degradation ladder above is the answer to
  it, and its bottom rung — the T2T fallback — is stricter than the rule it
  stands in for, so a bad week skips buyable names rather than buying
  unbuyable ones.
- The resize change reaches past this ADR's three rules. A holding whose
  20-day volume dipped, or whose ATR widened, can no longer be topped up,
  where ADR-033 deliberately stopped those two from being exits. Stopping a
  top-up is not selling, so ADR-033 is not contradicted, but the reach is
  real and is called out here rather than discovered later.
- `MAX_ENTRY_SLIPPAGE_PCT = 0.03` is a first guess. Thin `-BE` books can
  carry spreads that wide legitimately, and the guard will sometimes skip a
  name the strategy wanted. `orders.csv` and `candidates.csv` carry what is
  needed to tune it after a few weeks; it should be tuned, not left.
- Two new settings, `MIN_PRICE_BAND_PCT` and `MAX_ENTRY_SLIPPAGE_PCT`, and
  `.env.example` regenerated with them. The copy's path derives from
  `CACHE_DIR` and needs no setting of its own.
- The band the run reads is always the previous trading day's, because the
  run date's file does not exist yet. A stage move effective on the run
  date itself is unseen until the next run, a week later. Nothing in this
  ADR can close that gap; it is the file's publication schedule.
- A stale band copy is silently *right* most weeks and quietly wrong in the
  particular week a name is newly banded. The 10-day warning is the only
  signal, and it is a log line.

### Neutral

- The golden files move by one column and no decision. `exits.csv` gains
  `band`, blank on the fixtures, and that is the whole diff: the fixtures
  carry `IDEAX-BE` and `ZETA-BE` and no `BZ` or `SZ` name, so rule 2 fires
  on nothing; the injected lookup is permissive, so rule 1 fires on nothing;
  rule 3's one existing case, `THIN-BZ` with an empty book in
  `tests/test_pipeline.py`, is a SELL, which the rule deliberately leaves
  alone, and the fixture quotes' asks sit half a percent over the last. If
  any *row* of any golden file moves, the rules are wired more widely than
  this ADR says.
- Backtests do not apply rule 1. The band file is a point-in-time snapshot
  and today's list applied to five years of history is look-ahead bias in
  both directions. A per-date fetch into the cache is possible — the
  archive holds old files, `sec_list_11092025` fetches — but it is a
  separate decision and a separate ADR. Rule 2 *does* apply in a backtest,
  on today's series suffix, which is the same bias in a smaller dose: 27
  `-BZ` names in the universe, some of which were `EQ` for part of the
  history. Rule 3 needs a quote and a backtest has none.
- The band copy is a cache under `CACHE_DIR`, like the universe copy, and a
  `PLAN_ONLY` run refreshes it too. ADR-022's "writes nothing" is about the
  account's state, not about regenerable caches.
- GSM stays out of scope, with one hole recorded: GSM Stage III and IV
  names trade once a week but carry a 5% band, so the floor does not catch
  them. There are 4 such names in the universe and none is inside the
  cut-off, and Zerodha blocks fresh purchases at GSM Stage 3 and 4 at the
  broker, so a slip-through is rejected rather than filled.
- ST-ASM is invisible to all of this and harmlessly so: since September
  2024 it is a 100% margin requirement with no band change and no
  trade-to-trade, and a 100% margin is what a cash delivery buyer posts
  anyway.

## Alternatives Considered

### Option 1: Status quo, no tradability rule

**Pros:**

- No new dependency, no new settings, nothing to keep fresh.

**Cons:**

- The two best-ranked names in the market this week cannot be bought, and
  the strategy does not know it. Orders go out, sit for two minutes, and
  are cancelled — or worse, rest until a call auction nobody is watching.

### Option 2: Exclude every trade-to-trade series

The first shape this took: no `-BE`, `-BZ`, `-ST` name may be opened, using
`NO_MARKET_SERIES`, which `universe` defines and `execution` already uses to
choose the order type.

**Pros:**

- Free. No network, no cache, no staleness, no new failure mode — the
  series is in the Kite tradingsymbol the run already has, and it is the
  symbol the order is placed against.
- Catches 46 of the buy zone's names including all 6 that matter.

**Cons:**

- It bans the wrong thing. Trade-to-trade forbids intraday selling, which a
  weekly delivery rotation never does. Of the 46 T2T names inside the
  cut-off, 40 trade continuously at a 5% band and fill against a limit
  order perfectly well; excluding them forgoes 40 buyable names to catch 6.
- It leaves the actual barrier — the call auction — unnamed, so an
  `EQ`-series name banded to 2% is still bought. There is one in the
  universe today, `21STCENMGM`, and the series rule cannot see it. It
  survives here only as the no-copy fallback, where being stricter than the
  rule it stands in for is the point.

### Option 3: Drop banned names from the universe instead of disqualifying them

Filter in `get_universe`, so a 2% name is never scored and never appears.

**Pros:**

- Simplest possible wiring, and saves the candle fetch for 38 names.

**Cons:**

- It force-sells a holding the moment NSE bands it. A name that leaves the
  ranking is `unranked` and `exit_check` sells it — precisely when it has
  become impossible to sell. The exit would be attempted, not filled, and
  the run would report a sale it did not make.
- It moves the `pct_rank` denominator for a reason that has nothing to do
  with momentum, which is the problem ADR-033 was written to end.

### Option 4: Track ESM, GSM and ASM stages directly

Fetch the three surveillance lists and exclude on stage rather than on band.

**Pros:**

- Says what it means. "Not in ESM Stage II" is the actual rule.

**Cons:**

- Three lists instead of one, each behind the NSE web API rather than the
  archive — the archive paths for them 404 — so each needs cookie handling
  the archive fetch does not.
- Three stage ladders to track as SEBI revises them, and the ladders have
  been revised repeatedly. The band is downstream of all of it and moves
  when they move, which is the property worth having.

### Option 5: Fail closed when the band data cannot be had

No band file, no new positions that week.

**Pros:**

- Never buys blind. The simplest safe rule.

**Cons:**

- Turns an unreachable CSV into a skipped week of entries, and the fallback
  in the Decision gets nearly the same protection — 37 of 38 banded names
  are T2T — while still trading. Failing closed buys a little safety for a
  cost that recurs every time NSE's archive has a bad morning.

## Implementation Plan

1. **Prerequisites**: ADR-033 at Implemented, which it is.
2. **The band source**, one commit: the dated fetch with its walk-back, the
   `(Symbol, Series)` lookup, the last-good copy and the three-rung
   degradation, the `RunContext` field with the live source as its `None`
   default, and the static lookup injected in `conftest.py` and
   `test_golden.py` beside `StaticUniverse`. Tested against a fake fetch
   with no network — including the 404 walk-back, the stale copy, the
   no-copy fallback, and that a context with no injection reaches the live
   source and not a permissive default.
3. **The rules**, one commit: `price_band` and `non_compliant` at the head
   of `disqualification`, the tradability argument threaded through
   `evaluate`, the `MIN_PRICE_BAND_PCT` setting, the held-below-floor
   warning and its `exits.csv` column, and the resize top-up skip. Tests: a
   banded name ranked on its momentum and never bought, a banded *holding*
   held and reported, a `BZ` name disqualified and a `BE` name not, a
   resize increase skipped and a decrease allowed.
4. **The limit guard**, one commit: `MAX_ENTRY_SLIPPAGE_PCT`, the BUY-only
   check in `_sendable`, the empty-book BUY skip, and the reasons in
   `candidates.csv`. The existing `THIN-BZ` SELL test must pass unchanged.
5. **Settings and docs**: `.env.example` regenerated
   (`uv run python -m stocks_on_the_move.settings --example`), `ONBOARDING.md`
   section 3's filter list, and the file lifecycle for the new cache copy.
6. **Validation before Implemented.** Suite green and `uv run ty check`
   clean. Golden files moved by the `band` column of `exits.csv` and by
   **nothing else** — regenerated once, the diff reviewed and named in the
   commit body (ADR-009); any other movement, stop and find out why. A
   `PLAN_ONLY=1` run read by hand against the run of 2026-09-14: the six
   named symbols disqualified with `price_band`, `IL&FSENGG-BZ` with
   `non_compliant`, 39 `-BE` names still qualified, and `run.json` naming
   the rung the band fetch took and a band-file date one trading day before
   the run.

## References

- ADR-020 (the universe source and its last-good copy, which the band
  source mirrors), ADR-021 (pure rules over a snapshot and its parameters),
  ADR-022 (the plan run before the first live one), ADR-025 (the gap
  filter), ADR-033 (rank the universe, qualify for entry — this ADR adds
  two reasons to its chain), ADR-009 (the golden files)
- `src/stocks_on_the_move/rules.py` (`disqualification`, `evaluate`),
  `universe.py` (`series_of`, `base_symbol`, `NO_MARKET_SERIES`,
  `NseArchives`), `execution.py` (`_price_for`, `_sendable`),
  `pipeline.py` (`resize_positions`, `buy_candidates`)
- NSE, *Additional Surveillance Measure (ASM) — FAQs*:
  <https://nsearchives.nseindia.com/web/sites/default/files/inline-files/FAQs%20-%20Additional%20Surveillance%20Measure%20(ASM)_1.pdf>
  — the Long-term ASM stage ladder, and that GSM and T2T names are excluded
  from ASM shortlisting
- NSE, *Enhanced Surveillance Measure*:
  <https://www.nseindia.com/static/regulations/enhanced-surveillance-measure-esm>
  — Stage II is trade-for-trade at a 2% band under periodic call auction,
  market cap below ₹1000 crore, 90-day minimum retention
- Zerodha, *Surveillance indicators*:
  <https://support.zerodha.com/category/trading-and-markets/alerts-and-nudges/nudges/articles/surveillance-indicators>
  — which measures imply T2T, and that fresh purchases are blocked at the
  broker for GSM Stage 3 and 4
- The band file itself:
  `https://nsearchives.nseindia.com/content/equities/sec_list_DDMMYYYY.csv`
- The run this ADR argues from: `runs/2026-09-14/200710-paper`

## Implementation Status

Accepted by the owner on 2026-09-14, on the plan as written, with the three
open questions settled: `BZ` excluded, the limit-price guard in this ADR,
the staleness threshold at 10 days on the reasoning above.

## Notes

Open, for whoever tunes this: `MAX_ENTRY_SLIPPAGE_PCT` at `0.03` is a guess
and wants a few weeks of `orders.csv` behind it. And the run of 2026-09-14
raises a question this ADR does not answer — `MIN_VOLUME` is 10,000
*shares*, which at a ₹15 price is ₹150,000 of daily turnover against a
`MAX_WEIGHT` of 10% of equity. A turnover floor in rupees would be a better
liquidity rule than a share count, and would overlap this one; it is a
separate ADR.
