"""Column lists of the per-run artifact tables, and the row builder for the ranking (ADR-006, ADR-020).

The trade columns live in ``ledger.py`` because they are also the trades
ledger's header; the order columns live in ``execution.py`` next to the code
that fills them.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stocks_on_the_move.rules import RankItem

UNIVERSE_COLUMNS = ["symbol", "token", "status", "reason", "last", "ma100", "avg_vol_20", "atr", "atr_pct", "max_gap"]


RANKING_COLUMNS = [
    "rank",
    "symbol",
    "pct_rank",
    "score",
    "annual_slope",
    "r2",
    "close",
    "ma100",
    "qualified",
    "reason",
    "held",
]


# `band` is the holding's daily price band in percent, `inf` for No Band, blank when unknown (ADR-034)
EXIT_COLUMNS = [
    "symbol",
    "qty",
    "rank",
    "pct_rank",
    "close",
    "ma100",
    "band",
    "stop_level",
    "reasons",
    "decision",
    "price",
]


SIZING_COLUMNS = ["symbol", "qty", "price", "atr", "risk_qty", "cap_qty", "target_qty", "delta", "action"]


CANDIDATE_COLUMNS = ["rank", "symbol", "pct_rank", "decision", "qty", "target_qty", "est_cost", "cash_after"]


def ranking_rows(
    ranks: Sequence[RankItem],
    held: Collection[str],
    unrankable: Sequence[tuple[str, str]] = (),
) -> list[dict[str, Any]]:
    """``ranking.csv``: the ranked names best first, then the ones with no score at all (ADR-033).

    ``pct_rank`` maps best to near 0 and worst to 1 over the ranked names.
    ``qualified`` says whether the entry filters let a name be bought and
    ``reason`` names the first it failed. The tail carries a symbol and a
    reason and nothing else: rank, pct_rank and score stay blank, because
    there was no score to measure a position from.
    """
    total = len(ranks)
    rows = [
        {
            "rank": i + 1,
            "symbol": r.symbol,
            "pct_rank": (i + 1) / total,
            "score": r.score,
            "annual_slope": r.annual_slope,
            "r2": r.r2,
            "close": r.close,
            "ma100": r.ma100,
            "qualified": r.qualified,
            "reason": r.reason,
            "held": r.symbol in held,
        }
        for i, r in enumerate(ranks)
    ]
    rows += [{"symbol": sym, "qualified": False, "reason": reason, "held": sym in held} for sym, reason in unrankable]
    return rows
