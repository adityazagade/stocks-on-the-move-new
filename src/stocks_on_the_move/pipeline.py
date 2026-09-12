"""The weekly routine, step by step (ADR-020, ADR-021, ADR-022).

``run(ctx)`` is steps 2 to 12 over an assembled ``RunContext``. Each step
decides through the pure rules into ``TradeIntent``s, hands each to
``execution.trade``, which runs it through the context's executor and books
what filled, and writes its table through the context's artifacts. Cash flows
through the run in order: a step decides on the portfolio the previous step's
fills left. ``gather_snapshots`` is the one place the candle store is read.
``main()`` in ``momentum.py`` builds the context and calls ``run``.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from stocks_on_the_move.broker import Instrument
from stocks_on_the_move.context import (
    Fill,
    RunContext,
    TradeIntent,
    build_token_cache,
    filled_qty,
    strategy_params,
    token_of,
)
from stocks_on_the_move.execution import (
    ORDER_COLUMNS,
    gross_cost_for_buy,
    live_value,
    ltp_map,
    net_proceeds_for_sell,
    outcome,
    trade,
)
from stocks_on_the_move.indicators import Snapshot, SnapshotError
from stocks_on_the_move.ledger import TRADE_COLUMNS, init_cash_balance, read_portfolio, write_portfolio
from stocks_on_the_move.params import StrategyParams
from stocks_on_the_move.reporting import (
    CANDIDATE_COLUMNS,
    EXIT_COLUMNS,
    RANKING_COLUMNS,
    SIZING_COLUMNS,
    UNIVERSE_COLUMNS,
    ranking_rows,
)
from stocks_on_the_move.rules import ExitCheck, RankItem, Sizing, evaluate, exit_check, rank, regime, size
from stocks_on_the_move.universe import NseArchives, get_universe

logger = logging.getLogger(__name__)


# ── gathering: the one place the candle store is read (ADR-021) ──────────
def gather_snapshots(ctx: RunContext, instruments: Iterable[Instrument]) -> None:
    """One candle read and one ``Snapshot`` per instrument and per holding, into ``ctx.snapshots``.

    Symbols already gathered are left alone, so a step may call this with no
    instruments to be sure its holdings are covered. A fetch or a build that
    throws becomes a failed snapshot with the error on it, and a WARNING.
    """
    params = strategy_params(ctx)
    wanted: dict[str, int | None] = {i.tradingsymbol: i.token for i in instruments}
    for sym in ctx.portfolio.positions:
        wanted.setdefault(sym, None)
    for sym, tok in wanted.items():
        if sym in ctx.snapshots:
            continue
        try:
            token = token_of(ctx, sym, "NSE") if tok is None else tok
            frame = ctx.candles.get(token, params.history_days)
            ctx.snapshots[sym] = Snapshot.from_candles(sym, token, frame, params)
        except Exception as exc:
            logger.warning("%s skipped – %s: %s", sym, type(exc).__name__, exc)
            ctx.snapshots[sym] = Snapshot.failed(sym, tok or 0, exc)


def index_snapshot(ctx: RunContext) -> Snapshot:
    """The regime index's snapshot, over its own, longer window."""
    params = strategy_params(ctx)
    tok = token_of(ctx)
    frame = ctx.candles.get(tok, params.regime_ma_period)
    return Snapshot.from_candles(ctx.settings.index_symbol, tok, frame, params)


def rank_step(ctx: RunContext, universe: list[Instrument]) -> list[RankItem]:
    """Step 6: gather, evaluate every instrument into universe.csv, rank what passed."""
    params = strategy_params(ctx)
    gather_snapshots(ctx, universe)
    evaluations = [evaluate(ctx.snapshots[i.tradingsymbol], params) for i in universe]
    ctx.artifacts.write_table("universe", UNIVERSE_COLUMNS, [e.row() for e in evaluations])
    ranks = rank(evaluations)
    logger.info("Ranked universe: %d symbols", len(ranks))
    return ranks


def size_for(ctx: RunContext, sym: str, account_equity: float) -> Sizing:
    """The ATR size for a gathered symbol; raises like ``size`` does, or when nothing was gathered."""
    snap = ctx.snapshots.get(sym)
    if snap is None:
        raise LookupError(f"no snapshot for {sym}")
    return size(snap, account_equity, strategy_params(ctx))


def _describe(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, SnapshotError) else f"{type(exc).__name__}: {exc}"


def _reference(snap: Snapshot | None) -> float:
    """The candle close a decision was made at; ``nan`` when the step had none."""
    return snap.last if snap is not None and not math.isnan(snap.last) else math.nan


# ── 7 ▸ exits ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ExitDecision:
    """One holding's verdict at step 7: the rules that fired and the sell they imply, if any."""

    symbol: str
    qty: int
    ranked: RankItem | None
    pct_rank: float
    check: ExitCheck
    intent: TradeIntent | None


def decide_exits(
    positions: dict[str, int],
    ranks: list[RankItem],
    snapshots: dict[str, Snapshot],
    params: StrategyParams,
) -> list[ExitDecision]:
    """Step 7's decisions, pure: every holding against the exit rules (ADR-022)."""
    idx = {r.symbol: i for i, r in enumerate(ranks)}
    rmap = {r.symbol: r for r in ranks}
    total = len(ranks)
    decisions: list[ExitDecision] = []
    for sym, qty in positions.items():
        ranked = rmap.get(sym)
        pct = (idx[sym] + 1) / total if sym in idx else 1.0
        snap = snapshots.get(sym)
        cause = None
        if ranked is None and snap is not None:  # say which filter dropped a held name (ADR-025)
            cause = evaluate(snap, params).reason or "not_in_universe"
        check = exit_check(snap, ranked, pct, params, unranked_cause=cause)
        intent = None
        if check.sell:
            intent = TradeIntent(sym, "SELL", qty, "exit:" + ";".join(check.reasons), _reference(snap))
        decisions.append(ExitDecision(sym, qty, ranked, pct, check, intent))
    return decisions


def prune_portfolio(ctx: RunContext, ranks: list[RankItem]) -> None:
    """Sell names that violate exit rules; every holding's verdict goes to exits.csv. Guarded against empty ranking."""
    if not ranks:
        logger.warning("Ranking empty – skipping prune to avoid accidental liquidation")
        ctx.artifacts.write_table("exits", EXIT_COLUMNS, [])
        return

    pf = ctx.portfolio
    gather_snapshots(ctx, ())  # the run's rank step covered these; a direct call gets them here
    idx = {r.symbol: i for i, r in enumerate(ranks)}
    rows: list[dict[str, Any]] = []
    for d in decide_exits(dict(pf.positions), ranks, ctx.snapshots, strategy_params(ctx)):
        decision = "HOLD"
        price: float | None = None
        if d.intent is not None:
            fill = trade(ctx, d.intent, exit=True)  # even a partial exit bars a buy-back this run
            if fill is None:  # nothing was sent, so nothing changes (ADR-017)
                logger.warning(
                    "%s: exit wanted (%s) but no price came back; the holding stays",
                    d.symbol,
                    ";".join(d.check.reasons),
                )
            decision = outcome(d.intent, fill, not_sent="SKIP:no_price")
            price = fill.price if fill is not None and fill.filled > 0 else None
        rows.append(
            {
                "symbol": d.symbol,
                "qty": d.qty,
                "rank": idx[d.symbol] + 1 if d.symbol in idx else None,
                "pct_rank": d.pct_rank,
                "close": d.ranked.close if d.ranked else None,
                "ma100": d.ranked.ma100 if d.ranked else None,
                "stop_level": d.check.stop_level,
                "reasons": ";".join(d.check.reasons),
                "decision": decision,
                "price": price,
            }
        )
    ctx.artifacts.write_table("exits", EXIT_COLUMNS, rows)


# ── 9 ▸ size rebalance ───────────────────────────────────────────────────
def resize_positions(ctx: RunContext, bull: bool) -> None:
    """Every even ISO week (IST), rebalance sizes toward ATR targets (cash-aware); verdicts go to sizing.csv."""
    pf = ctx.portfolio
    if (ctx.now().isocalendar().week % 2) and not ctx.settings.force_resize:  # odd ISO week → skip
        logger.info("Size rebalance skipped (odd week, IST)")
        ctx.artifacts.write_table("sizing", SIZING_COLUMNS, [])
        ctx.artifacts.record(resize_performed=False)
        return
    ctx.artifacts.record(resize_performed=True)
    gather_snapshots(ctx, ())

    account_equity = pf.cash + live_value(ctx)

    rows: dict[str, dict[str, Any]] = {}
    sells: list[TradeIntent] = []
    buys: list[TradeIntent] = []
    for sym, qty in pf.positions.items():
        try:
            sizing = size_for(ctx, sym, account_equity)
        except Exception as exc:
            logger.warning("size calc error %s – %s", sym, _describe(exc))
            rows[sym] = {"symbol": sym, "qty": qty, "action": "SKIP:size_error"}
            continue
        diff = sizing.target_qty - qty
        rows[sym] = {
            "symbol": sym,
            "qty": qty,
            "price": sizing.price,
            "atr": sizing.atr,
            "risk_qty": sizing.risk_qty,
            "cap_qty": sizing.cap_qty,
            "target_qty": sizing.target_qty,
            "delta": diff,
            "action": "BUY" if diff > 0 else "SELL" if diff < 0 else "HOLD",
        }
        if diff > 0:
            buys.append(TradeIntent(sym, "BUY", diff, "resize", sizing.price))
        elif diff < 0:
            sells.append(TradeIntent(sym, "SELL", -diff, "resize", sizing.price))

    # 1️⃣ sell downs first
    for intent in sells:
        fill = trade(ctx, intent)  # the quantity moves by what filled, or not at all (ADR-017, ADR-019)
        rows[intent.symbol]["action"] = outcome(intent, fill)

    if not bull or pf.cash <= 0:
        for intent in buys:
            rows[intent.symbol]["action"] = "SKIP:bear" if not bull else "SKIP:no_cash"
        ctx.artifacts.write_table("sizing", SIZING_COLUMNS, rows.values())
        return

    prices = ltp_map(ctx.broker, [i.symbol for i in buys]) if buys else {}
    for intent in buys:
        price = prices.get(intent.symbol, 0.0)
        need = gross_cost_for_buy(ctx.settings, price, intent.quantity)
        if need > pf.cash + 1e-6:
            rows[intent.symbol]["action"] = "SKIP:no_cash"
            continue
        fill = trade(ctx, intent)
        rows[intent.symbol]["action"] = outcome(intent, fill)
    ctx.artifacts.write_table("sizing", SIZING_COLUMNS, rows.values())


# ── 3.5 ▸ kill switch, 8 ▸ cash for withdrawals ──────────────────────────
def liquidate_all(ctx: RunContext) -> None:
    """KILL SWITCH: sell every position in the portfolio."""
    pf = ctx.portfolio
    logger.warning("KILL SWITCH activated – liquidating all %d positions", len(pf.positions))
    for sym, qty in list(pf.positions.items()):
        trade(ctx, TradeIntent(sym, "SELL", qty, "kill_switch", math.nan))
    unsold = [f"{sym} x{qty}" for sym, qty in pf.positions.items()]  # nothing sent or nothing filled: it stays
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
        trade(ctx, TradeIntent(sym, "SELL", sell_qty, "raise_cash", price))
        need = -pf.cash  # update remaining need after whatever filled

    if pf.cash < 0:
        logger.warning("Could not fully raise cash for withdrawal. Short by %.2f", -pf.cash)
    else:
        logger.info("Raised cash for withdrawal. Cash now: %.2f", pf.cash)


# ── 11 ▸ new buys ────────────────────────────────────────────────────────
def buy_candidates(ctx: RunContext, ranks: list[RankItem], bull: bool, account_equity: float) -> None:
    """Step 11: open new positions down the ranking while the regime is bull and cash allows.

    Every ranked name visited gets a row in candidates.csv with the decision taken
    on it. One intent is decided at a time, because each fill changes the equity
    the next size is computed from.
    """
    s = ctx.settings
    pf = ctx.portfolio
    params = strategy_params(ctx)
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
                qty = size_for(ctx, r.symbol, account_equity).target_qty
            except Exception as exc:
                logger.warning("size calc error %s – %s", r.symbol, _describe(exc))
                row["decision"] = "SKIP:size_error"
                continue
            if qty < params.min_shares:
                row["decision"] = "SKIP:below_min_shares"
                continue
            cost_needed = gross_cost_for_buy(s, r.close, qty)
            if cost_needed > pf.cash + 1e-6:
                # buy as much as possible
                affordable_qty = int(math.floor(pf.cash / (r.close * (1.0 + s.fees_pct + s.slippage_pct))))
            else:
                affordable_qty = qty
            row.update(qty=affordable_qty, est_cost=gross_cost_for_buy(s, r.close, affordable_qty))
            if affordable_qty < params.min_shares:
                row["decision"] = "SKIP:no_cash"
                continue

            intent = TradeIntent(r.symbol, "BUY", affordable_qty, "new_position", r.close)
            fill = trade(ctx, intent)  # the position is what filled, not what was asked (ADR-019)
            row["decision"] = outcome(intent, fill)
            if filled_qty(fill):
                account_equity = pf.cash + live_value(ctx)  # update for subsequent picks
                row["cash_after"] = pf.cash
    else:
        logger.info("No new buys – bear regime or no cash.")
    ctx.artifacts.write_table("candidates", CANDIDATE_COLUMNS, candidates)


# ── the run ──────────────────────────────────────────────────────────────
def plan_summary(intents: Sequence[tuple[TradeIntent, Fill | None]]) -> str:
    """One line on what a plan decided: filled intents by reason, and how many had no price or cash."""
    filled = (intent.reason.split(":", 1)[0] for intent, fill in intents if filled_qty(fill) > 0)
    by_reason: Counter[str] = Counter(filled)
    unsent = sum(1 for _, fill in intents if fill is None)
    parts = ", ".join(f"{n} {reason}" for reason, n in sorted(by_reason.items())) or "nothing to do"
    tail = f"; {unsent} could not be priced or afforded" if unsent else ""
    return f"{len(intents)} intent(s): {parts}{tail}"


def _write_snapshot(ctx: RunContext) -> None:
    """Step 12's file, unless this is a plan, which writes no state (ADR-022)."""
    s = ctx.settings
    pf = ctx.portfolio
    if s.plan_only:
        logger.info("PLAN ONLY – %s not written; it would hold %s", s.out_file, dict(sorted(pf.positions.items())))
        logger.info("PLAN ONLY – nothing sent, nothing written. %s", plan_summary(pf.intents))
        return
    write_portfolio(s.out_file, pf.positions)


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
        _write_snapshot(ctx)
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
    trend = regime(index_snapshot(ctx), strategy_params(ctx))
    bull = trend.bull
    logger.info("Index %.2f vs 200-day MA %.2f → %s", trend.last, trend.ma200, "BULL" if bull else "BEAR")
    art.record(regime={"index_close": trend.last, "ma200": trend.ma200, "bull": bull})

    # 6) Gather one snapshot per instrument and holding, evaluate, rank
    universe = get_universe(ctx, symbols)
    ranks = rank_step(ctx, universe)
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
    _write_snapshot(ctx)
    equity_after = pf.cash + live_value(ctx)
    logger.info(
        "Done. Final → Equity: %.0f  | Cash: %.0f  | Holdings: %d",
        equity_after,
        pf.cash,
        len(pf.positions),
    )
    _finish(ctx, "completed", equity_after=equity_after)
