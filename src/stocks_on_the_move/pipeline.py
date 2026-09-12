"""The weekly routine, step by step (ADR-020).

``run(ctx)`` is steps 2 to 12 over an assembled ``RunContext``; each step is a
function here that decides, trades through ``execution`` and writes its table
through the context's artifacts. ``main()`` in ``momentum.py`` builds the
context and calls ``run``.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from stocks_on_the_move.context import RunContext, build_token_cache, filled_qty
from stocks_on_the_move.execution import (
    ORDER_COLUMNS,
    gross_cost_for_buy,
    live_value,
    ltp_map,
    net_proceeds_for_sell,
    safe_buy,
    safe_sell,
)
from stocks_on_the_move.indicators import MIN_SHARES
from stocks_on_the_move.ledger import TRADE_COLUMNS, init_cash_balance, read_portfolio, write_portfolio
from stocks_on_the_move.reporting import (
    CANDIDATE_COLUMNS,
    EXIT_COLUMNS,
    RANKING_COLUMNS,
    SIZING_COLUMNS,
    ranking_rows,
)
from stocks_on_the_move.rules import RankItem, exit_reasons, index_trend, rank_universe, size_position
from stocks_on_the_move.universe import NseArchives, get_universe

logger = logging.getLogger(__name__)


def prune_portfolio(ctx: RunContext, ranks: list[RankItem]) -> None:
    """Sell names that violate exit rules; every holding's verdict goes to exits.csv. Guarded against empty ranking."""
    if not ranks:
        logger.warning("Ranking empty – skipping prune to avoid accidental liquidation")
        ctx.artifacts.write_table("exits", EXIT_COLUMNS, [])
        return

    pf = ctx.portfolio
    idx = {r.symbol: i for i, r in enumerate(ranks)}
    rmap = {r.symbol: r for r in ranks}
    total = len(ranks)
    rows: list[dict[str, Any]] = []
    for sym, qty in list(pf.positions.items()):
        rank = rmap.get(sym)
        pct = (idx[sym] + 1) / total if sym in idx else 1.0
        check = exit_reasons(ctx, rank, pct)
        fill = None
        decision = "HOLD"
        if check.sell:
            fill = safe_sell(ctx, sym, qty)
            got = filled_qty(fill)
            if fill is None:  # nothing was sent, so nothing changes (ADR-017)
                logger.warning(
                    "%s: exit wanted (%s) but no price came back; the holding stays", sym, ";".join(check.reasons)
                )
                decision = "SKIP:no_price"
            elif got == 0:  # sent, nothing filled; _place said why (ADR-019)
                decision = "SKIP:no_fill"
            else:
                pf.sold.add(sym)  # even a partial exit bars a buy-back this run
                if got == qty:
                    pf.positions.pop(sym)
                    decision = "SELL"
                else:
                    pf.positions[sym] = qty - got
                    decision = "SELL:partial"
        rows.append(
            {
                "symbol": sym,
                "qty": qty,
                "rank": idx[sym] + 1 if sym in idx else None,
                "pct_rank": pct,
                "close": rank.close if rank else None,
                "ema100": rank.ema100 if rank else None,
                "stop_level": check.stop_level,
                "reasons": ";".join(check.reasons),
                "decision": decision,
                "price": fill.price if fill is not None and fill.filled else None,
            }
        )
    ctx.artifacts.write_table("exits", EXIT_COLUMNS, rows)


def resize_positions(ctx: RunContext, bull: bool) -> None:
    """Every even ISO week (IST), rebalance sizes toward ATR targets (cash-aware); verdicts go to sizing.csv."""
    pf = ctx.portfolio
    if (ctx.now().isocalendar().week % 2) and not ctx.settings.force_resize:  # odd ISO week → skip
        logger.info("Size rebalance skipped (odd week, IST)")
        ctx.artifacts.write_table("sizing", SIZING_COLUMNS, [])
        ctx.artifacts.record(resize_performed=False)
        return
    ctx.artifacts.record(resize_performed=True)

    account_equity = pf.cash + live_value(ctx)

    rows: dict[str, dict[str, Any]] = {}
    to_up, to_down = {}, {}
    for sym, qty in pf.positions.items():
        try:
            size = size_position(ctx, sym, account_equity)
        except Exception as exc:
            logger.warning("size calc error %s – %s: %s", sym, type(exc).__name__, exc)
            rows[sym] = {"symbol": sym, "qty": qty, "action": "SKIP:size_error"}
            continue
        diff = size.target_qty - qty
        rows[sym] = {
            "symbol": sym,
            "qty": qty,
            "price": size.price,
            "atr": size.atr,
            "risk_qty": size.risk_qty,
            "cap_qty": size.cap_qty,
            "target_qty": size.target_qty,
            "delta": diff,
            "action": "BUY" if diff > 0 else "SELL" if diff < 0 else "HOLD",
        }
        if diff > 0:
            to_up[sym] = diff
        elif diff < 0:
            to_down[sym] = -diff

    # 1️⃣ sell downs first
    for sym, delta in to_down.items():
        fill = safe_sell(ctx, sym, delta)
        got = filled_qty(fill)
        if got == 0:  # nothing sent, or nothing filled: the quantity stays (ADR-017, ADR-019)
            rows[sym]["action"] = "SKIP:not_placed" if fill is None else "SKIP:no_fill"
            continue
        if got < delta:
            rows[sym]["action"] = "SELL:partial"
        pf.positions[sym] -= got
        if pf.positions[sym] == 0:
            pf.positions.pop(sym)
            pf.sold.add(sym)

    if not bull or pf.cash <= 0:
        for sym in to_up:
            rows[sym]["action"] = "SKIP:bear" if not bull else "SKIP:no_cash"
        ctx.artifacts.write_table("sizing", SIZING_COLUMNS, rows.values())
        return

    prices = ltp_map(ctx.broker, to_up.keys()) if to_up else {}
    for sym, delta in to_up.items():
        price = prices.get(sym, 0.0)
        need = gross_cost_for_buy(ctx.settings, price, delta)
        if need > pf.cash + 1e-6:
            rows[sym]["action"] = "SKIP:no_cash"
            continue
        fill = safe_buy(ctx, sym, delta)
        got = filled_qty(fill)
        if got == 0:
            rows[sym]["action"] = "SKIP:not_placed" if fill is None else "SKIP:no_fill"
            continue
        if got < delta:
            rows[sym]["action"] = "BUY:partial"
        pf.positions[sym] = pf.positions.get(sym, 0) + got
    ctx.artifacts.write_table("sizing", SIZING_COLUMNS, rows.values())


def liquidate_all(ctx: RunContext) -> None:
    """KILL SWITCH: sell every position in the portfolio."""
    pf = ctx.portfolio
    logger.warning("KILL SWITCH activated – liquidating all %d positions", len(pf.positions))
    unsold: list[str] = []
    for sym, qty in list(pf.positions.items()):
        got = filled_qty(safe_sell(ctx, sym, qty))
        if got == qty:
            pf.positions.pop(sym)
            continue
        if got:  # a partial exit leaves the remainder (ADR-019)
            pf.positions[sym] = qty - got
        unsold.append(f"{sym} x{qty - got}")  # nothing sent or nothing filled: the holding stays (ADR-017)
    if unsold:
        logger.warning("KILL SWITCH could not sell %d position(s), still held: %s", len(unsold), ", ".join(unsold))
    logger.warning("KILL SWITCH complete – %d position(s) remain, cash: %.2f", len(pf.positions), pf.cash)


def raise_cash_if_needed(ctx: RunContext, ranks: list[RankItem]) -> None:
    """If cash < 0 (withdrawal > cash), sell worst-ranked holdings to cover."""
    pf = ctx.portfolio
    if pf.cash >= 0:
        return
    need = -pf.cash + 1e-6

    idx = {r.symbol: i for i, r in enumerate(ranks)}

    def sort_key(sym: str) -> int:
        return idx.get(sym, 10**9)

    hold_syms = sorted(pf.positions.keys(), key=sort_key, reverse=True)
    prices = ltp_map(ctx.broker, hold_syms) if hold_syms else {}

    for sym in hold_syms:
        if need <= 0:
            break
        qty = pf.positions[sym]
        price = prices.get(sym, 0.0)
        if price <= 0:
            continue
        per_share = net_proceeds_for_sell(ctx.settings, price, 1)
        sell_qty = min(qty, int(math.ceil(need / per_share)))
        if sell_qty <= 0:
            continue
        got = filled_qty(safe_sell(ctx, sym, sell_qty))
        if got == 0:  # nothing sent or nothing filled, so the holding stays (ADR-017, ADR-019)
            continue
        pf.positions[sym] -= got
        if pf.positions[sym] == 0:
            pf.positions.pop(sym)
            pf.sold.add(sym)
        need = -pf.cash  # update remaining need after cash change

    if pf.cash < 0:
        logger.warning("Could not fully raise cash for withdrawal. Short by %.2f", -pf.cash)
    else:
        logger.info("Raised cash for withdrawal. Cash now: %.2f", pf.cash)


def buy_candidates(ctx: RunContext, ranks: list[RankItem], bull: bool, account_equity: float) -> None:
    """Step 11: open new positions down the ranking while the regime is bull and cash allows.

    Every ranked name visited gets a row in candidates.csv with the decision taken
    on it. Equity is recomputed after each fill, so later sizes see earlier buys.
    """
    s = ctx.settings
    pf = ctx.portfolio
    total = len(ranks)
    candidates: list[dict[str, Any]] = []
    if bull and pf.cash > 0:
        for idx, r in enumerate(ranks):
            pct_rank = (idx + 1) / total
            row: dict[str, Any] = {"rank": idx + 1, "symbol": r.symbol, "pct_rank": pct_rank}
            candidates.append(row)
            if pct_rank > s.cut_off_pct:
                row["decision"] = "SKIP:beyond_cutoff"
                continue
            if len(pf.positions) >= s.max_positions:
                logger.info("Max positions reached (%d) – stopping new buys", s.max_positions)
                row["decision"] = "SKIP:max_positions"
                break
            if r.symbol in pf.positions:
                row["decision"] = "SKIP:held"
                continue
            if r.symbol in pf.sold:
                row["decision"] = "SKIP:sold_this_run"
                continue
            try:
                qty = size_position(ctx, r.symbol, account_equity).target_qty
            except Exception as exc:
                logger.warning("size calc error %s – %s: %s", r.symbol, type(exc).__name__, exc)
                row["decision"] = "SKIP:size_error"
                continue
            if qty < MIN_SHARES:
                row["decision"] = "SKIP:below_min_shares"
                continue
            cost_needed = gross_cost_for_buy(s, r.close, qty)
            if cost_needed > pf.cash + 1e-6:
                # buy as much as possible
                affordable_qty = int(math.floor(pf.cash / (r.close * (1.0 + s.fees_pct + s.slippage_pct))))
            else:
                affordable_qty = qty
            row.update(qty=affordable_qty, est_cost=gross_cost_for_buy(s, r.close, affordable_qty))
            if affordable_qty < MIN_SHARES:
                row["decision"] = "SKIP:no_cash"
                continue

            fill = safe_buy(ctx, r.symbol, affordable_qty)
            got = filled_qty(fill)
            if got:
                pf.positions[r.symbol] = got  # what filled, not what was asked (ADR-019)
                # update for subsequent picks
                account_equity = pf.cash + live_value(ctx)
                row.update(decision="BUY" if got == affordable_qty else "BUY:partial", cash_after=pf.cash)
            else:
                row["decision"] = "SKIP:not_placed" if fill is None else "SKIP:no_fill"
    else:
        logger.info("No new buys – bear regime or no cash.")
    ctx.artifacts.write_table("candidates", CANDIDATE_COLUMNS, candidates)


def _finish(ctx: RunContext, status: str, equity_after: float) -> None:
    """The closing artifacts: the portfolio after, this run's trades, the closing numbers, the status."""
    pf = ctx.portfolio
    ctx.artifacts.write_rows("portfolio_after", sorted(pf.positions.items()))
    ctx.artifacts.write_table("trades", TRADE_COLUMNS, pf.trades)
    ctx.artifacts.write_table("orders", ORDER_COLUMNS, pf.orders)
    ctx.artifacts.record(
        cash_after=pf.cash, equity_after=equity_after, positions_after=dict(sorted(pf.positions.items()))
    )
    ctx.artifacts.finish(status)


def run(ctx: RunContext) -> None:
    """Steps 2 to 12 of the weekly routine, against an assembled context."""
    s = ctx.settings
    pf = ctx.portfolio
    art = ctx.artifacts

    # 2) Load current portfolio and ledgers
    pf.positions = read_portfolio(s.portfolio_file)
    init_cash_balance(ctx)
    art.write_rows("portfolio_before", sorted(pf.positions.items()))

    # 3) Preload instrument tokens once (no quote() here)
    build_token_cache(ctx)
    value_before = live_value(ctx)
    logger.info("Portfolio value at start: %.2f", value_before)
    art.record(
        cash_before=pf.cash,
        equity_before=pf.cash + value_before,
        positions_before=dict(sorted(pf.positions.items())),
    )

    # 3.5) KILL SWITCH – liquidate everything and exit
    if s.kill_switch:
        liquidate_all(ctx)
        write_portfolio(s.out_file, pf.positions)
        logger.warning("KILL SWITCH run finished. Final cash: %.2f", pf.cash)
        _finish(ctx, "completed", equity_after=pf.cash)
        return

    # 4) The symbols the strategy may hold (NSE archives unless the context says otherwise)
    source = ctx.universe if ctx.universe is not None else NseArchives(s)
    symbols = source.symbols()
    logger.info("Universe symbols loaded: %d", len(symbols))
    art.record(universe_size=len(symbols))
    if not symbols:
        logger.error("Empty universe – aborting run to avoid accidental actions")
        _finish(ctx, "aborted:empty_universe", equity_after=pf.cash + value_before)
        return

    # 5) Determine index regime (bull/bear)
    bull, idx_last, idx_ema = index_trend(ctx)
    logger.info("Index %.2f vs 200-EMA %.2f → %s", idx_last, idx_ema, "BULL" if bull else "BEAR")
    art.record(regime={"index_close": idx_last, "ema200": idx_ema, "bull": bull})

    # 6) Build & rank universe
    universe = get_universe(ctx, symbols)
    ranks = rank_universe(ctx, universe)
    total = len(ranks)  # maps best=0.0, worst=1.0
    art.record(ranked_count=total)
    art.write_table("ranking", RANKING_COLUMNS, ranking_rows(ranks, pf.positions))
    for r in ranks[:20]:
        logger.info("Top %s: score=%.4f ann=%.2f%% R²=%.2f", r.symbol, r.score, 100 * r.annual_slope, r.r2)

    # 7) Exits (with guard against empty ranks inside prune_portfolio)
    prune_portfolio(ctx, ranks)

    # 8) If withdrawals exceed available cash, raise cash by selling worst holdings
    raise_cash_if_needed(ctx, ranks)

    # 9) Size parity rebalance (every even ISO week in IST), cash-aware
    resize_positions(ctx, bull)

    # 10) Cash left after re-sizing
    cash_left = pf.cash
    account_equity = cash_left + live_value(ctx)
    logger.info(
        "After resize → Equity: %.0f  | Cash: %.0f  | Positions: %d", account_equity, cash_left, len(pf.positions)
    )

    # 11) New buys if bull regime and cash available; every ranked name visited goes to candidates.csv
    buy_candidates(ctx, ranks, bull, account_equity)

    # 12) Write next portfolio snapshot
    write_portfolio(s.out_file, pf.positions)
    equity_after = pf.cash + live_value(ctx)
    logger.info(
        "Done. Final → Equity: %.0f  | Cash: %.0f  | Holdings: %d",
        equity_after,
        pf.cash,
        len(pf.positions),
    )
    _finish(ctx, "completed", equity_after=equity_after)
