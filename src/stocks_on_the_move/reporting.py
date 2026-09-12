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


RANKING_COLUMNS = ["rank", "symbol", "pct_rank", "score", "annual_slope", "r2", "close", "ma100", "held"]


EXIT_COLUMNS = ["symbol", "qty", "rank", "pct_rank", "close", "ma100", "stop_level", "reasons", "decision", "price"]


SIZING_COLUMNS = ["symbol", "qty", "price", "atr", "risk_qty", "cap_qty", "target_qty", "delta", "action"]


CANDIDATE_COLUMNS = ["rank", "symbol", "pct_rank", "decision", "qty", "est_cost", "cash_after"]


def ranking_rows(ranks: Sequence[RankItem], held: Collection[str]) -> list[dict[str, Any]]:
    """One ``ranking.csv`` row per ranked name, best first; ``pct_rank`` maps best to near 0 and worst to 1."""
    total = len(ranks)
    return [
        {
            "rank": i + 1,
            "symbol": r.symbol,
            "pct_rank": (i + 1) / total,
            "score": r.score,
            "annual_slope": r.annual_slope,
            "r2": r.r2,
            "close": r.close,
            "ma100": r.ma100,
            "held": r.symbol in held,
        }
        for i, r in enumerate(ranks)
    ]
