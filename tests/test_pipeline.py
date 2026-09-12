"""The strategy above the pure helpers, run against FakeBroker and a frozen clock (ADR-008)."""

from __future__ import annotations

import csv
import logging

import pytest

from fakes import EVEN_WEEK_WEDNESDAY, ODD_WEEK_WEDNESDAY, FakeBroker, make_candles, trending_closes
from stocks_on_the_move import momentum as m
from stocks_on_the_move.broker import Instrument, Order, Quote

TODAY = EVEN_WEEK_WEDNESDAY.date()
NIFTY = Instrument(256265, "NIFTY 50", "NSE", "INDICES", "EQ")
LOG = "stocks_on_the_move.momentum"


def ledger_rows(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def rank(symbol: str, *, close: float = 100.0, ema100: float = 90.0) -> m.RankItem:
    return m.RankItem(symbol, 0.5, 0.3, 0.9, close, ema100)


# ── prices and tokens ────────────────────────────────────────────────────


def test_ltp_map_strips_the_exchange_prefix(ctx):
    ctx.broker.ltps.update({"NSE:TCS": 100.0, "NSE:INFY": 50.0})
    assert m.ltp_map(ctx.broker, ["TCS", "INFY", "NOPE"]) == {"TCS": 100.0, "INFY": 50.0}
    assert m.ltp_map(ctx.broker, []) == {}
    assert not ctx.broker.calls[-1:] or ctx.broker.calls[-1][0] == "ltp"


def test_token_of_uses_the_cache_then_the_instruments_then_fails(ctx):
    ctx.broker.add_equity("TCS", 11, [100, 101], end=TODAY)
    ctx.broker._instruments.append(NIFTY)
    m.build_token_cache(ctx)
    assert ctx.tokens["NSE:TCS"] == 11
    assert m.token_of(ctx, "TCS", "NSE") == 11
    assert m.token_of(ctx) == NIFTY.token  # the regime index by default
    with pytest.raises(KeyError, match="NSE:NOPE"):
        m.token_of(ctx, "NOPE", "NSE")


# ── orders ───────────────────────────────────────────────────────────────


def test_market_buy_books_the_trade_the_cash_and_the_ledger_row(ctx):
    m.init_cash_balance(ctx)
    ctx.broker.ltps["NSE:TCS"] = 100.0
    ctx.portfolio.cash = 10_000.0

    assert m.safe_buy(ctx, "TCS", 10) == 100.0

    assert ctx.broker.orders == [Order("TCS", "BUY", 10, "MARKET")]
    friction = ctx.settings.fees_pct + ctx.settings.slippage_pct
    assert ctx.portfolio.cash == pytest.approx(10_000 - 1_000 * (1 + friction))
    (row,) = ledger_rows(ctx.settings.trades_ledger_file)
    assert (row["side"], row["symbol"], row["qty"], row["price"]) == ("BUY", "TCS", "10", "100.0000")
    assert row["timestamp"] == EVEN_WEEK_WEDNESDAY.isoformat(timespec="seconds")


def test_buy_is_refused_when_cash_is_short(ctx, caplog):
    m.init_cash_balance(ctx)
    ctx.broker.ltps["NSE:TCS"] = 100.0
    ctx.portfolio.cash = 500.0
    with caplog.at_level(logging.INFO, logger=LOG):
        assert m.safe_buy(ctx, "TCS", 10) is None
    assert ctx.broker.orders == []
    assert ctx.portfolio.cash == 500.0
    assert "Not enough cash" in caplog.text


def test_market_sell_credits_cash_net_of_friction(ctx):
    m.init_cash_balance(ctx)
    ctx.broker.ltps["NSE:TCS"] = 100.0
    ctx.portfolio.cash = 0.0
    assert m.safe_sell(ctx, "TCS", 10) == 100.0
    assert ctx.broker.orders == [Order("TCS", "SELL", 10, "MARKET")]
    friction = ctx.settings.fees_pct + ctx.settings.slippage_pct
    assert ctx.portfolio.cash == pytest.approx(1_000 * (1 - friction))


def test_no_market_series_trade_with_limit_orders_at_the_top_of_the_book(ctx):
    m.init_cash_balance(ctx)
    ctx.broker.quotes["NSE:IDEA-BE"] = Quote(last_price=10.0, best_bid=9.9, best_ask=10.1)
    ctx.broker.quotes["NSE:THIN-BZ"] = Quote(last_price=5.0, best_bid=None, best_ask=None)

    assert m.safe_buy(ctx, "IDEA-BE", 100) == 10.1
    assert m.safe_sell(ctx, "IDEA-BE", 100) == 9.9
    assert m.safe_sell(ctx, "THIN-BZ", 10) == 5.0  # empty book: last price

    assert ctx.broker.orders == [
        Order("IDEA-BE", "BUY", 100, "LIMIT", limit_price=10.1),
        Order("IDEA-BE", "SELL", 100, "LIMIT", limit_price=9.9),
        Order("THIN-BZ", "SELL", 10, "LIMIT", limit_price=5.0),
    ]


def test_a_trade_without_a_price_is_skipped_with_a_warning(ctx, caplog):
    m.init_cash_balance(ctx)
    with caplog.at_level(logging.WARNING, logger=LOG):
        assert m.safe_buy(ctx, "GHOST", 5) is None
        assert m.safe_sell(ctx, "GHOST", 5) is None
    assert ctx.broker.orders == []
    assert caplog.text.count("No price for GHOST") == 2


def test_tiny_quantities_are_ignored(ctx):
    assert m.safe_buy(ctx, "TCS", 0) is None
    assert m.safe_sell(ctx, "TCS", 0) is None
    assert ctx.broker.calls == []


def test_trade_lines_keep_the_paper_label(ctx, caplog):
    m.init_cash_balance(ctx)
    ctx.broker.ltps["NSE:TCS"] = 100.0
    with caplog.at_level(logging.INFO, logger=LOG):
        m.safe_buy(ctx, "TCS", 1)
        ctx.paper = False
        m.safe_sell(ctx, "TCS", 1)
    assert "PAPER BUY TCS" in caplog.text
    assert "SELL TCS" in caplog.text and "PAPER SELL" not in caplog.text


# ── exits ────────────────────────────────────────────────────────────────


def test_rank_says_exit_rules(settings):
    assert m.rank_says_exit(settings, None, 0.1) is True
    assert m.rank_says_exit(settings, rank("AAA"), settings.cut_off_pct + 0.01) is True
    assert m.rank_says_exit(settings, rank("AAA", close=90.0, ema100=90.0), 0.1) is True
    assert m.rank_says_exit(settings, rank("AAA"), settings.cut_off_pct) is False


def test_trailing_stop_fires_after_a_collapse_and_not_in_an_uptrend(ctx):
    steady = trending_closes(120, daily=0.001)
    ctx.broker.add_equity("UP", 1, steady, end=TODAY)
    ctx.broker.add_equity("DOWN", 2, steady[:-10] + [c * 0.5 for c in steady[-10:]], end=TODAY)
    m.build_token_cache(ctx)
    assert m._trailing_stop_hit(ctx, "UP") is False
    assert m._trailing_stop_hit(ctx, "DOWN") is True


def test_prune_sells_unranked_holdings_and_skips_on_an_empty_ranking(make_context, caplog):
    broker = FakeBroker()
    broker.add_equity("KEEP", 1, trending_closes(120, daily=0.002), end=TODAY)
    broker.add_equity("DROP", 2, trending_closes(120, daily=0.002), end=TODAY)
    ctx = make_context(broker, cut_off_pct=1.0)
    m.init_cash_balance(ctx)
    m.build_token_cache(ctx)
    ctx.portfolio.positions = {"KEEP": 5, "DROP": 3}

    m.prune_portfolio(ctx, [rank("KEEP")])

    assert [(o.symbol, o.side, o.quantity) for o in broker.orders] == [("DROP", "SELL", 3)]
    assert ctx.portfolio.positions == {"KEEP": 5}
    assert ctx.portfolio.sold == {"DROP"}

    with caplog.at_level(logging.WARNING, logger=LOG):
        m.prune_portfolio(ctx, [])
    assert "Ranking empty" in caplog.text
    assert ctx.portfolio.positions == {"KEEP": 5}


# ── resize and cash management ───────────────────────────────────────────


def test_resize_skips_odd_iso_weeks_unless_forced(make_context):
    for forced in (False, True):
        broker = FakeBroker()
        broker.add_equity("AAA", 1, trending_closes(120), end=ODD_WEEK_WEDNESDAY.date())
        ctx = make_context(broker, now=lambda: ODD_WEEK_WEDNESDAY, force_resize=forced)
        m.init_cash_balance(ctx)
        m.build_token_cache(ctx)
        ctx.portfolio.positions = {"AAA": 1000}
        m.resize_positions(ctx, bull=True)
        touched = any(c[0] in ("ltp", "historical_data", "place_order") for c in broker.calls)
        assert touched is forced


def test_resize_sells_down_and_buys_up_toward_atr_targets(make_context):
    broker = FakeBroker()
    broker.add_equity("FAT", 1, trending_closes(120, start=100.0), end=TODAY)
    broker.add_equity("THIN", 2, trending_closes(120, start=100.0), end=TODAY)
    ctx = make_context(broker)
    m.init_cash_balance(ctx)
    m.build_token_cache(ctx)
    ctx.portfolio.positions = {"FAT": 1000, "THIN": 1}
    ctx.portfolio.cash = 100_000.0

    m.resize_positions(ctx, bull=True)

    sides = {(o.symbol, o.side) for o in broker.orders}
    assert sides == {("FAT", "SELL"), ("THIN", "BUY")}
    assert 1 < ctx.portfolio.positions["FAT"] < 1000
    assert ctx.portfolio.positions["THIN"] > 1
    assert ctx.portfolio.positions["FAT"] == ctx.portfolio.positions["THIN"]  # same price series, same target


def test_raise_cash_sells_the_worst_ranked_holding_first(ctx):
    m.init_cash_balance(ctx)
    ctx.broker.ltps.update({"NSE:BEST": 100.0, "NSE:WORST": 100.0})
    ctx.portfolio.positions = {"BEST": 10, "WORST": 10}
    ctx.portfolio.cash = -500.0

    m.raise_cash_if_needed(ctx, [rank("BEST"), rank("WORST")])

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
    ctx.universe = lambda: set(DRIFTS)

    with caplog.at_level(logging.INFO, logger=LOG):
        m.run(ctx)

    buys = [o for o in broker.orders if o.side == "BUY"]
    assert [o.symbol for o in buys] == ["AAA", "BBB"]  # best first, strongest drift ranks highest
    assert all(o.order_type == "MARKET" for o in buys)
    assert set(ctx.portfolio.positions) == {"AAA", "BBB"}
    assert all(q >= 1 for q in ctx.portfolio.positions.values())
    assert m.read_portfolio(ctx.settings.out_file) == ctx.portfolio.positions
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
    ctx.universe = lambda: set(DRIFTS)

    with caplog.at_level(logging.INFO, logger=LOG):
        m.run(ctx)

    assert broker.orders == []
    assert ctx.portfolio.positions == {}
    assert "→ BEAR" in caplog.text and "No new buys" in caplog.text


def test_run_kill_switch_liquidates_everything(make_context):
    broker = FakeBroker(ltp={"NSE:AAA": 100.0, "NSE:BBB": 50.0})
    ctx = make_context(broker, kill_switch=True)
    m.write_portfolio(ctx.settings.portfolio_file, {"AAA": 10, "BBB": 4})

    m.run(ctx)

    assert broker.orders == [Order("AAA", "SELL", 10, "MARKET"), Order("BBB", "SELL", 4, "MARKET")]
    assert ctx.portfolio.positions == {}
    assert m.read_portfolio(ctx.settings.out_file) == {}
    proceeds = 10 * 100.0 + 4 * 50.0
    friction = ctx.settings.fees_pct + ctx.settings.slippage_pct
    assert ctx.portfolio.cash == pytest.approx(ctx.settings.starting_cash + proceeds * (1 - friction))


def test_run_aborts_on_an_empty_universe(ctx, caplog):
    ctx.universe = set
    with caplog.at_level(logging.ERROR, logger=LOG):
        m.run(ctx)
    assert "Empty universe" in caplog.text
    assert ctx.broker.orders == []
    assert not any(c[0] == "historical_data" for c in ctx.broker.calls)


def test_env_cashflow_is_booked_once_at_start(make_context):
    ctx = make_context(env_cashflow=2_500.0, cashflow_note="salary")
    m.init_cash_balance(ctx)
    (row,) = ledger_rows(ctx.settings.cash_ledger_file)
    assert (row["date"], row["amount"], row["note"]) == (TODAY.isoformat(), "2500.00", "salary")
    assert ctx.portfolio.cash == ctx.settings.starting_cash + 2_500.0
