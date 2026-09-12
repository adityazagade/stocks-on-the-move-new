"""Generate the golden-test fixtures (ADR-009).

The repository is public, so the fixtures are synthetic: seeded geometric
random walks shaped so that every reachable branch of the pipeline is
exercised, not copies of Kite's historical data. Each instrument has a *role*
below that says which branch it is there for. Re-run deliberately with

    uv run python tests/fixtures/golden/make_fixtures.py

and then regenerate the expected files with ``uv run pytest --update-golden``.
Both diffs are reviewed in the commit like any code change.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).parent
IST = ZoneInfo("Asia/Kolkata")
IST_OFFSET = timezone(timedelta(hours=5, minutes=30))  # Kite's fixed offset, as the cache stores it
SEED = 20260912
DAYS = 260


def wednesday(even_week: bool) -> date:
    d = date(2026, 9, 16)  # a Wednesday
    while (d.isocalendar().week % 2 == 0) != even_week:
        d += timedelta(days=7)
    return d


AS_OF = {"even_week": wednesday(even_week=True), "odd_week": wednesday(even_week=False)}
END = max(AS_OF.values())  # candles run to the later date; the earlier run sees a prefix


@dataclass(frozen=True)
class Spec:
    symbol: str
    token: int
    role: str
    drift: float = 0.0
    vol: float = 0.005
    price: float = 100.0
    days: int = DAYS
    volume: int = 500_000
    spread: float = 0.012  # high-low as a fraction of the close
    collapse: float | None = (
        None  # multiply the close down to this factor over collapse_days, ending on the even-week date
    )
    collapse_days: int = 4
    segment: str = "NSE"
    instrument_type: str = "EQ"
    in_universe: bool = True
    notes: str = ""

    def as_meta(self) -> dict:
        return {"symbol": self.symbol, "token": self.token, "role": self.role, "notes": self.notes}


# Daily noise is kept near 0.5 % so the drifts, not the last five days' noise, order the ranking.
SPECS: list[Spec] = [
    # Strong trends that rank inside the cut-off and get bought until MAX_POSITIONS stops the loop
    Spec("ALPHA", 1001, "rank_buy", 0.0035, 0.005, 1200, notes="strongest trend; first buy"),
    Spec("BRAVO", 1002, "rank_buy", 0.0030, 0.004, 450),
    Spec("CHARLIE", 1003, "rank_buy", 0.0028, 0.006, 2300),
    Spec("DELTA", 1004, "rank_buy", 0.0025, 0.005, 95),
    Spec("ECHO", 1005, "rank_buy", 0.0022, 0.005, 780),
    Spec("FOXTROT", 1006, "rank_buy", 0.0020, 0.004, 310),
    # Ranked, but too weak for the cut-off or beyond MAX_POSITIONS
    Spec("GOLF", 1007, "rank", 0.0018, 0.006, 1500),
    Spec("HOTEL", 1008, "rank", 0.0016, 0.007, 66),
    Spec("INDIA", 1009, "rank", 0.0014, 0.005, 890),
    Spec("JULIET", 1010, "rank", 0.0012, 0.006, 2100),
    Spec("KILO", 1011, "rank", 0.0010, 0.006, 540),
    Spec("LIMA", 1012, "rank", 0.0008, 0.005, 130),
    Spec("MIKE", 1013, "rank", 0.0006, 0.007, 760),
    Spec("NOVEMBER", 1014, "rank", 0.0005, 0.005, 3300),
    Spec("OSCAR", 1015, "rank", 0.0004, 0.006, 210),
    Spec("PAPA", 1016, "rank", 0.0003, 0.006, 1750),
    Spec("QUEBEC", 1017, "rank", 0.0002, 0.005, 420),
    Spec("ROMEO", 1018, "rank", 0.0001, 0.006, 980),
    # The fixture portfolio
    Spec("HOLDFAST", 1021, "held_keep", 0.0030, 0.005, 600, notes="held 50, under its ATR size: resize buys"),
    Spec("ANCHOR", 1026, "held_keep", 0.0028, 0.005, 150, notes="held 2000, over its ATR size: resize sells"),
    Spec("DRIFTER", 1022, "held_drop", -0.0020, 0.006, 400, notes="held, below EMA-100, sold as unranked"),
    Spec(
        "CLIFF",
        1023,
        "held_stop",
        0.0035,
        0.004,
        900,
        spread=0.008,
        collapse=0.88,
        collapse_days=4,
        notes="held; 12 % off its high on the even-week date, still above EMA-100: trailing stop",
    ),
    Spec("IDEAX-BE", 1024, "held_be", -0.0015, 0.006, 12, notes="held -BE name, unranked; LIMIT sell at the bid"),
    Spec("ZETA-BE", 1025, "rank_buy_be", 0.0032, 0.005, 25, notes="-BE name that ranks; LIMIT buy at the ask"),
    # Each filter
    Spec("SLIDER", 1031, "below_ema100", -0.0025, 0.006, 300),
    Spec("SINKER", 1032, "below_ema100", -0.0015, 0.005, 1100),
    Spec("FADER", 1033, "below_ema100", -0.0008, 0.006, 55),
    Spec("NEWBIE", 1041, "history", 0.0030, 0.005, 200, days=60, notes="60 rows, below MIN_HISTORY"),
    Spec("ROOKIE", 1042, "history", 0.0020, 0.005, 150, days=90),
    Spec("GHOSTLY", 1043, "history", days=0, notes="listed, no candles at all"),
    Spec("THINLY", 1051, "volume", 0.0020, 0.005, 800, volume=2_000),
    Spec("QUIETLY", 1052, "volume", 0.0025, 0.005, 95, volume=5_000),
    Spec("JUMPY", 1061, "atr_pct", 0.0030, 0.060, 40, spread=0.12),
    Spec("WILDCAT", 1062, "atr_pct", 0.0020, 0.080, 15, spread=0.15),
    # Never reaches the ranking
    Spec("OUTSIDER", 1071, "outside_universe", 0.0030, 0.005, 500, in_universe=False, notes="not in nifty500.txt"),
    Spec("ALPHADR", 1072, "not_equity", 0.0030, 0.005, 1200, instrument_type="DR", notes="wrong instrument type"),
    # The regime index
    Spec("NIFTY 50", 256265, "index", 0.0006, 0.005, 22_000, volume=0, segment="INDICES", in_universe=False),
]

PORTFOLIO_BEFORE = {"HOLDFAST": 50, "ANCHOR": 2000, "DRIFTER": 150, "CLIFF": 100, "IDEAX-BE": 500}
SETTINGS = {
    "starting_cash": 500_000,
    "risk_factor": 0.002,
    "max_weight": 0.10,
    "max_positions": 6,  # two survivors plus four buys, so the fifth candidate hits the ceiling
    "cut_off_pct": 0.40,
    "min_volume": 10_000,
    "max_atr_pct": 0.10,
    "exit_multiple": 5.0,
    "allow_kite_execution": False,
}
CASH_LEDGER = [("2026-06-01", "250000.00", "deposit")]
TRADES_LEDGER = [  # the buys that built the fixture portfolio, priced well below today's closes
    ("2026-06-03T10:05:00+05:30", "BUY", "HOLDFAST", 50, "300.0000", "0.001500", "0.000500", "-15030.00"),
    ("2026-06-03T10:06:00+05:30", "BUY", "ANCHOR", 2000, "120.0000", "0.001500", "0.000500", "-240480.00"),
    ("2026-06-03T10:07:00+05:30", "BUY", "DRIFTER", 150, "450.0000", "0.001500", "0.000500", "-67635.00"),
    ("2026-06-10T10:05:00+05:30", "BUY", "CLIFF", 100, "500.0000", "0.001500", "0.000500", "-50100.00"),
    ("2026-07-01T10:05:00+05:30", "BUY", "IDEAX-BE", 500, "15.0000", "0.001500", "0.000500", "-7515.00"),
]
TRADE_COLUMNS = ["timestamp", "side", "symbol", "qty", "price", "fees_pct", "slippage_pct", "cash_delta"]


def business_days(end: date, count: int) -> list[date]:
    days: list[date] = []
    d = end
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def make_rows(spec: Spec, rng: np.random.Generator) -> list[tuple[str, float, float, float, float, int]]:
    if spec.days == 0:
        return []
    days = business_days(END, spec.days)
    steps = spec.drift + rng.normal(0.0, spec.vol, spec.days)
    closes = spec.price * np.exp(np.cumsum(steps))
    if spec.collapse is not None:
        # The drop ends on the even-week date, so both configurations see it: the even-week run
        # on its last four candles, the odd-week run one week later at the same level.
        end_idx = days.index(AS_OF["even_week"])
        n = spec.collapse_days
        ramp = np.linspace(1.0, spec.collapse, n + 1)[1:]
        closes[end_idx - n + 1 : end_idx + 1] = closes[end_idx - n] * ramp
        after = spec.days - end_idx - 1
        closes[end_idx + 1 :] = closes[end_idx] * np.exp(np.cumsum(rng.normal(0.0, spec.vol, after)))
    volumes = rng.lognormal(np.log(max(spec.volume, 1)), 0.35, spec.days) if spec.volume else np.zeros(spec.days)
    rows = []
    for d, close, vol in zip(days, closes, volumes, strict=True):
        close = round(float(close), 2)
        half = close * spec.spread / 2
        wobble = rng.normal(0.0, half * 0.3)
        high = round(close + half + abs(wobble), 2)
        low = round(max(close - half - abs(wobble), 0.05), 2)
        open_ = round(min(max(close + wobble, low), high), 2)
        stamp = datetime.combine(d, time.min, tzinfo=IST_OFFSET).isoformat(sep=" ")
        rows.append((stamp, open_, high, low, close, int(vol)))
    return rows


def write_csv(path: Path, header: list[str] | None, rows) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        if header:
            w.writerow(header)
        w.writerows(rows)


def main() -> None:
    rng = np.random.default_rng(SEED)
    candles_dir = HERE / "candles"
    candles_dir.mkdir(exist_ok=True)
    for old in candles_dir.glob("*.csv"):
        old.unlink()

    last_close: dict[str, float] = {}
    for spec in SPECS:
        rows = make_rows(spec, rng)
        write_csv(candles_dir / f"{spec.token}.csv", ["date", "open", "high", "low", "close", "volume"], rows)
        if rows:
            last_close[spec.symbol] = rows[-1][4]

    write_csv(
        HERE / "instruments.csv",
        ["instrument_token", "tradingsymbol", "exchange", "segment", "instrument_type"],
        [(s.token, s.symbol, "NSE", s.segment, s.instrument_type) for s in SPECS],
    )
    (HERE / "nifty500.txt").write_text("\n".join(sorted(s.symbol.split("-")[0] for s in SPECS if s.in_universe)) + "\n")
    # Last prices: today's close, nudged for two held names so mark-to-market differs from the candle
    nudges = {"HOLDFAST": 1.01, "DRIFTER": 0.99}
    write_csv(
        HERE / "ltp.csv",
        ["symbol", "last_price"],
        [(sym, round(px * nudges.get(sym, 1.0), 2)) for sym, px in sorted(last_close.items())],
    )
    write_csv(
        HERE / "quotes.csv",
        ["symbol", "last_price", "best_bid", "best_ask"],
        [
            (
                s.symbol,
                last_close[s.symbol],
                round(last_close[s.symbol] * 0.995, 2),
                round(last_close[s.symbol] * 1.005, 2),
            )
            for s in SPECS
            if s.symbol.endswith("-BE")
        ],
    )
    write_csv(HERE / "portfolio_before.csv", None, sorted(PORTFOLIO_BEFORE.items()))
    write_csv(HERE / "cash_ledger.csv", ["date", "amount", "note"], CASH_LEDGER)
    write_csv(HERE / "trades_ledger.csv", TRADE_COLUMNS, TRADES_LEDGER)
    meta = {
        "description": "Synthetic golden fixtures (ADR-009). Seeded random walks; no market data.",
        "generated_on": date.today().isoformat(),
        "generator": "tests/fixtures/golden/make_fixtures.py",
        "seed": SEED,
        "candles_end": END.isoformat(),
        "configs": {
            name: {"as_of": datetime.combine(d, time(10, 0), tzinfo=IST).isoformat(), "iso_week": d.isocalendar().week}
            for name, d in AS_OF.items()
        },
        "settings": SETTINGS,
        "portfolio_before": PORTFOLIO_BEFORE,
        "instruments": [s.as_meta() for s in SPECS],
    }
    (HERE / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {len(SPECS)} instruments to {HERE}; candles end {END}; as_of {AS_OF}")


if __name__ == "__main__":
    main()
