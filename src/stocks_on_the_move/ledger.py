"""The five state files: the portfolio snapshot, the two ledgers and the strategy state (ADR-020, ADR-027, ADR-029).

Cash is never stored; it is reconstructed from the ledgers on every run. A
trade reaches the ledger only through ``record_trade``.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from collections.abc import Sequence
from datetime import date
from typing import Any

from stocks_on_the_move.context import RunContext

logger = logging.getLogger(__name__)


TRADE_COLUMNS = ["timestamp", "side", "symbol", "qty", "price", "fees_pct", "slippage_pct", "cash_delta"]


def read_portfolio(path: str) -> dict[str, int]:
    """Read SYMBOL,QUANTITY rows from CSV into a dict."""
    pf: dict[str, int] = {}
    if os.path.isfile(path):
        with open(path, newline="") as f:
            for sym, qty in csv.reader(f):
                try:
                    pf[sym.strip().upper()] = int(qty)
                except ValueError:
                    logger.warning("Invalid line in %s: %s,%s", path, sym, qty)
    logger.info("Portfolio loaded – %d positions", len(pf))
    return pf


def _ensure_parent(path: str) -> None:
    """Create the directory a state file is written to; a fresh checkout has no runs/ yet (ADR-029)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_portfolio(path: str, pf: dict[str, int]) -> None:
    """Write the portfolio dict back to CSV (sorted for determinism)."""
    _ensure_parent(path)
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(sorted(pf.items()))
    logger.info("Portfolio written → %s (%d lines)", path, len(pf))


# Ledgers ------------------------------------------------------------------
def _ensure_csv(path: str, header: list[str]) -> None:
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        _ensure_parent(path)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


def _append_row(path: str, row: Sequence[Any]) -> None:
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow(row)


def append_cashflow(path: str, day: date, amount: float, note: str) -> None:
    """Append one dated deposit (+) or withdrawal (-) row; ENV_CASHFLOW and the console's form share it (ADR-031)."""
    _ensure_csv(path, ["date", "amount", "note"])
    _append_row(path, [day.isoformat(), f"{amount:.2f}", note])


def append_env_cashflow_if_any(ctx: RunContext) -> None:
    """If ENV_CASHFLOW!=0, append a dated row to cash ledger for today."""
    s = ctx.settings
    if abs(s.env_cashflow) < 1e-9:
        return
    append_cashflow(s.cash_ledger_file, ctx.now().date(), s.env_cashflow, s.cashflow_note)
    logger.info("Applied ENV_CASHFLOW: %+.2f (%s)", s.env_cashflow, s.cashflow_note)


def cash_from_cash_ledger(path: str) -> float:
    """Sum deposits/withdrawals from cash ledger."""
    if not os.path.isfile(path):
        return 0.0
    total = 0.0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                total += float(row.get("amount", "0").strip())
            except Exception:
                continue
    return total


def trades_cash_delta(path: str) -> float:
    """Sum cash impact from the trades ledger (already net of fees/slippage)."""
    if not os.path.isfile(path):
        return 0.0
    total = 0.0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                total += float(row.get("cash_delta", "0").strip())
            except Exception:
                continue
    return total


def init_cash_balance(ctx: RunContext) -> float:
    """Reconstruct the cash for this run from the ledgers and store it on the portfolio."""
    s = ctx.settings
    if s.plan_only:  # a plan counts the cash flow and writes nothing (ADR-022)
        if abs(s.env_cashflow) >= 1e-9:
            logger.info("PLAN ONLY – ENV_CASHFLOW %+.2f counted, not written", s.env_cashflow)
        ledger = cash_from_cash_ledger(s.cash_ledger_file) + s.env_cashflow
    else:
        _ensure_csv(s.cash_ledger_file, ["date", "amount", "note"])
        _ensure_csv(s.trades_ledger_file, TRADE_COLUMNS)
        append_env_cashflow_if_any(ctx)
        ledger = cash_from_cash_ledger(s.cash_ledger_file)
    trades = trades_cash_delta(s.trades_ledger_file)
    ctx.portfolio.cash = s.starting_cash + ledger + trades
    logger.info(
        "Cash reconstructed: START=%.2f, ledger=%.2f, trades=%.2f → CASH=%.2f",
        s.starting_cash,
        ledger,
        trades,
        ctx.portfolio.cash,
    )
    return ctx.portfolio.cash


def record_trade(ctx: RunContext, side: str, symbol: str, qty: int, price: float) -> float:
    """Write a trade to the trades ledger and return its cash delta (positive when cash increases).

    The portfolio is not touched here; ``Portfolio.apply`` takes the delta
    (ADR-022). In plan mode the row goes to this run's table only, never to
    the ledger file.
    """
    s = ctx.settings
    if qty <= 0 or price <= 0:
        return 0.0
    side = side.upper()
    # A paper price is the price seen, so the slippage estimate applies; a live fill's average
    # price already contains whatever slippage there was (ADR-019). Fees sit outside both.
    slippage = s.slippage_pct if ctx.paper else 0.0
    if side == "BUY":
        cash_delta = -qty * price * (1.0 + s.fees_pct + slippage)
    elif side == "SELL":
        cash_delta = +qty * price * (1.0 - s.fees_pct - slippage)
    else:
        raise ValueError("side must be BUY or SELL")
    values = [
        ctx.now().isoformat(timespec="seconds"),
        side,
        symbol.upper(),
        qty,
        f"{price:.4f}",
        f"{s.fees_pct:.6f}",
        f"{slippage:.6f}",
        f"{cash_delta:.2f}",
    ]
    if not s.plan_only:
        _append_row(s.trades_ledger_file, values)
    ctx.portfolio.trades.append(dict(zip(TRADE_COLUMNS, values, strict=True)))
    ctx.artifacts.write_table("trades", TRADE_COLUMNS, ctx.portfolio.trades)
    return cash_delta


# ── the strategy's own state (ADR-027) ───────────────────────────────────
def load_state(path: str) -> dict[str, Any]:
    """``strategy_state.json`` as a dict; empty when the file is missing, with a WARNING when it is unreadable."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            state = json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring the state file %s: %s", path, exc)
        return {}
    if not isinstance(state, dict):
        logger.warning("Ignoring the state file %s: not an object", path)
        return {}
    return state


def save_state(path: str, state: dict[str, Any]) -> None:
    _ensure_parent(path)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def last_resize_date(state: dict[str, Any]) -> date | None:
    """The date of the last size rebalance the state records, or ``None``."""
    raw = state.get("last_resize_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        logger.warning("Ignoring last_resize_date %r: not a date", raw)
        return None
