"""Tests for broker-confirmed fills (ADR-019): the wait, the booking rules, and where a position follows a fill."""

from __future__ import annotations

import csv
import logging

import pytest

from fakes import EVEN_WEEK_WEDNESDAY, FakeBroker, make_candles, trending_closes
from stocks_on_the_move.broker import Instrument, PaperBroker, Quote
from stocks_on_the_move.context import Fill, RunContext, build_token_cache, filled_qty
from stocks_on_the_move.execution import safe_buy, safe_sell
from stocks_on_the_move.ledger import init_cash_balance, read_portfolio, write_portfolio
from stocks_on_the_move.pipeline import prune_portfolio, raise_cash_if_needed, resize_positions, run
from stocks_on_the_move.rules import RankItem
from stocks_on_the_move.universe import StaticUniverse

LOG = "stocks_on_the_move"  # the package logger: fills and the pipeline log from different modules
TODAY = EVEN_WEEK_WEDNESDAY.date()
NIFTY = Instrument(256265, "NIFTY 50", "NSE", "INDICES", "EQ")
DRIFTS = {"AAA": 0.004, "BBB": 0.003, "CCC": 0.002, "DDD": 0.001, "EEE": 0.0005}


def ledger_rows(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def read_table(path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def rank(symbol: str, *, close: float = 100.0, ma100: float = 90.0) -> RankItem:
    return RankItem(symbol, 0.5, 0.3, 0.9, close, ma100)


def funded(ctx: RunContext, cash: float = 10_000.0, **ltps: float) -> RunContext:
    """Ledgers initialised, last prices set, cash forced to ``cash``."""
    init_cash_balance(ctx)
    broker = ctx.broker
    assert isinstance(broker, FakeBroker)
    broker.ltps.update({f"NSE:{sym}": price for sym, price in ltps.items()})
    ctx.portfolio.cash = cash
    return ctx


def polls(broker: FakeBroker) -> list[str]:
    return [order_id for name, order_id in broker.calls if name == "order_status"]


def sent(fill: Fill | None) -> Fill:
    """The fill of an order that was sent; fails the test when nothing was."""
    assert fill is not None
    return fill


def bull_market(drifts: dict[str, float]) -> FakeBroker:
    broker = FakeBroker()
    broker._instruments.append(NIFTY)
    broker.candles[NIFTY.token] = make_candles(trending_closes(260, start=20_000.0, daily=0.001), end=TODAY)
    for token, (sym, drift) in enumerate(drifts.items(), start=1):
        broker.add_equity(sym, token, trending_closes(260, daily=drift), end=TODAY)
    return broker


# ── the wait ─────────────────────────────────────────────────────────────


def test_a_fill_on_the_first_poll_never_sleeps(make_context):
    sleeps: list[float] = []
    ctx = make_context()
    ctx.sleep = sleeps.append
    funded(ctx, TCS=100.0)

    fill = sent(safe_buy(ctx, "TCS", 10))

    assert fill == Fill("TCS", "BUY", "FAKE-0001", "COMPLETE", 10, 10, 100.0)
    assert not fill.partial
    assert sleeps == [] and polls(ctx.broker) == ["FAKE-0001"]


def test_the_wait_polls_at_the_interval_until_the_status_is_terminal(make_context):
    sleeps: list[float] = []
    ctx = make_context(fill_poll_seconds=2.0, fill_timeout_seconds=120)
    ctx.sleep = sleeps.append
    funded(ctx, TCS=100.0)
    ctx.broker.script_fills("TCS", ("OPEN", 0, 0.0), ("OPEN", 4, 99.5), ("COMPLETE", 10, 99.75))

    fill = sent(safe_buy(ctx, "TCS", 10))

    assert (fill.status, fill.filled, fill.price) == ("COMPLETE", 10, 99.75)
    assert sleeps == [2.0, 2.0]  # between the three polls, none before the first
    (row,) = ctx.portfolio.orders
    assert (row["status"], row["filled_qty"], row["average_price"], row["polls"], row["waited_s"]) == (
        "COMPLETE",
        10,
        99.75,
        3,
        4.0,
    )


def test_the_timeout_cancels_and_books_what_filled(make_context, caplog):
    sleeps: list[float] = []
    ctx = make_context(fill_poll_seconds=1.0, fill_timeout_seconds=3)
    ctx.sleep = sleeps.append
    funded(ctx, TCS=100.0)
    ctx.broker.script_fills("TCS", ("OPEN", 4, 99.0))  # four filled, then nothing more, ever

    with caplog.at_level(logging.WARNING, logger=LOG):
        fill = sent(safe_buy(ctx, "TCS", 10))

    assert ("cancel_order", "FAKE-0001") in ctx.broker.calls
    assert (fill.status, fill.filled, fill.price, fill.partial) == ("CANCELLED", 4, 99.0, True)
    assert len(polls(ctx.broker)) == 4  # three within the timeout, one after the cancel
    assert sleeps == [1.0, 1.0, 1.0]
    assert "Order FAKE-0001 still OPEN after 3 poll(s) over 2s; cancelling" in caplog.text
    assert "BUY TCS: 4 of 10 filled (CANCELLED); booking the part that did" in caplog.text
    (row,) = ledger_rows(ctx.settings.trades_ledger_file)
    assert (row["qty"], row["price"]) == ("4", "99.0000")
    assert ctx.portfolio.orders[0]["waited_s"] == 3.0


def test_a_cancel_that_races_a_fill_books_the_fill(make_context):
    ctx = make_context(fill_poll_seconds=1.0, fill_timeout_seconds=2)
    funded(ctx, TCS=100.0)
    ctx.broker.script_fills("TCS", ("OPEN", 0, 0.0), ("OPEN", 0, 0.0), ("COMPLETE", 10, 100.5))

    fill = sent(safe_buy(ctx, "TCS", 10))

    assert ("cancel_order", "FAKE-0001") in ctx.broker.calls
    assert (fill.status, fill.filled, fill.price) == ("COMPLETE", 10, 100.5)
    (row,) = ledger_rows(ctx.settings.trades_ledger_file)
    assert (row["qty"], row["price"]) == ("10", "100.5000")


def test_an_order_that_will_neither_fill_nor_cancel_is_booked_with_a_loud_warning(make_context, caplog):
    class StubbornExchange(FakeBroker):
        def cancel_order(self, order_id: str) -> None:  # the cancel is accepted and ignored
            self.calls.append(("cancel_order", order_id))

    ctx = make_context(StubbornExchange(ltp={"NSE:TCS": 100.0}), fill_poll_seconds=1.0, fill_timeout_seconds=1)
    funded(ctx)
    ctx.broker.script_fills("TCS", ("OPEN", 3, 100.0))

    with caplog.at_level(logging.WARNING, logger=LOG):
        fill = sent(safe_buy(ctx, "TCS", 10))

    assert (fill.status, fill.filled) == ("OPEN", 3)
    assert len(polls(ctx.broker)) == 2
    assert "neither filled nor cancelled after 2 polls; the broker's order book is the truth" in caplog.text
    assert [r["qty"] for r in ledger_rows(ctx.settings.trades_ledger_file)] == ["3"]


def test_a_paper_run_waits_for_nothing(make_context):
    inner = FakeBroker(ltp={"NSE:TCS": 100.0})
    sleeps: list[float] = []
    ctx = make_context(PaperBroker(inner))
    ctx.sleep = sleeps.append
    init_cash_balance(ctx)
    ctx.portfolio.cash = 10_000.0

    fill = sent(safe_buy(ctx, "TCS", 10))

    assert (fill.order_id, fill.status, fill.filled, fill.price) == ("PAPER-0001", "COMPLETE", 10, 100.0)
    assert sleeps == [] and inner.orders == []


# ── booking ──────────────────────────────────────────────────────────────


def test_a_rejection_books_nothing_and_names_the_reason(make_context, caplog):
    ctx = make_context()
    funded(ctx, TCS=100.0)
    ctx.broker.script_fills("TCS", ("REJECTED", 0, 0.0, "Insufficient funds"))

    with caplog.at_level(logging.WARNING, logger=LOG):
        fill = sent(safe_buy(ctx, "TCS", 10))

    assert fill == Fill("TCS", "BUY", "FAKE-0001", "REJECTED", 10, 0, 0.0)
    assert filled_qty(fill) == 0 and filled_qty(None) == 0
    assert ledger_rows(ctx.settings.trades_ledger_file) == []
    assert ctx.portfolio.cash == 10_000.0
    assert "BUY TCS x10: nothing filled (REJECTED: Insufficient funds); nothing booked" in caplog.text
    assert ctx.portfolio.orders[0]["status_message"] == "Insufficient funds"


def test_a_fill_without_an_average_price_is_booked_at_the_price_seen(make_context, caplog):
    ctx = make_context()
    funded(ctx, TCS=100.0)
    ctx.broker.script_fills("TCS", ("COMPLETE", 10, 0.0))

    with caplog.at_level(logging.WARNING, logger=LOG):
        fill = sent(safe_buy(ctx, "TCS", 10))

    assert (fill.filled, fill.price) == (10, 100.0)
    assert "BUY TCS: 10 filled but no average price came back; booking at 100.00" in caplog.text
    assert ledger_rows(ctx.settings.trades_ledger_file)[0]["price"] == "100.0000"


def test_a_live_row_carries_the_average_price_and_no_slippage(make_context):
    ctx = make_context()
    funded(ctx, TCS=100.0)
    ctx.broker.script_fills("TCS", ("COMPLETE", 10, 100.4))
    ctx.paper = False

    fill = sent(safe_buy(ctx, "TCS", 10))

    assert fill.price == 100.4
    (row,) = ledger_rows(ctx.settings.trades_ledger_file)
    assert (row["price"], row["slippage_pct"]) == ("100.4000", "0.000000")
    assert ctx.portfolio.cash == pytest.approx(10_000 - 10 * 100.4 * (1 + ctx.settings.fees_pct))


def test_a_paper_row_keeps_the_configured_slippage(ctx):
    funded(ctx, TCS=100.0)
    safe_sell(ctx, "TCS", 10)
    (row,) = ledger_rows(ctx.settings.trades_ledger_file)
    assert row["slippage_pct"] == f"{ctx.settings.slippage_pct:.6f}"
    friction = ctx.settings.fees_pct + ctx.settings.slippage_pct
    assert ctx.portfolio.cash == pytest.approx(10_000 + 1_000 * (1 - friction))


def test_orders_csv_has_the_order_id_before_the_wait_and_the_verdict_after(make_context):
    class DyingLine(FakeBroker):
        def order_status(self, order_id: str):
            raise RuntimeError("connection reset")

    ctx = make_context(DyingLine(ltp={"NSE:TCS": 100.0}), artifacts=True)
    funded(ctx)

    with pytest.raises(RuntimeError):
        safe_buy(ctx, "TCS", 10)

    (row,) = read_table(ctx.artifacts.path / "orders.csv")
    assert (row["order_id"], row["status"], row["requested_qty"], row["filled_qty"]) == (
        "FAKE-0001",
        "PLACED",
        "10",
        "0",
    )
    assert ledger_rows(ctx.settings.trades_ledger_file) == []  # nothing booked before the verdict


def test_orders_csv_holds_every_order_and_its_verdict(make_context):
    ctx = make_context(artifacts=True, fill_poll_seconds=1.0, fill_timeout_seconds=1)
    funded(ctx, TCS=100.0)
    ctx.broker.quotes["NSE:IDEA-BE"] = Quote(last_price=10.0, best_bid=9.9, best_ask=10.1)
    ctx.broker.script_fills("IDEA-BE", ("OPEN", 40, 10.1))

    safe_buy(ctx, "TCS", 10)
    safe_buy(ctx, "IDEA-BE", 100)

    rows = read_table(ctx.artifacts.path / "orders.csv")
    assert [(r["symbol"], r["order_type"], r["limit_price"], r["status"], r["filled_qty"]) for r in rows] == [
        ("TCS", "MARKET", "", "COMPLETE", "10"),
        ("IDEA-BE", "LIMIT", "10.100000", "CANCELLED", "40"),
    ]
    assert [float(r["average_price"]) for r in rows] == [100.0, 10.1]
    assert [(r["polls"], r["waited_s"]) for r in rows] == [("1", "0.000000"), ("2", "1.000000")]
    assert [r["qty"] for r in ledger_rows(ctx.settings.trades_ledger_file)] == ["10", "40"]


# ── the five places a position follows a fill ────────────────────────────


def test_prune_books_a_partial_exit_and_keeps_the_remainder(make_context):
    broker = FakeBroker(ltp={"NSE:GONE": 100.0, "NSE:PART": 50.0}).script_fills("PART", ("OPEN", 3, 50.0))
    ctx = make_context(broker, artifacts=True, fill_poll_seconds=1.0, fill_timeout_seconds=1)
    funded(ctx)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"GONE": 10, "PART": 8}

    prune_portfolio(ctx, [rank("HELD")])  # neither holding is ranked, so both must go

    assert ctx.portfolio.positions == {"PART": 5}
    assert ctx.portfolio.sold == {"GONE", "PART"}  # a partial exit still bars a buy-back this run
    rows = {r["symbol"]: r for r in read_table(ctx.artifacts.path / "exits.csv")}
    assert (rows["GONE"]["decision"], rows["PART"]["decision"]) == ("SELL", "SELL:partial")
    assert (float(rows["GONE"]["price"]), float(rows["PART"]["price"])) == (100.0, 50.0)
    assert [(r["symbol"], r["qty"]) for r in ledger_rows(ctx.settings.trades_ledger_file)] == [
        ("GONE", "10"),
        ("PART", "3"),
    ]


def test_prune_keeps_a_holding_whose_exit_the_broker_rejected(make_context, caplog):
    broker = FakeBroker(ltp={"NSE:GONE": 100.0}).script_fills("GONE", ("REJECTED", 0, 0.0, "Market closed"))
    ctx = make_context(broker, artifacts=True)
    funded(ctx)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"GONE": 10}
    cash_before = ctx.portfolio.cash

    with caplog.at_level(logging.WARNING, logger=LOG):
        prune_portfolio(ctx, [rank("HELD")])

    assert ctx.portfolio.positions == {"GONE": 10} and ctx.portfolio.sold == set()
    assert ctx.portfolio.cash == cash_before
    (row,) = read_table(ctx.artifacts.path / "exits.csv")
    assert (row["decision"], row["price"]) == ("SKIP:no_fill", "")
    assert ledger_rows(ctx.settings.trades_ledger_file) == []
    assert read_table(ctx.artifacts.path / "orders.csv")[0]["status_message"] == "Market closed"
    assert "SELL GONE x10: nothing filled (REJECTED: Market closed)" in caplog.text


def test_resize_moves_the_quantity_by_what_filled(make_context):
    broker = FakeBroker()
    broker.add_equity("FAT", 1, trending_closes(120, start=100.0), end=TODAY)
    broker.add_equity("THIN", 2, trending_closes(120, start=100.0), end=TODAY)
    ctx = make_context(broker, artifacts=True, fill_poll_seconds=1.0, fill_timeout_seconds=1)
    funded(ctx, cash=100_000.0)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"FAT": 1000, "THIN": 1}
    price = broker.ltps["NSE:FAT"]
    broker.script_fills("FAT", ("OPEN", 7, price))  # seven of the sell-down fill, then the cancel
    broker.script_fills("THIN", ("OPEN", 2, price))  # two of the buy-up

    resize_positions(ctx, bull=True)

    assert ctx.portfolio.positions == {"FAT": 993, "THIN": 3}
    rows = {r["symbol"]: r for r in read_table(ctx.artifacts.path / "sizing.csv")}
    assert (rows["FAT"]["action"], rows["THIN"]["action"]) == ("SELL:partial", "BUY:partial")
    assert [(r["symbol"], r["qty"]) for r in ledger_rows(ctx.settings.trades_ledger_file)] == [
        ("FAT", "7"),
        ("THIN", "2"),
    ]


def test_resize_leaves_the_quantity_when_nothing_fills(make_context):
    broker = FakeBroker()
    broker.add_equity("FAT", 1, trending_closes(120, start=100.0), end=TODAY)
    broker.script_fills("FAT", ("REJECTED", 0, 0.0, "Holding not available"))
    ctx = make_context(broker, artifacts=True)
    funded(ctx, cash=100_000.0)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"FAT": 1000}

    resize_positions(ctx, bull=False)

    assert ctx.portfolio.positions == {"FAT": 1000}
    rows = {r["symbol"]: r for r in read_table(ctx.artifacts.path / "sizing.csv")}
    assert rows["FAT"]["action"] == "SKIP:no_fill"


def test_raise_cash_counts_only_what_filled_and_keeps_selling(make_context):
    broker = FakeBroker(ltp={"NSE:BEST": 100.0, "NSE:WORST": 100.0}).script_fills("WORST", ("OPEN", 2, 100.0))
    ctx = make_context(broker, fill_poll_seconds=1.0, fill_timeout_seconds=1)
    funded(ctx, cash=-500.0)
    ctx.portfolio.positions = {"BEST": 10, "WORST": 10}

    raise_cash_if_needed(ctx, [rank("BEST"), rank("WORST")])

    # WORST: 6 asked, 2 filled; the shortfall then costs BEST 4 shares instead of none
    assert ctx.portfolio.positions == {"BEST": 6, "WORST": 8}
    assert ctx.portfolio.cash > 0
    assert [(r["symbol"], r["qty"]) for r in ledger_rows(ctx.settings.trades_ledger_file)] == [
        ("WORST", "2"),
        ("BEST", "4"),
    ]


def test_kill_switch_keeps_the_remainder_of_a_partial_exit(make_context, caplog):
    broker = FakeBroker(ltp={"NSE:AAA": 100.0, "NSE:BBB": 50.0}).script_fills("BBB", ("OPEN", 1, 50.0))
    ctx = make_context(broker, kill_switch=True, fill_poll_seconds=1.0, fill_timeout_seconds=1)
    write_portfolio(ctx.settings.portfolio_file, {"AAA": 10, "BBB": 4})

    with caplog.at_level(logging.WARNING, logger=LOG):
        run(ctx)

    assert ctx.portfolio.positions == {"BBB": 3}
    assert read_portfolio(ctx.settings.out_file) == {"BBB": 3}
    assert "could not sell 1 position(s), still held: BBB x3" in caplog.text
    assert [(r["symbol"], r["qty"]) for r in ledger_rows(ctx.settings.trades_ledger_file)] == [
        ("AAA", "10"),
        ("BBB", "1"),
    ]


def test_buys_hold_what_filled_not_what_was_asked(make_context):
    broker = bull_market(DRIFTS)
    broker.script_fills("AAA", ("OPEN", 1, broker.ltps["NSE:AAA"]))  # one share of the top pick, then the cancel
    broker.script_fills("BBB", ("REJECTED", 0, 0.0, "Circuit limit"))
    ctx = make_context(broker, cut_off_pct=0.5, artifacts=True, fill_poll_seconds=1.0, fill_timeout_seconds=1)
    ctx.universe = StaticUniverse(DRIFTS)

    run(ctx)

    assert ctx.portfolio.positions == {"AAA": 1}
    candidates = {r["symbol"]: r for r in read_table(ctx.artifacts.path / "candidates.csv")}
    assert (candidates["AAA"]["decision"], candidates["BBB"]["decision"]) == ("BUY:partial", "SKIP:no_fill")
    assert read_portfolio(ctx.settings.out_file) == {"AAA": 1}
    orders = read_table(ctx.artifacts.path / "orders.csv")
    assert [(o["symbol"], o["status"], o["filled_qty"]) for o in orders] == [
        ("AAA", "CANCELLED", "1"),
        ("BBB", "REJECTED", "0"),
    ]


def test_an_exit_that_does_not_fill_leaves_less_cash_for_the_buys(make_context, tmp_path):
    files = ("portfolio_file", "out_file", "cash_ledger_file", "trades_ledger_file")

    def run_with(tag: str, *, exit_fills: bool) -> tuple[RunContext, int]:
        broker = bull_market(DRIFTS)
        broker.ltps["NSE:ZZZ"] = 100.0  # held and unranked, so step 7 sells it before step 11 buys
        if not exit_fills:
            broker.script_fills("ZZZ", ("REJECTED", 0, 0.0, "Market closed"))
        paths = {name: str(tmp_path / f"{tag}_{name}.csv") for name in files}
        ctx = make_context(broker, cut_off_pct=0.5, starting_cash=1_000.0, **paths)  # the exit is most of the money
        ctx.universe = StaticUniverse(DRIFTS)
        write_portfolio(ctx.settings.portfolio_file, {"ZZZ": 100})
        run(ctx)
        bought = sum(int(r["qty"]) for r in ledger_rows(ctx.settings.trades_ledger_file) if r["side"] == "BUY")
        return ctx, bought

    filled, bought_after_a_fill = run_with("filled", exit_fills=True)
    rejected, bought_after_a_rejection = run_with("rejected", exit_fills=False)

    assert "ZZZ" not in filled.portfolio.positions and rejected.portfolio.positions["ZZZ"] == 100
    assert bought_after_a_rejection < bought_after_a_fill
