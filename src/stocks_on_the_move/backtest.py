"""Replay the weekly pipeline over the candle cache with the live rules (ADR-023).

Three commands behind ``python -m stocks_on_the_move.backtest``:

- ``warm``: log in once and fetch years of daily candles for the universe and
  the regime index into the same per-token cache the live run reads, plus the
  instrument list the replay needs. Everything after this is offline.
- ``run``: walk every configured trading weekday from ``--from`` to ``--to``.
  For each run date the pipeline sees candles strictly before that date, every
  intent fills in full at that date's close through ``PlanExecutor``, fees and
  slippage are booked as a paper run books them, and the harness carries the
  positions and the trade ledger to the next date in files under its output
  directory. ``--set NAME=VALUE`` overrides a ``StrategyParams`` field.
- ``compare``: the summaries of several labelled runs side by side.

The universe is today's constituents over the whole range, which flatters
every variant alike, and fills at the close with fixed slippage flatter thin
names. The summary says so on its first line: the numbers compare variants,
they do not predict live returns.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import dataclasses
import json
import logging
import math
import statistics
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, get_type_hints
from zoneinfo import ZoneInfo

import pandas as pd

from stocks_on_the_move.artifacts import NoArtifacts, settings_snapshot
from stocks_on_the_move.broker import Broker, Candle, Instrument, Order, OrderStatus, Quote
from stocks_on_the_move.candles import window
from stocks_on_the_move.context import RunContext
from stocks_on_the_move.execution import live_value
from stocks_on_the_move.ledger import TRADE_COLUMNS, save_state, write_portfolio
from stocks_on_the_move.logging_setup import configure_logging
from stocks_on_the_move.momentum import authenticate
from stocks_on_the_move.params import StrategyParams
from stocks_on_the_move.pipeline import run
from stocks_on_the_move.settings import Settings, SettingsError
from stocks_on_the_move.universe import NseArchives, StaticUniverse, base_symbol

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENTS_FILE = "instruments-nse.csv"
CHUNK_DAYS = 1900  # Kite serves at most 2,000 days of daily candles per request
SUMMARY_NOTE = (
    "Relative comparison only: today's constituents over the whole range (survivorship bias), "
    "every intent filled at the run date's close with fixed slippage, no delistings."
)
INSTRUMENT_COLUMNS = ["instrument_token", "tradingsymbol", "exchange", "segment", "instrument_type"]


# ── the instrument list, saved once by warm-up ───────────────────────────
def instruments_path(settings: Settings) -> Path:
    return Path(settings.cache_dir) / INSTRUMENTS_FILE


def save_instruments(path: Path, instruments: Iterable[Instrument]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(INSTRUMENT_COLUMNS)
        for i in instruments:
            writer.writerow([i.token, i.tradingsymbol, i.exchange, i.segment, i.instrument_type])


def load_instruments(path: Path) -> list[Instrument]:
    with path.open(newline="") as f:
        return [
            Instrument(
                int(r["instrument_token"]), r["tradingsymbol"], r["exchange"], r["segment"], r["instrument_type"]
            )
            for r in csv.DictReader(f)
        ]


def universe_instruments(instruments: Iterable[Instrument], universe: set[str], settings: Settings) -> list[Instrument]:
    """The NSE equities whose base symbol is in the universe, plus the regime index."""
    out = [
        i
        for i in instruments
        if i.instrument_type == "EQ" and i.segment == "NSE" and base_symbol(i.tradingsymbol) in universe
    ]
    index = [
        i for i in instruments if i.tradingsymbol == settings.index_symbol and i.exchange == settings.index_exchange
    ]
    return out + index


# ── warm-up: years of candles into the same cache the live run reads ─────
def warm_cache(
    broker: Broker,
    settings: Settings,
    universe: set[str],
    *,
    history_days: int,
    today: date,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Fetch ``history_days`` of daily candles per instrument in chunks Kite accepts, merging into the cache files.

    Writes the instrument list beside them. Returns counts for the log.
    """
    instruments = broker.instruments("NSE")
    save_instruments(instruments_path(settings), instruments)
    wanted = universe_instruments(instruments, universe, settings)
    start = today - timedelta(days=history_days)
    cache_dir = Path(settings.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    requests = 0
    for inst in wanted:
        frames: list[pd.DataFrame] = []
        chunk_start = start
        while chunk_start <= today:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), today)
            rows: list[Candle] = broker.historical_data(inst.token, chunk_start, chunk_end, "day")
            requests += 1
            if rows:
                frames.append(pd.DataFrame(rows))
            chunk_start = chunk_end + timedelta(days=1)
        if not frames:
            continue
        fresh = pd.concat(frames, ignore_index=True)
        fresh["date"] = pd.to_datetime(fresh["date"])
        path = cache_dir / f"{inst.token}.csv"
        if path.exists():
            cached = pd.read_csv(path, parse_dates=["date"])
            fresh = pd.concat([cached, fresh], ignore_index=True)
        merged = fresh.drop_duplicates(subset="date", keep="last").sort_values(by="date").reset_index(drop=True)
        merged.to_csv(path, index=False)
        sleep(settings.candle_sleep_sec)
    return {"instruments": len(instruments), "tokens": len(wanted), "requests": requests}


# ── the replay's data: candles strictly before the run date, prices at its close ──
def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])


class ReplayCandles:
    """``CandleSource`` over frames loaded once from the cache; ``get`` sees only candles before ``as_of``."""

    def __init__(self, frames: dict[int, pd.DataFrame]) -> None:
        self._frames: dict[int, pd.DataFrame] = {}
        self._days: dict[int, Any] = {}
        self._closes: dict[int, dict[date, float]] = {}
        for token, frame in frames.items():
            if frame.empty:
                continue
            frame = frame.sort_values(by="date").reset_index(drop=True)
            days = pd.to_datetime(frame["date"], utc=True).dt.tz_convert("Asia/Kolkata").dt.date
            self._frames[token] = frame
            self._days[token] = days.to_numpy()
            self._closes[token] = dict(zip(days, frame["close"].astype(float), strict=True))
        self.as_of: date | None = None

    @classmethod
    def load(cls, cache_dir: Path, tokens: Iterable[int]) -> ReplayCandles:
        frames = {}
        for token in tokens:
            path = Path(cache_dir) / f"{token}.csv"
            if path.exists():
                frames[token] = pd.read_csv(path, parse_dates=["date"])
        return cls(frames)

    @property
    def tokens(self) -> set[int]:
        return set(self._frames)

    def get(self, token: int, days: int) -> pd.DataFrame:
        frame = self._frames.get(token)
        if frame is None or self.as_of is None:
            return _empty_frame()
        start_d, end_d = window(days, self.as_of - timedelta(days=1))
        day = self._days[token]
        mask = (day >= start_d) & (day <= end_d)
        return frame[mask].reset_index(drop=True)

    def close_on(self, token: int, day: date) -> float | None:
        return self._closes.get(token, {}).get(day)

    def trading_days(self, token: int) -> list[date]:
        return sorted(self._closes.get(token, {}))


class ReplayBroker:
    """A read-only ``Broker`` over the replay's candles, priced at the run date's close. It never sends an order."""

    def __init__(self, instruments: Sequence[Instrument], candles: ReplayCandles) -> None:
        self._instruments = list(instruments)
        self._candles = candles
        self._token_of = {f"{i.exchange}:{i.tradingsymbol}": i.token for i in instruments}
        self.as_of: date | None = None

    def instruments(self, exchange: str) -> list[Instrument]:
        return [i for i in self._instruments if i.exchange == exchange]

    def ltp(self, keys: Iterable[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        if self.as_of is None:
            return out
        for key in keys:
            token = self._token_of.get(key)
            close = self._candles.close_on(token, self.as_of) if token is not None else None
            if close is not None:
                out[key] = close
        return out

    def quote(self, keys: Iterable[str]) -> dict[str, Quote]:
        return {key: Quote(price, price, price) for key, price in self.ltp(keys).items()}

    def historical_data(self, token: int, from_date: date, to_date: date, interval: str = "day") -> list[Candle]:
        return []  # the replay never fetches; a symbol missing from the cache is simply short of history

    def place_order(self, order: Order) -> str:
        raise RuntimeError("the replay sends no orders; every intent fills through PlanExecutor")

    def order_status(self, order_id: str) -> OrderStatus:
        raise RuntimeError("the replay sends no orders")

    def cancel_order(self, order_id: str) -> None:
        raise RuntimeError("the replay sends no orders")

    def profile(self) -> dict[str, Any]:
        return {"user_id": "REPLAY"}


# ── run dates ────────────────────────────────────────────────────────────
def run_dates(trading_days: Sequence[date], start: date, end: date, weekday: int) -> list[date]:
    """Every ``weekday`` from ``start`` to ``end``, each moved to the next trading day in its week when it has none.

    A week with no trading day left after the weekday is skipped rather than run
    on the following Monday next to that week's own run.
    """
    days = sorted(set(trading_days))
    out: list[date] = []
    d = start
    while d.weekday() != weekday:
        d += timedelta(days=1)
    while d <= end:
        pos = bisect.bisect_left(days, d)
        week_end = d + timedelta(days=6 - d.weekday())
        if pos < len(days) and days[pos] <= min(week_end, end):
            out.append(days[pos])
        d += timedelta(days=7)
    return out


# ── parameter overrides ─────────────────────────────────────────────────
def parse_overrides(pairs: Iterable[str], base: StrategyParams) -> StrategyParams:
    """``NAME=VALUE`` pairs applied to a parameter set, typed by the field they name."""
    hints = get_type_hints(StrategyParams)
    updates: dict[str, Any] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        if not sep or name not in hints:
            raise ValueError(f"cannot set {pair!r}: choose one of {', '.join(sorted(hints))} as NAME=VALUE")
        kind = hints[name]
        try:
            updates[name] = int(raw) if kind is int else float(raw)
        except ValueError as exc:
            raise ValueError(f"cannot set {name}={raw!r}: expected {kind.__name__}") from exc
    return dataclasses.replace(base, **updates)


# ── the simulation ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class WeekResult:
    date: date
    cash: float
    market_value: float
    equity: float
    positions: int
    exposure: float
    bull: bool | None
    ranked: int | None
    exits: int
    buys: int
    resize_performed: bool | None


@dataclass(frozen=True)
class Backtest:
    """Everything one replay needs, assembled by ``main`` or a test."""

    settings: Settings
    params: StrategyParams
    universe: frozenset[str]
    instruments: Sequence[Instrument]
    candles: ReplayCandles
    dates: Sequence[date]
    label: str


class _Recorder(NoArtifacts):
    """Keeps what the run records (regime, counts); every table is dropped."""

    def __init__(self) -> None:
        self.fields: dict[str, Any] = {}

    def record(self, **fields: Any) -> None:
        self.fields.update(fields)


@contextmanager
def quiet(level: int = logging.WARNING):
    """Raise the package logger to ``level`` for the block: a replay runs the pipeline hundreds of times."""
    pkg = logging.getLogger("stocks_on_the_move")
    old = pkg.level
    pkg.setLevel(level)
    try:
        yield
    finally:
        pkg.setLevel(old)


def _sim_settings(settings: Settings, state_dir: Path) -> Settings:
    """The live settings, redirected: plan mode, no execution, and every state file under the replay's directory."""
    values = settings.model_dump()
    for name in ("kite_api_key", "kite_api_secret"):
        values[name] = values[name].get_secret_value()
    values.update(
        plan_only=True,
        allow_kite_execution=False,
        kill_switch=False,
        force_resize=False,
        env_cashflow=0.0,
        portfolio_file=str(state_dir / "portfolio.csv"),
        out_file=str(state_dir / "next_portfolio.csv"),
        cash_ledger_file=str(state_dir / "cash_ledger.csv"),
        trades_ledger_file=str(state_dir / "trades_ledger.csv"),
        state_file=str(state_dir / "strategy_state.json"),
        runs_dir=state_dir / "runs",
    )
    return Settings.from_values(**values)


def _append_rows(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(c, "") for c in columns])


def _write_table(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_cell(row.get(c)) for c in columns])


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.6f}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def simulate(bt: Backtest, out_dir: Path) -> dict[str, Any]:
    """Replay every run date, write the five output files, return the summary."""
    state = out_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    for name in ("portfolio.csv", "next_portfolio.csv", "cash_ledger.csv", "trades_ledger.csv", "strategy_state.json"):
        (state / name).unlink(missing_ok=True)
    sim = _sim_settings(bt.settings, state)
    broker = ReplayBroker(bt.instruments, bt.candles)
    universe = StaticUniverse(bt.universe)

    weeks: list[WeekResult] = []
    trades: list[dict[str, Any]] = []
    for n, d in enumerate(bt.dates, start=1):
        bt.candles.as_of = d
        broker.as_of = d
        recorder = _Recorder()
        ctx = RunContext(
            settings=sim,
            broker=broker,
            candles=bt.candles,
            now=lambda d=d: datetime.combine(d, dtime(10, 0), tzinfo=IST),
            paper=True,
            universe=universe,
            artifacts=recorder,
            sleep=lambda _: None,
            params=bt.params,
        )
        with quiet():
            run(ctx)
            pf = ctx.portfolio
            _append_rows(state / "trades_ledger.csv", TRADE_COLUMNS, pf.trades)  # next date's cash comes from here
            write_portfolio(str(state / "portfolio.csv"), pf.positions)
            if recorder.fields.get("resize_performed"):  # a plan does not write the date; the harness carries it
                save_state(str(state / "strategy_state.json"), {"last_resize_date": d.isoformat()})
            market_value = live_value(ctx)
        filled = [(intent, fill) for intent, fill in pf.intents if fill is not None and fill.filled > 0]
        for (intent, _), row in zip(filled, pf.trades, strict=True):
            trades.append({**row, "date": d.isoformat(), "reason": intent.reason})
        equity = pf.cash + market_value
        regime_info = recorder.fields.get("regime") or {}
        weeks.append(
            WeekResult(
                date=d,
                cash=pf.cash,
                market_value=market_value,
                equity=equity,
                positions=len(pf.positions),
                exposure=market_value / equity if equity > 0 else 0.0,
                bull=regime_info.get("bull"),
                ranked=recorder.fields.get("ranked_count"),
                exits=sum(1 for intent, _ in filled if intent.reason.startswith("exit")),
                buys=sum(1 for intent, _ in filled if intent.reason == "new_position"),
                resize_performed=recorder.fields.get("resize_performed"),
            )
        )
        if n % 26 == 0 or n == len(bt.dates):
            logger.info(
                "%s: %d/%d run dates, equity %.0f, %d positions", bt.label, n, len(bt.dates), equity, len(pf.positions)
            )

    summary = {"note": SUMMARY_NOTE, "label": bt.label, "from": bt.dates[0].isoformat(), "to": bt.dates[-1].isoformat()}
    summary.update(metrics(weeks, trades))
    _write_table(
        out_dir / "equity.csv",
        ["date", "cash", "market_value", "equity", "positions", "exposure"],
        [dataclasses.asdict(w) for w in weeks],
    )
    _write_table(
        out_dir / "weekly.csv",
        ["date", "bull", "ranked", "exits", "buys", "resize_performed", "equity"],
        [dataclasses.asdict(w) for w in weeks],
    )
    _write_table(out_dir / "trades.csv", ["date", *TRADE_COLUMNS, "reason"], trades)
    (out_dir / "params.json").write_text(
        json.dumps(
            {
                "label": bt.label,
                "from": bt.dates[0].isoformat(),
                "to": bt.dates[-1].isoformat(),
                "run_dates": len(bt.dates),
                "universe_size": len(bt.universe),
                "params": dataclasses.asdict(bt.params),
                "settings": settings_snapshot(bt.settings),
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def metrics(weeks: Sequence[WeekResult], trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The summary's numbers from the weekly equity series and the trades."""
    equity = [w.equity for w in weeks]
    start, end = equity[0], equity[-1]
    days = max((weeks[-1].date - weeks[0].date).days, 1)
    years = days / 365.0
    cagr = (end / start) ** (1 / years) - 1 if start > 0 and len(weeks) > 1 else 0.0
    returns = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity)) if equity[i - 1] > 0]
    volatility = statistics.pstdev(returns) * math.sqrt(52) if len(returns) > 1 else 0.0
    peak, peak_date, trough_date, max_dd = equity[0], weeks[0].date, weeks[0].date, 0.0
    running_peak_date = weeks[0].date
    for w in weeks:
        if w.equity > peak:
            peak, running_peak_date = w.equity, w.date
        dd = w.equity / peak - 1 if peak > 0 else 0.0
        if dd < max_dd:
            max_dd, peak_date, trough_date = dd, running_peak_date, w.date
    notional = sum(float(t["qty"]) * float(t["price"]) for t in trades)
    mean_equity = statistics.fmean(equity) if equity else 0.0
    return {
        "weeks": len(weeks),
        "start_equity": round(start, 2),
        "end_equity": round(end, 2),
        "cagr": round(cagr, 6),
        "annual_volatility": round(volatility, 6),
        "return_over_volatility": round(cagr / volatility, 4) if volatility else None,
        "max_drawdown": round(max_dd, 6),
        "max_drawdown_peak": peak_date.isoformat(),
        "max_drawdown_trough": trough_date.isoformat(),
        "avg_positions": round(statistics.fmean(w.positions for w in weeks), 2),
        "max_positions": max(w.positions for w in weeks),
        "avg_exposure": round(statistics.fmean(w.exposure for w in weeks), 4),
        "trades": len(trades),
        "trades_per_year": round(len(trades) / years, 2),
        "turnover_per_year": round(notional / mean_equity / years, 4) if mean_equity else None,
    }


# ── the command ──────────────────────────────────────────────────────────
COMPARE_FIELDS = [
    "cagr",
    "annual_volatility",
    "return_over_volatility",
    "max_drawdown",
    "avg_positions",
    "avg_exposure",
    "trades_per_year",
    "turnover_per_year",
]


def backtests_dir(settings: Settings) -> Path:
    return Path(settings.runs_dir) / "backtests"


def find_summary(settings: Settings, label: str) -> Path:
    matches = sorted(backtests_dir(settings).glob(f"*-{label}/summary.json"))
    if not matches:
        raise FileNotFoundError(f"no backtest labelled {label!r} under {backtests_dir(settings)}")
    return matches[-1]


def compare_table(summaries: Sequence[dict[str, Any]]) -> str:
    labels = [str(s["label"]) for s in summaries]
    width = max(24, *(len(label) for label in labels))
    lines = [" " * 24 + "".join(f"{label:>{width}}" for label in labels)]
    for field_name in ["from", "to", "weeks", *COMPARE_FIELDS]:
        cells = []
        for s in summaries:
            value = s.get(field_name)
            cells.append(f"{value:>{width}.4f}" if isinstance(value, float) else f"{str(value):>{width}}")
        lines.append(f"{field_name:<24}" + "".join(cells))
    return "\n".join(lines)


def _build(settings: Settings, start: date, end: date, label: str, overrides: Sequence[str]) -> Backtest:
    path = instruments_path(settings)
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing: run `python -m stocks_on_the_move.backtest warm` first")
    instruments = load_instruments(path)
    saved = NseArchives(settings).saved_symbols()
    if not saved:
        raise FileNotFoundError(f"no saved universe under {settings.cache_dir}: run `warm` first")
    wanted = universe_instruments(instruments, saved, settings)
    candles = ReplayCandles.load(Path(settings.cache_dir), [i.token for i in wanted])
    index = [i for i in wanted if i.tradingsymbol == settings.index_symbol]
    if not index or index[0].token not in candles.tokens:
        raise FileNotFoundError(f"no candles for the regime index {settings.index_symbol!r}: run `warm` first")
    dates = run_dates(candles.trading_days(index[0].token), start, end, settings.trading_weekday)
    if not dates:
        raise ValueError(f"no run dates between {start} and {end} with index candles; is the cache warm that far back?")
    params = parse_overrides(overrides, StrategyParams.from_settings(settings))
    return Backtest(settings, params, frozenset(saved), instruments, candles, dates, label)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m stocks_on_the_move.backtest", description=(__doc__ or "").split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    warm = sub.add_parser("warm", help="fetch years of candles and the instrument list into the cache (logs in)")
    warm.add_argument(
        "--history-days", type=int, default=5 * 365, help="calendar days of history to fetch (default 1825)"
    )
    runp = sub.add_parser("run", help="replay the pipeline over the cache and write a labelled result")
    runp.add_argument("--from", dest="start", type=date.fromisoformat, required=True, help="first run date, YYYY-MM-DD")
    runp.add_argument("--to", dest="end", type=date.fromisoformat, required=True, help="last run date, YYYY-MM-DD")
    runp.add_argument("--label", default="baseline", help="name of this variant (default baseline)")
    runp.add_argument(
        "--set", action="append", default=[], metavar="NAME=VALUE", help="override a StrategyParams field"
    )
    comp = sub.add_parser("compare", help="print the summaries of labelled runs side by side")
    comp.add_argument("labels", nargs="+")
    args = parser.parse_args(argv)

    configure_logging("INFO")
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        logger.error("%s", exc)
        return 2
    configure_logging(settings.log_level)

    if args.command == "warm":
        source = NseArchives(settings)
        universe = source.symbols()
        if not universe:
            logger.error("Empty universe; nothing to warm")
            return 1
        broker = authenticate(settings)
        counts = warm_cache(broker, settings, universe, history_days=args.history_days, today=datetime.now(IST).date())
        logger.info(
            "Warm: %d instruments saved, %d tokens fetched in %d requests into %s",
            counts["instruments"],
            counts["tokens"],
            counts["requests"],
            settings.cache_dir,
        )
        return 0

    if args.command == "run":
        try:
            bt = _build(settings, args.start, args.end, args.label, args.set)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("%s", exc)
            return 1
        out_dir = backtests_dir(settings) / f"{bt.dates[0].isoformat()}_{bt.dates[-1].isoformat()}-{bt.label}"
        logger.info("Replaying %d run dates from %s to %s into %s", len(bt.dates), bt.dates[0], bt.dates[-1], out_dir)
        summary = simulate(bt, out_dir)
        print(summary["note"])
        print(compare_table([summary]))
        return 0

    summaries = []
    for label in args.labels:
        try:
            summaries.append(json.loads(find_summary(settings, label).read_text()))
        except FileNotFoundError as exc:
            logger.error("%s", exc)
            return 1
    print(SUMMARY_NOTE)
    print(compare_table(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
