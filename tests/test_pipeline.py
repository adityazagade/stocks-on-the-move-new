"""The strategy above the pure helpers, run against FakeBroker and a frozen clock (ADR-008)."""

from __future__ import annotations

import csv
import json
import logging

import pytest

from fakes import EVEN_WEEK_WEDNESDAY, ODD_WEEK_WEDNESDAY, FakeBroker, make_candles, trending_closes
from stocks_on_the_move.broker import Instrument, Order, Quote
from stocks_on_the_move.context import Fill, build_token_cache, token_of
from stocks_on_the_move.execution import ltp_map, safe_buy, safe_sell
from stocks_on_the_move.ledger import TRADE_COLUMNS, init_cash_balance, read_portfolio, write_portfolio
from stocks_on_the_move.pipeline import (
    gather_snapshots,
    prune_portfolio,
    raise_cash_if_needed,
    rank_step,
    resize_positions,
    run,
)
from stocks_on_the_move.reporting import EXIT_COLUMNS, SIZING_COLUMNS
from stocks_on_the_move.rules import RankItem
from stocks_on_the_move.universe import StaticUniverse

TODAY = EVEN_WEEK_WEDNESDAY.date()
NIFTY = Instrument(256265, "NIFTY 50", "NSE", "INDICES", "EQ")
LOG = "stocks_on_the_move"  # the package logger: the pipeline's lines come from several modules now


def ledger_rows(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def rank(symbol: str, *, close: float = 100.0, ma100: float = 90.0) -> RankItem:
    return RankItem(symbol, 0.5, 0.3, 0.9, close, ma100)


def booked(fill: Fill | None) -> float:
    """The price a trade was booked at; fails the test when nothing was sent (ADR-019)."""
    assert fill is not None
    return fill.price


# ── prices and tokens ────────────────────────────────────────────────────


def test_ltp_map_strips_the_exchange_prefix(ctx):
    ctx.broker.ltps.update({"NSE:TCS": 100.0, "NSE:INFY": 50.0})
    assert ltp_map(ctx.broker, ["TCS", "INFY", "NOPE"]) == {"TCS": 100.0, "INFY": 50.0}
    assert ltp_map(ctx.broker, []) == {}
    assert not ctx.broker.calls[-1:] or ctx.broker.calls[-1][0] == "ltp"


def test_token_of_uses_the_cache_then_the_instruments_then_fails(ctx):
    ctx.broker.add_equity("TCS", 11, [100, 101], end=TODAY)
    ctx.broker._instruments.append(NIFTY)
    build_token_cache(ctx)
    assert ctx.tokens["NSE:TCS"] == 11
    assert token_of(ctx, "TCS", "NSE") == 11
    assert token_of(ctx) == NIFTY.token  # the regime index by default
    with pytest.raises(KeyError, match="NSE:NOPE"):
        token_of(ctx, "NOPE", "NSE")


# ── orders ───────────────────────────────────────────────────────────────


def test_market_buy_books_the_trade_the_cash_and_the_ledger_row(ctx):
    init_cash_balance(ctx)
    ctx.broker.ltps["NSE:TCS"] = 100.0
    ctx.portfolio.cash = 10_000.0

    assert booked(safe_buy(ctx, "TCS", 10)) == 100.0

    assert ctx.broker.orders == [Order("TCS", "BUY", 10, "MARKET")]
    friction = ctx.settings.fees_pct + ctx.settings.slippage_pct
    assert ctx.portfolio.cash == pytest.approx(10_000 - 1_000 * (1 + friction))
    (row,) = ledger_rows(ctx.settings.trades_ledger_file)
    assert (row["side"], row["symbol"], row["qty"], row["price"]) == ("BUY", "TCS", "10", "100.0000")
    assert row["timestamp"] == EVEN_WEEK_WEDNESDAY.isoformat(timespec="seconds")


def test_buy_is_refused_when_cash_is_short(ctx, caplog):
    init_cash_balance(ctx)
    ctx.broker.ltps["NSE:TCS"] = 100.0
    ctx.portfolio.cash = 500.0
    with caplog.at_level(logging.INFO, logger=LOG):
        assert safe_buy(ctx, "TCS", 10) is None
    assert ctx.broker.orders == []
    assert ctx.portfolio.cash == 500.0
    assert "Not enough cash" in caplog.text


def test_market_sell_credits_cash_net_of_friction(ctx):
    init_cash_balance(ctx)
    ctx.broker.ltps["NSE:TCS"] = 100.0
    ctx.portfolio.cash = 0.0
    assert booked(safe_sell(ctx, "TCS", 10)) == 100.0
    assert ctx.broker.orders == [Order("TCS", "SELL", 10, "MARKET")]
    friction = ctx.settings.fees_pct + ctx.settings.slippage_pct
    assert ctx.portfolio.cash == pytest.approx(1_000 * (1 - friction))


def test_no_market_series_trade_with_limit_orders_at_the_top_of_the_book(ctx):
    init_cash_balance(ctx)
    ctx.broker.quotes["NSE:IDEA-BE"] = Quote(last_price=10.0, best_bid=9.9, best_ask=10.1)
    ctx.broker.quotes["NSE:THIN-BZ"] = Quote(last_price=5.0, best_bid=None, best_ask=None)

    assert booked(safe_buy(ctx, "IDEA-BE", 100)) == 10.1
    assert booked(safe_sell(ctx, "IDEA-BE", 100)) == 9.9
    assert booked(safe_sell(ctx, "THIN-BZ", 10)) == 5.0  # empty book: last price

    assert ctx.broker.orders == [
        Order("IDEA-BE", "BUY", 100, "LIMIT", limit_price=10.1),
        Order("IDEA-BE", "SELL", 100, "LIMIT", limit_price=9.9),
        Order("THIN-BZ", "SELL", 10, "LIMIT", limit_price=5.0),
    ]


def test_a_trade_without_a_price_is_skipped_with_a_warning(ctx, caplog):
    init_cash_balance(ctx)
    with caplog.at_level(logging.WARNING, logger=LOG):
        assert safe_buy(ctx, "GHOST", 5) is None
        assert safe_sell(ctx, "GHOST", 5) is None
    assert ctx.broker.orders == []
    assert caplog.text.count("No price for GHOST") == 2


def test_tiny_quantities_are_ignored(ctx):
    assert safe_buy(ctx, "TCS", 0) is None
    assert safe_sell(ctx, "TCS", 0) is None
    assert ctx.broker.calls == []


def test_trade_lines_keep_the_paper_label(ctx, caplog):
    init_cash_balance(ctx)
    ctx.broker.ltps["NSE:TCS"] = 100.0
    with caplog.at_level(logging.INFO, logger=LOG):
        safe_buy(ctx, "TCS", 1)
        ctx.paper = False
        safe_sell(ctx, "TCS", 1)
    assert "PAPER BUY TCS" in caplog.text
    assert "SELL TCS" in caplog.text and "PAPER SELL" not in caplog.text


# ── exits ────────────────────────────────────────────────────────────────


def test_prune_sells_unranked_holdings_and_skips_on_an_empty_ranking(make_context, caplog):
    broker = FakeBroker()
    broker.add_equity("KEEP", 1, trending_closes(120, daily=0.002), end=TODAY)
    broker.add_equity("DROP", 2, trending_closes(120, daily=0.002), end=TODAY)
    ctx = make_context(broker, cut_off_pct=1.0)
    init_cash_balance(ctx)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"KEEP": 5, "DROP": 3}

    prune_portfolio(ctx, [rank("KEEP")])

    assert [(o.symbol, o.side, o.quantity) for o in broker.orders] == [("DROP", "SELL", 3)]
    assert ctx.portfolio.positions == {"KEEP": 5}
    assert ctx.portfolio.sold == {"DROP"}

    with caplog.at_level(logging.WARNING, logger=LOG):
        prune_portfolio(ctx, [])
    assert "Ranking empty" in caplog.text
    assert ctx.portfolio.positions == {"KEEP": 5}


# ── resize and cash management ───────────────────────────────────────────


def test_resize_skips_odd_iso_weeks_unless_forced(make_context):
    for forced in (False, True):
        broker = FakeBroker()
        broker.add_equity("AAA", 1, trending_closes(120), end=ODD_WEEK_WEDNESDAY.date())
        ctx = make_context(broker, now=lambda: ODD_WEEK_WEDNESDAY, force_resize=forced)
        init_cash_balance(ctx)
        build_token_cache(ctx)
        ctx.portfolio.positions = {"AAA": 1000}
        resize_positions(ctx, bull=True)
        touched = any(c[0] in ("ltp", "historical_data", "place_order") for c in broker.calls)
        assert touched is forced


def test_resize_sells_down_and_buys_up_toward_atr_targets(make_context):
    broker = FakeBroker()
    broker.add_equity("FAT", 1, trending_closes(120, start=100.0), end=TODAY)
    broker.add_equity("THIN", 2, trending_closes(120, start=100.0), end=TODAY)
    ctx = make_context(broker)
    init_cash_balance(ctx)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"FAT": 1000, "THIN": 1}
    ctx.portfolio.cash = 100_000.0

    resize_positions(ctx, bull=True)

    sides = {(o.symbol, o.side) for o in broker.orders}
    assert sides == {("FAT", "SELL"), ("THIN", "BUY")}
    assert 1 < ctx.portfolio.positions["FAT"] < 1000
    assert ctx.portfolio.positions["THIN"] > 1
    assert ctx.portfolio.positions["FAT"] == ctx.portfolio.positions["THIN"]  # same price series, same target


def test_raise_cash_sells_the_worst_ranked_holding_first(ctx):
    init_cash_balance(ctx)
    ctx.broker.ltps.update({"NSE:BEST": 100.0, "NSE:WORST": 100.0})
    ctx.portfolio.positions = {"BEST": 10, "WORST": 10}
    ctx.portfolio.cash = -500.0

    raise_cash_if_needed(ctx, [rank("BEST"), rank("WORST")])

    per_share = 100.0 * (1 - ctx.settings.fees_pct - ctx.settings.slippage_pct)
    assert ctx.broker.orders == [Order("WORST", "SELL", 6, "MARKET")]  # ceil(500 / 99.8)
    assert ctx.portfolio.positions == {"BEST": 10, "WORST": 4}
    assert ctx.portfolio.cash == pytest.approx(-500.0 + 6 * per_share)


# ── the whole routine ────────────────────────────────────────────────────


def bull_market(drifts: dict[str, float]) -> FakeBroker:
    broker = FakeBroker()
    broker._instruments.append(NIFTY)
    broker.candles[NIFTY.token] = make_candles(trending_closes(260, start=20_000.0, daily=0.001), end=TODAY)
    for token, (sym, drift) in enumerate(drifts.items(), start=1):
        broker.add_equity(sym, token, trending_closes(260, daily=drift), end=TODAY)
    return broker


DRIFTS = {"AAA": 0.004, "BBB": 0.003, "CCC": 0.002, "DDD": 0.001, "EEE": 0.0005}


def test_run_buys_the_top_ranked_names_in_a_bull_market(make_context, caplog):
    broker = bull_market(DRIFTS)
    ctx = make_context(broker, cut_off_pct=0.5)  # top 2 of 5
    ctx.universe = StaticUniverse(DRIFTS)

    with caplog.at_level(logging.INFO, logger=LOG):
        run(ctx)

    buys = [o for o in broker.orders if o.side == "BUY"]
    assert [o.symbol for o in buys] == ["AAA", "BBB"]  # best first, strongest drift ranks highest
    assert all(o.order_type == "MARKET" for o in buys)
    assert set(ctx.portfolio.positions) == {"AAA", "BBB"}
    assert all(q >= 1 for q in ctx.portfolio.positions.values())
    assert read_portfolio(ctx.settings.out_file) == ctx.portfolio.positions
    assert 0 < ctx.portfolio.cash < ctx.settings.starting_cash
    assert [r["side"] for r in ledger_rows(ctx.settings.trades_ledger_file)] == ["BUY", "BUY"]
    assert "→ BULL" in caplog.text
    assert caplog.text.index("Top AAA") < caplog.text.index("Top BBB") < caplog.text.index("Top CCC")
    assert "PAPER BUY AAA" in caplog.text
    assert "Done. Final" in caplog.text


def test_run_sits_out_a_bear_market(make_context, caplog):
    broker = bull_market(DRIFTS)
    broker.candles[NIFTY.token] = make_candles(trending_closes(260, start=20_000.0, daily=-0.002), end=TODAY)
    ctx = make_context(broker, cut_off_pct=0.5)
    ctx.universe = StaticUniverse(DRIFTS)

    with caplog.at_level(logging.INFO, logger=LOG):
        run(ctx)

    assert broker.orders == []
    assert ctx.portfolio.positions == {}
    assert "→ BEAR" in caplog.text and "No new buys" in caplog.text


def test_run_kill_switch_liquidates_everything(make_context):
    broker = FakeBroker(ltp={"NSE:AAA": 100.0, "NSE:BBB": 50.0})
    ctx = make_context(broker, kill_switch=True)
    write_portfolio(ctx.settings.portfolio_file, {"AAA": 10, "BBB": 4})

    run(ctx)

    assert broker.orders == [Order("AAA", "SELL", 10, "MARKET"), Order("BBB", "SELL", 4, "MARKET")]
    assert ctx.portfolio.positions == {}
    assert read_portfolio(ctx.settings.out_file) == {}
    proceeds = 10 * 100.0 + 4 * 50.0
    friction = ctx.settings.fees_pct + ctx.settings.slippage_pct
    assert ctx.portfolio.cash == pytest.approx(ctx.settings.starting_cash + proceeds * (1 - friction))


def test_run_aborts_on_an_empty_universe(ctx, caplog):
    ctx.universe = StaticUniverse(())
    with caplog.at_level(logging.ERROR, logger=LOG):
        run(ctx)
    assert "Empty universe" in caplog.text
    assert ctx.broker.orders == []
    assert not any(c[0] == "historical_data" for c in ctx.broker.calls)


def test_env_cashflow_is_booked_once_at_start(make_context):
    ctx = make_context(env_cashflow=2_500.0, cashflow_note="salary")
    init_cash_balance(ctx)
    (row,) = ledger_rows(ctx.settings.cash_ledger_file)
    assert (row["date"], row["amount"], row["note"]) == (TODAY.isoformat(), "2500.00", "salary")
    assert ctx.portfolio.cash == ctx.settings.starting_cash + 2_500.0


# ── ADR-006: reasons and artifacts ───────────────────────────────────────


def read_table(path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_a_candle_fetch_that_throws_is_an_error_row_not_a_crash(make_context):
    class BrokenBroker(FakeBroker):
        def historical_data(self, *args, **kwargs):
            raise RuntimeError("boom")

    broker = BrokenBroker()
    broker.add_equity("X", 1, [1.0], end=TODAY)
    ctx = make_context(broker, artifacts=True)
    assert rank_step(ctx, broker.instruments("NSE")) == []
    (row,) = read_table(ctx.artifacts.path / "universe.csv")
    assert (row["symbol"], row["status"], row["reason"]) == ("X", "excluded", "error:RuntimeError")
    assert ctx.snapshots["X"].error == "RuntimeError: boom"


def test_gather_covers_the_universe_and_the_holdings_once(make_context):
    broker = FakeBroker()
    broker.add_equity("AAA", 1, trending_closes(120), end=TODAY)
    broker.add_equity("HELD", 2, trending_closes(120), end=TODAY)
    ctx = make_context(broker)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"HELD": 5, "GHOST": 1}  # GHOST has no instrument

    gather_snapshots(ctx, [i for i in broker.instruments("NSE") if i.tradingsymbol == "AAA"])
    fetches = [c for c in broker.calls if c[0] == "historical_data"]

    assert set(ctx.snapshots) == {"AAA", "HELD", "GHOST"}
    assert ctx.snapshots["AAA"].enough_history and ctx.snapshots["HELD"].enough_history
    assert ctx.snapshots["GHOST"].error.startswith("KeyError")
    assert len(fetches) == 2
    gather_snapshots(ctx, broker.instruments("NSE"))  # a second call fetches nothing new
    assert len([c for c in broker.calls if c[0] == "historical_data"]) == 2


def test_record_trade_mirrors_the_ledger_into_trades_csv(make_context):
    ctx = make_context(artifacts=True)
    init_cash_balance(ctx)
    ctx.broker.ltps["NSE:TCS"] = 100.0
    safe_buy(ctx, "TCS", 3)
    safe_sell(ctx, "TCS", 1)
    artifact_rows = read_table(ctx.artifacts.path / "trades.csv")
    assert artifact_rows == ledger_rows(ctx.settings.trades_ledger_file)
    assert [r["side"] for r in artifact_rows] == ["BUY", "SELL"]


def test_prune_writes_a_verdict_per_holding(make_context):
    broker = FakeBroker()
    broker.add_equity("KEEP", 1, trending_closes(120, daily=0.002), end=TODAY)
    broker.add_equity("DROP", 2, trending_closes(120, daily=0.002), end=TODAY)
    ctx = make_context(broker, cut_off_pct=1.0, artifacts=True)
    init_cash_balance(ctx)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"KEEP": 5, "DROP": 3}

    prune_portfolio(ctx, [rank("KEEP", close=120.0, ma100=100.0)])

    rows = {r["symbol"]: r for r in read_table(ctx.artifacts.path / "exits.csv")}
    assert (rows["KEEP"]["decision"], rows["KEEP"]["reasons"], rows["KEEP"]["rank"]) == ("HOLD", "", "1")
    assert rows["KEEP"]["stop_level"] != "" and rows["KEEP"]["price"] == ""
    assert (rows["DROP"]["decision"], rows["DROP"]["reasons"], rows["DROP"]["rank"]) == (
        "SELL",
        "unranked:not_in_universe",  # it passes every filter; it is simply not in the ranking it was given (ADR-025)
        "",
    )
    assert float(rows["DROP"]["price"]) == pytest.approx(broker.ltps["NSE:DROP"])

    prune_portfolio(ctx, [])
    assert (ctx.artifacts.path / "exits.csv").read_text() == ",".join(EXIT_COLUMNS) + "\n"


def test_skipped_resize_leaves_a_header_and_a_flag(make_context):
    ctx = make_context(now=lambda: ODD_WEEK_WEDNESDAY, artifacts=True)
    ctx.portfolio.positions = {"AAA": 10}
    resize_positions(ctx, bull=True)
    assert (ctx.artifacts.path / "sizing.csv").read_text() == ",".join(SIZING_COLUMNS) + "\n"
    assert json.loads((ctx.artifacts.path / "run.json").read_text())["resize_performed"] is False


def test_resize_records_every_holding_with_its_action(make_context):
    broker = FakeBroker()
    broker.add_equity("FAT", 1, trending_closes(120, start=100.0), end=TODAY)
    broker.add_equity("THIN", 2, trending_closes(120, start=100.0), end=TODAY)
    ctx = make_context(broker, artifacts=True)
    init_cash_balance(ctx)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"FAT": 1000, "THIN": 1, "GHOST": 5}
    ctx.portfolio.cash = 100_000.0

    resize_positions(ctx, bull=False)

    rows = {r["symbol"]: r for r in read_table(ctx.artifacts.path / "sizing.csv")}
    assert rows["FAT"]["action"] == "SELL" and int(rows["FAT"]["delta"]) < 0
    assert rows["THIN"]["action"] == "SKIP:bear" and int(rows["THIN"]["delta"]) > 0
    assert rows["GHOST"]["action"] == "SKIP:size_error" and rows["GHOST"]["target_qty"] == ""
    assert int(rows["FAT"]["target_qty"]) == int(rows["FAT"]["qty"]) + int(rows["FAT"]["delta"])


def test_run_writes_the_whole_artifact_set(make_context, caplog):
    broker = bull_market(DRIFTS)
    ctx = make_context(broker, cut_off_pct=0.5, artifacts=True)
    ctx.universe = StaticUniverse(DRIFTS)
    ctx.artifacts.attach_log(logging.getLogger(LOG))

    with caplog.at_level(logging.INFO, logger=LOG):
        run(ctx)

    path = ctx.artifacts.path
    assert sorted(p.name for p in path.iterdir()) == [
        "candidates.csv",
        "exits.csv",
        "orders.csv",
        "portfolio_after.csv",
        "portfolio_before.csv",
        "ranking.csv",
        "run.json",
        "run.log",
        "sizing.csv",
        "trades.csv",
        "universe.csv",
    ]
    assert (ctx.settings.runs_dir / "latest").resolve() == path.resolve()

    ranking = read_table(path / "ranking.csv")
    assert [r["symbol"] for r in ranking] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert [r["rank"] for r in ranking] == ["1", "2", "3", "4", "5"]
    assert ranking[0]["pct_rank"] == "0.200000" and ranking[0]["held"] == "false"
    top_lines = [line for line in caplog.text.splitlines() if "Top " in line]
    assert [line.split("Top ")[1].split(":")[0] for line in top_lines] == [r["symbol"] for r in ranking]

    universe = read_table(path / "universe.csv")
    assert {r["status"] for r in universe} == {"ranked"} and len(universe) == 5

    candidates = {r["symbol"]: r for r in read_table(path / "candidates.csv")}
    assert candidates["AAA"]["decision"] == "BUY" and candidates["BBB"]["decision"] == "BUY"
    assert {candidates[s]["decision"] for s in ("CCC", "DDD", "EEE")} == {"SKIP:beyond_cutoff"}
    assert float(candidates["AAA"]["cash_after"]) > float(candidates["BBB"]["cash_after"])

    assert read_table(path / "trades.csv") == ledger_rows(ctx.settings.trades_ledger_file)
    orders = read_table(path / "orders.csv")
    assert [(o["symbol"], o["side"], o["status"]) for o in orders] == [
        ("AAA", "BUY", "COMPLETE"),
        ("BBB", "BUY", "COMPLETE"),
    ]
    assert [o["filled_qty"] for o in orders] == [t["qty"] for t in read_table(path / "trades.csv")]
    assert (path / "exits.csv").read_text() == ",".join(EXIT_COLUMNS) + "\n"  # no holdings to judge
    assert (path / "sizing.csv").read_text() == ",".join(SIZING_COLUMNS) + "\n"  # nothing to resize
    assert (path / "portfolio_before.csv").read_text() == ""
    assert read_portfolio(str(path / "portfolio_after.csv")) == ctx.portfolio.positions

    meta = json.loads((path / "run.json").read_text())
    assert meta["status"] == "completed" and meta["finished"] is not None
    assert meta["mode"] == "paper" and meta["regime"]["bull"] is True
    assert (meta["universe_size"], meta["ranked_count"], meta["resize_performed"]) == (5, 5, True)
    assert meta["positions_before"] == {} and meta["positions_after"] == ctx.portfolio.positions
    assert meta["cash_after"] == pytest.approx(ctx.portfolio.cash)
    assert meta["equity_after"] > meta["cash_after"]
    assert "KITE_API_SECRET" not in json.dumps(meta) and "test-secret" not in json.dumps(meta)
    assert "Done. Final" in (path / "run.log").read_text()


def test_run_records_an_aborted_status_on_an_empty_universe(make_context):
    ctx = make_context(artifacts=True)
    ctx.universe = StaticUniverse(())
    run(ctx)
    path = ctx.artifacts.path
    meta = json.loads((path / "run.json").read_text())
    assert meta["status"] == "aborted:empty_universe" and meta["universe_size"] == 0
    assert (path / "portfolio_before.csv").exists() and not (path / "universe.csv").exists()
    assert (path / "trades.csv").read_text() == ",".join(TRADE_COLUMNS) + "\n"


def test_run_kill_switch_uses_the_kill_mode_directory(make_context):
    broker = FakeBroker(ltp={"NSE:AAA": 100.0})
    ctx = make_context(broker, kill_switch=True, artifacts=True)
    write_portfolio(ctx.settings.portfolio_file, {"AAA": 10})
    run(ctx)
    assert ctx.artifacts.path.name.endswith("-kill")
    meta = json.loads((ctx.artifacts.path / "run.json").read_text())
    assert (meta["status"], meta["positions_before"], meta["positions_after"]) == ("completed", {"AAA": 10}, {})
    assert len(read_table(ctx.artifacts.path / "trades.csv")) == 1


# ── ADR-017: a position changes only when a trade was placed ─────────────


def test_prune_keeps_a_holding_it_cannot_price(make_context, caplog):
    broker = FakeBroker()
    broker.add_equity("KEEP", 1, trending_closes(120, daily=0.002), end=TODAY)
    ctx = make_context(broker, cut_off_pct=1.0, artifacts=True)
    init_cash_balance(ctx)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"KEEP": 5, "GHOST": 3}  # GHOST has no price anywhere
    cash_before = ctx.portfolio.cash

    with caplog.at_level(logging.WARNING, logger=LOG):
        prune_portfolio(ctx, [rank("KEEP", close=120.0, ma100=100.0)])

    assert ctx.portfolio.positions == {"KEEP": 5, "GHOST": 3}
    assert ctx.portfolio.sold == set()
    assert broker.orders == [] and ledger_rows(ctx.settings.trades_ledger_file) == []
    assert ctx.portfolio.cash == cash_before
    rows = {r["symbol"]: r for r in read_table(ctx.artifacts.path / "exits.csv")}
    assert (rows["GHOST"]["decision"], rows["GHOST"]["reasons"], rows["GHOST"]["price"]) == (
        "SKIP:no_price",
        "unranked:error:KeyError",  # no instrument, so no snapshot: the cause is the lookup that failed (ADR-025)
        "",
    )
    assert "GHOST: exit wanted (unranked:error:KeyError) but no price came back" in caplog.text


def test_resize_leaves_the_quantity_when_the_sell_down_has_no_price(make_context):
    broker = FakeBroker()
    broker.add_equity("FAT", 1, trending_closes(120, start=100.0), end=TODAY)
    del broker.ltps["NSE:FAT"]  # candles for sizing, but no last price to sell at
    ctx = make_context(broker, artifacts=True)
    init_cash_balance(ctx)
    build_token_cache(ctx)
    ctx.portfolio.positions = {"FAT": 1000}

    resize_positions(ctx, bull=False)

    assert ctx.portfolio.positions == {"FAT": 1000}
    assert broker.orders == []
    rows = {r["symbol"]: r for r in read_table(ctx.artifacts.path / "sizing.csv")}
    assert rows["FAT"]["action"] == "SKIP:not_placed"


def test_raise_cash_skips_a_holding_it_cannot_price_and_sells_the_next(ctx):
    init_cash_balance(ctx)
    ctx.broker.ltps.update({"NSE:BEST": 100.0})  # WORST has no price
    ctx.portfolio.positions = {"BEST": 10, "WORST": 10}
    ctx.portfolio.cash = -500.0

    raise_cash_if_needed(ctx, [rank("BEST"), rank("WORST")])

    assert ctx.portfolio.positions["WORST"] == 10  # untouched: nothing was sent
    assert [o.symbol for o in ctx.broker.orders] == ["BEST"]
    assert ctx.portfolio.positions["BEST"] == 4 and ctx.portfolio.cash > 0


def test_kill_switch_reports_what_it_could_not_sell(make_context, caplog):
    broker = FakeBroker(ltp={"NSE:AAA": 100.0})  # BBB has no price
    ctx = make_context(broker, kill_switch=True, artifacts=True)
    write_portfolio(ctx.settings.portfolio_file, {"AAA": 10, "BBB": 4})

    with caplog.at_level(logging.WARNING, logger=LOG):
        run(ctx)

    assert broker.orders == [Order("AAA", "SELL", 10, "MARKET")]
    assert ctx.portfolio.positions == {"BBB": 4}
    assert read_portfolio(ctx.settings.out_file) == {"BBB": 4}
    assert "could not sell 1 position(s), still held: BBB" in caplog.text
    assert "1 position(s) remain" in caplog.text
