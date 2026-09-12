"""Tests for trade intents, the one bookkeeping path and plan mode (ADR-022)."""

from __future__ import annotations

import csv
import json
import logging
import math
from datetime import datetime

from fakes import EVEN_WEEK_WEDNESDAY, FakeBroker, make_candles, trending_closes
from stocks_on_the_move import momentum as m
from stocks_on_the_move.broker import Instrument, Side
from stocks_on_the_move.context import Fill, Portfolio, TradeIntent
from stocks_on_the_move.execution import BrokerExecutor, PlanExecutor, executor_for, outcome, safe_buy
from stocks_on_the_move.ledger import write_portfolio
from stocks_on_the_move.params import StrategyParams
from stocks_on_the_move.pipeline import decide_exits, plan_summary, run
from stocks_on_the_move.rules import RankItem
from stocks_on_the_move.universe import StaticUniverse

TODAY = EVEN_WEEK_WEDNESDAY.date()
NIFTY = Instrument(256265, "NIFTY 50", "NSE", "INDICES", "EQ")
DRIFTS = {"AAA": 0.004, "BBB": 0.003, "CCC": 0.002, "DDD": 0.001, "EEE": 0.0005}
LOG = "stocks_on_the_move"


def read_table(path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def bull_market(drifts: dict[str, float]) -> FakeBroker:
    broker = FakeBroker()
    broker._instruments.append(NIFTY)
    broker.candles[NIFTY.token] = make_candles(trending_closes(260, start=20_000.0, daily=0.001), end=TODAY)
    for token, (sym, drift) in enumerate(drifts.items(), start=1):
        broker.add_equity(sym, token, trending_closes(260, daily=drift), end=TODAY)
    return broker


def fill(symbol: str, side: Side, requested: int, filled: int, price: float, status: str = "COMPLETE") -> Fill:
    return Fill(symbol, side, "1", status, requested, filled, price)


# ── the one bookkeeping path ─────────────────────────────────────────────


def test_apply_moves_a_position_by_what_filled_and_takes_the_cash_delta():
    pf = Portfolio(cash=1_000.0)
    pf.apply(fill("TCS", "BUY", 10, 10, 100.0), -1_001.5)
    assert pf.positions == {"TCS": 10} and pf.cash == -1.5
    pf.apply(fill("TCS", "BUY", 5, 2, 100.0, "CANCELLED"), -200.3)  # a partial buy adds what filled
    assert pf.positions == {"TCS": 12}
    pf.apply(fill("TCS", "SELL", 12, 4, 100.0, "CANCELLED"), 399.4)  # a partial sell leaves the rest
    assert pf.positions == {"TCS": 8} and pf.sold == set()
    pf.apply(fill("TCS", "SELL", 8, 8, 100.0), 798.8)  # sold to zero: gone, and marked sold
    assert pf.positions == {} and pf.sold == {"TCS"}
    assert pf.cash == -1.5 - 200.3 + 399.4 + 798.8


def test_apply_ignores_an_empty_fill_and_an_exit_marks_even_a_partial_sale_sold():
    pf = Portfolio(positions={"X": 10}, cash=0.0)
    pf.apply(fill("X", "SELL", 10, 0, 0.0, "REJECTED"), 0.0)
    assert pf.positions == {"X": 10} and pf.sold == set() and pf.cash == 0.0
    pf.apply(fill("X", "SELL", 10, 3, 50.0, "CANCELLED"), 149.7, exit=True)
    assert pf.positions == {"X": 7} and pf.sold == {"X"}


def test_outcome_names_what_became_of_an_intent():
    intent = TradeIntent("X", "SELL", 10, "exit:unranked", 100.0)
    assert outcome(intent, None) == "SKIP:not_placed"
    assert outcome(intent, None, not_sent="SKIP:no_price") == "SKIP:no_price"
    assert outcome(intent, fill("X", "SELL", 10, 0, 0.0, "REJECTED")) == "SKIP:no_fill"
    assert outcome(intent, fill("X", "SELL", 10, 4, 99.0, "CANCELLED")) == "SELL:partial"
    assert outcome(intent, fill("X", "SELL", 10, 10, 99.0)) == "SELL"
    assert outcome(TradeIntent("Y", "BUY", 3, "new_position", 1.0), fill("Y", "BUY", 3, 3, 1.0)) == "BUY"


def test_a_direct_trade_records_its_intent_and_books_through_apply(ctx):
    ctx.broker.ltps["NSE:TCS"] = 100.0
    ctx.portfolio.cash = 10_000.0
    got = safe_buy(ctx, "TCS", 10)
    assert got is not None and ctx.portfolio.positions == {"TCS": 10}
    ((intent, recorded),) = ctx.portfolio.intents
    assert (intent.symbol, intent.side, intent.quantity, intent.reason) == ("TCS", "BUY", 10, "direct")
    assert math.isnan(intent.reference_price) and recorded == got


# ── deciding without trading ─────────────────────────────────────────────


def test_decide_exits_is_pure_and_names_the_rules():
    ranks = [RankItem(sym, 0.5, 0.3, 0.9, 100.0, 90.0) for sym in ("KEEP", "B", "C", "D", "E")]  # KEEP is top 20 %
    gone, keep = decide_exits({"GONE": 10, "KEEP": 5}, ranks, {}, StrategyParams())
    assert gone.intent is not None
    assert (gone.intent.symbol, gone.intent.side, gone.intent.quantity) == ("GONE", "SELL", 10)
    assert gone.intent.reason == "exit:unranked" and math.isnan(gone.intent.reference_price)
    assert gone.check.reasons == ("unranked",) and gone.pct_rank == 1.0
    assert keep.intent is None and keep.check.reasons == () and keep.pct_rank == 0.2


def test_plan_summary_counts_filled_intents_by_reason():
    intents = [
        (TradeIntent("A", "SELL", 1, "exit:unranked", 1.0), fill("A", "SELL", 1, 1, 1.0)),
        (TradeIntent("B", "SELL", 1, "exit:rank_cutoff", 1.0), fill("B", "SELL", 1, 1, 1.0)),
        (TradeIntent("C", "BUY", 1, "new_position", 1.0), fill("C", "BUY", 1, 1, 1.0)),
        (TradeIntent("D", "BUY", 1, "new_position", 1.0), None),
    ]
    assert plan_summary(intents) == "4 intent(s): 2 exit, 1 new_position; 1 could not be priced or afforded"
    assert plan_summary([]) == "0 intent(s): nothing to do"


# ── the executor ─────────────────────────────────────────────────────────


def test_the_executor_is_the_contexts_or_follows_the_settings(make_context):
    assert isinstance(executor_for(make_context()), BrokerExecutor)
    assert isinstance(executor_for(make_context(plan_only=True)), PlanExecutor)

    class Custom:
        def execute(self, intent):
            return None

    ctx = make_context()
    ctx.executor = Custom()
    assert isinstance(executor_for(ctx), Custom)


# ── plan mode ────────────────────────────────────────────────────────────


def test_a_plan_run_decides_everything_sends_nothing_and_writes_no_state(make_context, caplog, tmp_path):
    broker = bull_market(DRIFTS)
    broker.ltps["NSE:ZZZ"] = 100.0  # held, unranked: the exit the plan will decide
    ctx = make_context(broker, plan_only=True, cut_off_pct=0.5, env_cashflow=500.0, artifacts=True)
    ctx.universe = StaticUniverse(DRIFTS)
    write_portfolio(ctx.settings.portfolio_file, {"ZZZ": 100})
    s = ctx.settings

    with caplog.at_level(logging.INFO, logger=LOG):
        run(ctx)

    # nothing reached the broker, nothing reached the state files
    assert broker.orders == []
    assert not (tmp_path / "trades_ledger.csv").exists() and not (tmp_path / "cash_ledger.csv").exists()
    assert not (tmp_path / "next_portfolio.csv").exists()
    # the decisions happened, in memory
    assert "ZZZ" not in ctx.portfolio.positions and {"AAA", "BBB"} <= set(ctx.portfolio.positions)
    orders = read_table(ctx.artifacts.path / "orders.csv")
    assert {o["status"] for o in orders} == {"PLANNED"} and [o["symbol"] for o in orders][0] == "ZZZ"
    assert [t["symbol"] for t in read_table(ctx.artifacts.path / "trades.csv")] == [o["symbol"] for o in orders]
    meta = json.loads((ctx.artifacts.path / "run.json").read_text())
    assert meta["mode"] == "plan" and meta["status"] == "completed"
    assert meta["cash_before"] == s.starting_cash + 500.0  # the cash flow counted, not written
    assert "PLAN  SELL ZZZ" in caplog.text and "PLAN  BUY AAA" in caplog.text
    assert "PLAN ONLY – nothing sent, nothing written. " in caplog.text
    assert "1 exit, 2 new_position" in caplog.text


def test_a_plan_wins_over_execution_and_the_kill_switch_plans_the_liquidation(make_context, tmp_path):
    broker = FakeBroker(ltp={"NSE:AAA": 100.0, "NSE:BBB": 50.0})
    ctx = make_context(broker, plan_only=True, kill_switch=True, allow_kite_execution=True, artifacts=True)
    write_portfolio(ctx.settings.portfolio_file, {"AAA": 10, "BBB": 4})

    run(ctx)

    assert m.run_mode(ctx.settings) == "plan" and ctx.artifacts.path.name.endswith("-plan")
    assert broker.orders == [] and ctx.portfolio.positions == {}
    assert not (tmp_path / "next_portfolio.csv").exists()
    assert [(i.reason, i.side) for i, _ in ctx.portfolio.intents] == [("kill_switch", "SELL"), ("kill_switch", "SELL")]
    assert {o["status"] for o in read_table(ctx.artifacts.path / "orders.csv")} == {"PLANNED"}


def test_the_weekday_guard_lets_a_plan_and_a_kill_switch_through(make_settings):
    wednesday = datetime(2026, 9, 16, 10, 0)
    thursday = datetime(2026, 9, 17, 10, 0)
    assert m.is_trading_day(make_settings(), wednesday) is True
    assert m.is_trading_day(make_settings(), thursday) is False
    assert m.is_trading_day(make_settings(plan_only=True), thursday) is True
    assert m.is_trading_day(make_settings(kill_switch=True), thursday) is True
    assert m.run_mode(make_settings(kill_switch=True)) == "kill" and m.run_mode(make_settings()) == "paper"
