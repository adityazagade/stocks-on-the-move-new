"""Runtime configuration as one validated object (ADR-007).

Every knob the strategy reads from the environment is a field here, under the
same variable name and with the same default it had as a module constant in
``momentum.py``. ``Settings.from_env()`` reads the process environment once,
at the start of ``main()``; nothing is read at import time, and uv's
``--env-file`` is the only ``.env`` loader.

``uv run python -m stocks_on_the_move.settings --example`` renders
``.env.example`` from the model; a test keeps the committed file equal to
that output. ``--check`` loads the environment and prints the result with
secrets masked, which is the quickest way to see why a run refuses to start.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import annotated_types
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class SettingsError(ValueError):
    """The environment holds a value the model rejects. The message names every offending variable."""


class Settings(BaseSettings):
    """All configuration, validated once. Field names are the environment variable names in lower case."""

    model_config = SettingsConfigDict(env_file=None, frozen=True, validate_default=True)

    # ── Kite credentials ─────────────────────────────────────────────────
    kite_api_key: SecretStr = Field(min_length=1, description="Kite Connect API key from the developer console.")
    kite_api_secret: SecretStr = Field(
        min_length=1, description="Kite Connect API secret. Rotate it if it ever lands in a chat or a log."
    )

    # ── Kite session (ADR-005) ───────────────────────────────────────────
    kite_session_file: Path = Field(
        Path("~/.config/stocks-on-the-move/kite_session.json"),
        description="Where the day's access token is cached, mode 0600. A leading ~ expands.",
    )
    kite_redirect_port: int = Field(
        8765,
        ge=0,
        le=65535,
        description=(
            "Loopback port that captures the login redirect. The app's redirect URL in the Kite console "
            "must be http://127.0.0.1:<port>/. 0 disables the listener; the token is pasted instead."
        ),
    )
    kite_open_browser: bool = Field(True, description="Open the login URL in the default browser.")
    kite_forget_session: bool = Field(False, description="Discard the cached session and log in afresh.")

    # ── Safety switches ──────────────────────────────────────────────────
    allow_kite_execution: bool = Field(
        True, description="0 = paper mode: run the whole pipeline and write the ledgers, but send no orders."
    )
    kill_switch: bool = Field(False, description="1 = liquidate everything and exit, ignoring the weekday guard.")
    force_resize: bool = Field(False, description="1 = rebalance position sizes even on an odd ISO week.")

    # ── Universe and regime ──────────────────────────────────────────────
    index_symbol: str = Field("NIFTY 50", description="Index whose 200-day EMA sets the bull or bear regime.")
    index_exchange: str = Field("NSE", description="Exchange segment of that index.")
    use_full_nifty_universe: bool = Field(
        False, description="1 = rank every NSE equity instead of the NIFTY 500 constituents."
    )

    # ── Sizing and risk ──────────────────────────────────────────────────
    account_value: float = Field(100_000, ge=0, description="Legacy account size. Only the default for STARTING_CASH.")
    starting_cash: float = Field(
        default_factory=lambda data: data["account_value"],
        ge=0,
        description="Cash before any ledger row. Changing it changes the meaning of every historical row.",
        json_schema_extra={"example_default": "<same as ACCOUNT_VALUE>"},
    )
    risk_factor: float = Field(0.001, ge=0, description="Fraction of equity risked per ATR when sizing a position.")
    atr_period: int = Field(20, gt=0, description="Days in the ATR used for sizing and the trailing stop.")
    max_weight: float = Field(0.10, gt=0, le=1, description="Cap on one position as a fraction of equity.")
    max_positions: int = Field(25, gt=0, description="Stop opening new positions at this count.")
    cut_off_pct: float = Field(
        0.20, gt=0, le=1, description="Hold and buy only names ranked in this top fraction of the universe."
    )
    exit_multiple: float = Field(
        5.0, gt=0, description="Trailing stop: sell when the close is this many ATRs below the 40-day high close."
    )
    min_volume: int = Field(10_000, ge=0, description="Minimum 20-day average volume for a name to be ranked.")
    max_atr_pct: float = Field(0.10, gt=0, description="Skip names whose ATR exceeds this fraction of their price.")

    # ── Scheduling ───────────────────────────────────────────────────────
    trading_weekday: int = Field(
        2, ge=0, le=6, description="Weekday (IST) the run is allowed on: 0 = Monday ... 6 = Sunday."
    )

    # ── Cash flow for this run ───────────────────────────────────────────
    env_cashflow: float = Field(
        0.0,
        description=(
            "Deposit (+) or withdrawal (-) appended to the cash ledger when the run starts. "
            "Set it for exactly one run; never leave it in .env."
        ),
    )
    cashflow_note: str = Field("env-cashflow", description="Note column of that ledger row.")

    # ── Friction ─────────────────────────────────────────────────────────
    fees_pct: float = Field(0.0015, ge=0, description="All-in fee fraction booked on every trade.")
    slippage_pct: float = Field(0.0005, ge=0, description="Slippage fraction booked on every trade.")

    # ── Order confirmation (ADR-019) ─────────────────────────────────────
    fill_timeout_seconds: int = Field(
        120, ge=1, le=900, description="Seconds to wait for an order to fill before cancelling what has not."
    )
    fill_poll_seconds: float = Field(2.0, ge=0.5, le=30, description="Seconds between two order-status polls.")

    # ── Kite rate limiting ───────────────────────────────────────────────
    kite_rps: float = Field(2.0, ge=0, description="Ceiling on Kite REST calls per second (floored at 0.1).")
    kite_max_retries: int = Field(6, ge=1, description="Attempts per Kite call while it answers 'too many requests'.")
    candle_sleep_sec: float = Field(0.15, ge=0, description="Pause after each historical-data download.")

    # ── Logging (ADR-015) ────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        "INFO",
        description="Console log level. The run's log file under RUNS_DIR always gets DEBUG.",
    )

    # ── Files ────────────────────────────────────────────────────────────
    portfolio_file: str = Field("current_portfolio.csv", description="Positions going into the run: SYMBOL,QUANTITY.")
    out_file: str = Field("next_portfolio.csv", description="Positions after the run.")
    cash_ledger_file: str = Field("cash_ledger.csv", description="Deposits and withdrawals: date, amount, note.")
    trades_ledger_file: str = Field("trades_ledger.csv", description="Every placed or paper trade and its cash effect.")
    cache_dir: Path = Field(Path(".cache_candles"), description="Per-instrument daily-candle cache; regenerable.")
    runs_dir: Path = Field(
        Path("runs"),
        description="Per-run artifacts (ADR-006): a directory per run with the ranking, exits, sizes, trades and log.",
    )

    @field_validator("*", mode="before")
    @classmethod
    def _strip_strings(cls, value: Any) -> Any:
        """A stray space in the environment must not turn a value into junk or, worse, into 'true'."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("kite_session_file", "cache_dir", "runs_dir")
    @classmethod
    def _expand_user(cls, value: Path) -> Path:
        return value.expanduser()

    @property
    def kite_min_interval(self) -> float:
        """Seconds between Kite calls implied by KITE_RPS."""
        return 1.0 / max(self.kite_rps, 0.1)

    @classmethod
    def from_env(cls) -> Settings:
        """Read the process environment; raise SettingsError naming every bad variable."""
        try:
            return cls()
        except ValidationError as exc:
            raise SettingsError(describe_errors(exc)) from None

    @classmethod
    def from_values(cls, **values: Any) -> Settings:
        """Build from explicit values only, ignoring the environment. For tests and tools."""
        return _ExplicitSettings(**values)


class _ExplicitSettings(Settings):
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def describe_errors(exc: ValidationError) -> str:
    """One line per problem, ENV_NAME first. Values are left out: one of them may be a secret."""
    lines = ["Configuration error in the environment:"]
    for err in exc.errors():
        if err["type"] == "default_factory_not_called":
            continue  # STARTING_CASH could not be derived because ACCOUNT_VALUE failed; that error is listed
        loc = err["loc"]
        name = str(loc[0]).upper() if loc else "<settings>"
        lines.append(f"  {name}: {err['msg']}")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# .env.example generator
# ═════════════════════════════════════════════════════════════════════════
# Field names per section, in the order the example prints them. A test checks
# that every field of Settings appears here exactly once.
EXAMPLE_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Kite credentials (required)", ("kite_api_key", "kite_api_secret")),
    (
        "Kite session (ADR-005)",
        ("kite_session_file", "kite_redirect_port", "kite_open_browser", "kite_forget_session"),
    ),
    ("Safety switches", ("allow_kite_execution", "kill_switch", "force_resize")),
    ("Universe and regime", ("index_symbol", "index_exchange", "use_full_nifty_universe")),
    (
        "Sizing and risk",
        (
            "account_value",
            "starting_cash",
            "risk_factor",
            "atr_period",
            "max_weight",
            "max_positions",
            "cut_off_pct",
            "exit_multiple",
            "min_volume",
            "max_atr_pct",
        ),
    ),
    ("Scheduling", ("trading_weekday",)),
    ("Cash flow for this run", ("env_cashflow", "cashflow_note")),
    ("Friction", ("fees_pct", "slippage_pct")),
    ("Order confirmation (ADR-019)", ("fill_timeout_seconds", "fill_poll_seconds")),
    ("Kite rate limiting", ("kite_rps", "kite_max_retries", "candle_sleep_sec")),
    ("Logging", ("log_level",)),
    ("Files", ("portfolio_file", "out_file", "cash_ledger_file", "trades_ledger_file", "cache_dir", "runs_dir")),
)

EXAMPLE_HEADER = """\
# Generated from src/stocks_on_the_move/settings.py by
#   uv run python -m stocks_on_the_move.settings --example > .env.example
# Edit the model, not this file; a test keeps the two identical.
#
# Copy to .env, fill in the two credentials, then run:
#   uv run --env-file .env stocks-on-the-move
# Commented lines show the default; uncomment to override.
# Booleans accept 1/0, true/false, yes/no, on/off. Anything else refuses to start.
"""


def _format_default(info: FieldInfo) -> str:
    extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
    if "example_default" in extra:
        return str(extra["example_default"])
    value = info.default
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _format_range(info: FieldInfo) -> str:
    parts = []
    for meta in info.metadata:
        if isinstance(meta, annotated_types.Ge):
            parts.append(f"at least {meta.ge}")
        elif isinstance(meta, annotated_types.Gt):
            parts.append(f"more than {meta.gt}")
        elif isinstance(meta, annotated_types.Le):
            parts.append(f"at most {meta.le}")
        elif isinstance(meta, annotated_types.Lt):
            parts.append(f"less than {meta.lt}")
    return ", ".join(parts)


def render_example() -> str:
    """The text of .env.example: every field, its description, its range and its default."""
    out = [EXAMPLE_HEADER]
    for title, names in EXAMPLE_SECTIONS:
        out.append(f"# ── {title} ──")
        for name in names:
            info = Settings.model_fields[name]
            out.extend(f"# {line}" for line in textwrap.wrap(info.description or "", width=96))
            if bounds := _format_range(info):
                out.append(f"# Range: {bounds}.")
            env = name.upper()
            out.append(f"{env}=" if info.is_required() else f"# {env}={_format_default(info)}")
        out.append("")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stocks_on_the_move.settings", description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--example", action="store_true", help="print the text of .env.example")
    action.add_argument("--check", action="store_true", help="load settings from the environment and show them")
    args = parser.parse_args(argv)
    if args.example:
        sys.stdout.write(render_example())
        return 0
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 2
    for name, value in settings.model_dump().items():
        print(f"{name.upper()}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
