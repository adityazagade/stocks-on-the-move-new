"""Tests for the broker boundary: Kite adapter mapping, backoff, paper wrapper (ADR-008, ADR-019)."""

from __future__ import annotations

from datetime import date

import pytest
from kiteconnect.exceptions import NetworkException, TokenException

from fakes import FakeBroker
from stocks_on_the_move.broker import (
    TERMINAL_STATUSES,
    BrokerError,
    Instrument,
    KiteBroker,
    Order,
    OrderStatus,
    PaperBroker,
    Quote,
)


class FakeKite:
    """Just enough of KiteConnect: the constants and the eight methods, all scripted."""

    VARIETY_REGULAR = "regular"
    EXCHANGE_NSE = "NSE"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    PRODUCT_CNC = "CNC"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_MARKET = "MARKET"
    VALIDITY_DAY = "DAY"

    def __init__(self, *, rate_limit_first: int = 0):
        self.remaining_429 = rate_limit_first
        self.calls: list[tuple] = []
        self.instrument_downloads = 0
        self.history: dict[str, list[dict]] = {}  # order id -> the entries order_history returns

    def _maybe_429(self):
        if self.remaining_429 > 0:
            self.remaining_429 -= 1
            raise NetworkException("Too many requests", code=429)

    def instruments(self, exchange):
        self.instrument_downloads += 1
        return [
            {
                "instrument_token": 1,
                "tradingsymbol": "TCS",
                "exchange": "NSE",
                "segment": "NSE",
                "instrument_type": "EQ",
            },
            {"instrument_token": 0, "tradingsymbol": "BROKEN"},  # dropped: no token
            {"instrument_token": 2, "tradingsymbol": "IDEA-BE", "segment": "NSE", "instrument_type": "EQ"},
        ]

    def ltp(self, keys):
        self._maybe_429()
        self.calls.append(("ltp", keys))
        return {"NSE:TCS": {"instrument_token": 1, "last_price": 3500.5}, "NSE:BAD": {"last_price": "n/a"}}

    def quote(self, keys):
        return {
            "NSE:IDEA-BE": {
                "last_price": 10.0,
                "depth": {"buy": [{"price": 9.9, "quantity": 5}], "sell": [{"price": 10.1, "quantity": 5}]},
            },
            "NSE:EMPTY": {"last_price": 5.0, "depth": {"buy": [], "sell": []}},
        }

    def historical_data(self, token, from_date, to_date, interval, continuous=False, oi=False):
        self.calls.append(("historical_data", token, from_date, to_date, interval, continuous, oi))
        return [{"date": "2026-09-15", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]

    def place_order(self, **kwargs):
        self._maybe_429()
        self.calls.append(("place_order", kwargs))
        return 240912000001

    def order_history(self, order_id):
        self._maybe_429()
        self.calls.append(("order_history", order_id))
        return self.history.get(order_id, [])

    def cancel_order(self, variety, order_id, parent_order_id=None):
        self._maybe_429()
        self.calls.append(("cancel_order", variety, order_id))
        return order_id

    def profile(self):
        return {"user_id": "AB1234"}


def broker(kite, *, retries=3, interval=0.5):
    sleeps: list[float] = []
    clock = {"t": 100.0}

    def monotonic():
        clock["t"] += 0.01
        return clock["t"]

    b = KiteBroker(
        kite,
        min_interval=interval,
        max_retries=retries,
        sleep=sleeps.append,
        monotonic=monotonic,
        uniform=lambda a, b: 0.0,
    )
    return b, sleeps


# ── Order ────────────────────────────────────────────────────────────────


def test_order_validates_its_own_invariants():
    Order("TCS", "BUY", 1, "MARKET")
    Order("TCS", "SELL", 1, "LIMIT", limit_price=10.0)
    with pytest.raises(ValueError, match="side"):
        Order("TCS", "HOLD", 1, "MARKET")  # ty: ignore[invalid-argument-type]  (the point of the test)
    with pytest.raises(ValueError, match="quantity"):
        Order("TCS", "BUY", 0, "MARKET")
    with pytest.raises(ValueError, match="LIMIT"):
        Order("TCS", "BUY", 1, "LIMIT")
    with pytest.raises(ValueError, match="MARKET"):
        Order("TCS", "BUY", 1, "MARKET", limit_price=10.0)


# ── KiteBroker: throttle and backoff ─────────────────────────────────────


def test_requests_are_spaced_by_min_interval():
    b, sleeps = broker(FakeKite(), interval=0.5)
    b.ltp(["NSE:TCS"])
    assert sleeps == []  # the first request never waits
    b.ltp(["NSE:TCS"])
    assert len(sleeps) == 1 and 0.4 < sleeps[0] <= 0.5  # clock moved 0.02s between the two calls


def test_rate_limit_backs_off_then_succeeds():
    kite = FakeKite(rate_limit_first=2)
    b, sleeps = broker(kite, retries=4, interval=0.0)
    assert b.ltp(["NSE:TCS"]) == {"NSE:TCS": 3500.5}
    assert sleeps == [0.25, 0.5]  # jitter patched to zero: pure exponential backoff


def test_rate_limit_exhaustion_raises_broker_error_not_none():
    kite = FakeKite(rate_limit_first=99)
    b, sleeps = broker(kite, retries=3, interval=0.0)
    with pytest.raises(BrokerError, match="3 attempts") as err:
        b.ltp(["NSE:TCS"])
    assert isinstance(err.value.__cause__, NetworkException)
    assert sleeps == [0.25, 0.5, 1.0]


def test_other_errors_propagate_immediately():
    class AngryKite(FakeKite):
        def ltp(self, keys):
            raise TokenException("Incorrect `api_key` or `access_token`.")

        def quote(self, keys):
            raise NetworkException("Gateway timeout", code=504)

    b, sleeps = broker(AngryKite(), retries=5, interval=0.0)
    with pytest.raises(TokenException):
        b.ltp(["NSE:TCS"])
    with pytest.raises(NetworkException, match="Gateway"):
        b.quote(["NSE:TCS"])
    assert sleeps == []


# ── KiteBroker: mapping to our types ─────────────────────────────────────


def test_instruments_are_mapped_filtered_and_fetched_once():
    kite = FakeKite()
    b, _ = broker(kite)
    first = b.instruments("NSE")
    assert first == [
        Instrument(1, "TCS", "NSE", "NSE", "EQ"),
        Instrument(2, "IDEA-BE", "NSE", "NSE", "EQ"),
    ]
    assert b.instruments("NSE") is first
    assert kite.instrument_downloads == 1


def test_ltp_skips_unparseable_prices():
    b, _ = broker(FakeKite())
    assert b.ltp(["NSE:TCS", "NSE:BAD"]) == {"NSE:TCS": 3500.5}
    assert b.ltp([]) == {}


def test_quote_reads_top_of_book_and_tolerates_an_empty_book():
    b, _ = broker(FakeKite())
    q = b.quote(["NSE:IDEA-BE", "NSE:EMPTY"])
    assert q["NSE:IDEA-BE"] == Quote(last_price=10.0, best_bid=9.9, best_ask=10.1)
    assert q["NSE:EMPTY"] == Quote(last_price=5.0, best_bid=None, best_ask=None)


def test_historical_data_passes_kite_flags_and_types_the_rows():
    kite = FakeKite()
    b, _ = broker(kite)
    rows = b.historical_data(1, date(2026, 9, 1), date(2026, 9, 15))
    assert kite.calls[-1] == ("historical_data", 1, date(2026, 9, 1), date(2026, 9, 15), "day", False, False)
    assert rows == [{"date": "2026-09-15", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}]


def test_place_order_speaks_kite_vocabulary_only_in_the_adapter():
    kite = FakeKite()
    b, _ = broker(kite)
    assert b.place_order(Order("TCS", "BUY", 10, "MARKET")) == "240912000001"
    assert kite.calls[-1][1] == {
        "variety": "regular",
        "exchange": "NSE",
        "tradingsymbol": "TCS",
        "transaction_type": "BUY",
        "quantity": 10,
        "product": "CNC",
        "order_type": "MARKET",
    }
    b.place_order(Order("IDEA-BE", "SELL", 5, "LIMIT", limit_price=9.9))
    assert kite.calls[-1][1] == {
        "variety": "regular",
        "exchange": "NSE",
        "tradingsymbol": "IDEA-BE",
        "transaction_type": "SELL",
        "quantity": 5,
        "product": "CNC",
        "order_type": "LIMIT",
        "price": 9.9,
        "validity": "DAY",
    }


def test_profile_is_a_plain_dict():
    b, _ = broker(FakeKite())
    assert b.profile() == {"user_id": "AB1234"}


# ── order status and cancellation (ADR-019) ──────────────────────────────


def test_order_status_is_terminal_only_for_the_three_final_states():
    assert {"COMPLETE", "REJECTED", "CANCELLED"} == TERMINAL_STATUSES
    for status in ("COMPLETE", "REJECTED", "CANCELLED"):
        assert OrderStatus("1", status, 0, 0, 0.0).terminal
    for status in ("OPEN", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING", "TRIGGER PENDING", "UNKNOWN"):
        assert not OrderStatus("1", status, 0, 0, 0.0).terminal


def test_order_status_reads_the_latest_history_entry_and_only_the_named_fields():
    kite = FakeKite()
    kite.history["240912000001"] = [
        {"status": "PUT ORDER REQ RECEIVED", "filled_quantity": 0, "pending_quantity": 10, "average_price": 0},
        {"status": "OPEN", "filled_quantity": 4, "pending_quantity": 6, "average_price": 99.5},
        {
            "status": "COMPLETE",
            "filled_quantity": 10,
            "pending_quantity": 0,
            "average_price": 99.75,
            "status_message": None,
            "tag": "not read",
        },
    ]
    b, _ = broker(kite)
    status = b.order_status("240912000001")
    assert status == OrderStatus("240912000001", "COMPLETE", 10, 0, 99.75, None)
    assert status.terminal
    assert kite.calls[-1] == ("order_history", "240912000001")


def test_order_status_keeps_the_rejection_message_and_tolerates_missing_fields():
    kite = FakeKite()
    kite.history["1"] = [{"status": "REJECTED", "status_message": "Insufficient funds", "average_price": None}]
    kite.history["2"] = [{"status": "open"}]
    b, _ = broker(kite)
    assert b.order_status("1") == OrderStatus("1", "REJECTED", 0, 0, 0.0, "Insufficient funds")
    assert b.order_status("2") == OrderStatus("2", "OPEN", 0, 0, 0.0, None)


def test_order_status_with_no_history_yet_is_unknown_not_terminal():
    b, _ = broker(FakeKite())
    status = b.order_status("fresh")
    assert status == OrderStatus("fresh", "UNKNOWN", 0, 0, 0.0)
    assert not status.terminal


def test_cancel_order_speaks_kite_vocabulary_and_goes_through_the_wrapper():
    kite = FakeKite(rate_limit_first=1)
    b, sleeps = broker(kite, retries=3, interval=0.0)
    b.cancel_order("240912000001")
    assert kite.calls[-1] == ("cancel_order", "regular", "240912000001")
    assert len(sleeps) == 1  # one backoff for the one 429


def test_order_status_backs_off_like_every_other_call():
    kite = FakeKite(rate_limit_first=99)
    b, _ = broker(kite, retries=2, interval=0.0)
    with pytest.raises(BrokerError):
        b.order_status("1")


# ── PaperBroker ──────────────────────────────────────────────────────────


def test_paper_broker_delegates_reads_and_swallows_orders(caplog):
    inner = FakeBroker(ltp={"NSE:TCS": 100.0})
    paper = PaperBroker(inner)
    assert paper.ltp(["NSE:TCS"]) == {"NSE:TCS": 100.0}
    assert paper.profile() == {"user_id": "FAKE01"}

    order = Order("TCS", "BUY", 3, "MARKET")
    with caplog.at_level("DEBUG", logger="stocks_on_the_move.broker"):
        assert paper.place_order(order) == "PAPER-0001"
        assert paper.place_order(order) == "PAPER-0002"
    assert paper.orders == [order, order]
    assert inner.orders == []  # never reached the real broker
    assert "not sent" in caplog.text


def test_paper_broker_fills_every_order_in_full_on_the_first_poll():
    inner = FakeBroker(ltp={"NSE:TCS": 100.0})
    paper = PaperBroker(inner)
    market = paper.place_order(Order("TCS", "BUY", 3, "MARKET"))
    limit = paper.place_order(Order("IDEA-BE", "SELL", 50, "LIMIT", limit_price=9.9))

    assert paper.order_status(market) == OrderStatus(market, "COMPLETE", 3, 0, 100.0)
    assert paper.order_status(limit) == OrderStatus(limit, "COMPLETE", 50, 0, 9.9)
    assert ("ltp", ["NSE:TCS"]) in inner.calls  # the market fill is the wrapped broker's last price
    paper.cancel_order(market)  # nothing to cancel, nothing raised
    assert inner.calls[-1] == ("ltp", ["NSE:TCS"])  # and nothing reached the wrapped broker
