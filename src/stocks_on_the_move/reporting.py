"""Column lists of the per-run artifact tables (ADR-006, ADR-020).

The trade columns live in ``ledger.py`` because they are also the trades
ledger's header; the order columns live in ``execution.py`` next to the code
that fills them.
"""

from __future__ import annotations

UNIVERSE_COLUMNS = ["symbol", "token", "status", "reason", "last", "ema100", "avg_vol_20", "atr", "atr_pct"]


RANKING_COLUMNS = ["rank", "symbol", "pct_rank", "score", "annual_slope", "r2", "close", "ema100", "held"]


EXIT_COLUMNS = ["symbol", "qty", "rank", "pct_rank", "close", "ema100", "stop_level", "reasons", "decision", "price"]


SIZING_COLUMNS = ["symbol", "qty", "price", "atr", "risk_qty", "cap_qty", "target_qty", "delta", "action"]


CANDIDATE_COLUMNS = ["rank", "symbol", "pct_rank", "decision", "qty", "est_cost", "cash_after"]
