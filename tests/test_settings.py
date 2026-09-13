"""Tests for the typed settings object and the generated .env.example (ADR-007)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from stocks_on_the_move import settings as st
from stocks_on_the_move.settings import Settings, SettingsError

REPO_ROOT = Path(__file__).resolve().parents[1]
CREDS = {"kite_api_key": "test-key", "kite_api_secret": "test-secret"}


def build(**overrides) -> Settings:
    return Settings.from_values(**{**CREDS, **overrides})


# ── the generated example file ───────────────────────────────────────────


def test_env_example_matches_the_generator():
    committed = (REPO_ROOT / ".env.example").read_text()
    assert committed == st.render_example(), (
        "regenerate with: uv run python -m stocks_on_the_move.settings --example > .env.example"
    )


def test_every_field_appears_in_exactly_one_example_section():
    listed = [name for _, names in st.EXAMPLE_SECTIONS for name in names]
    assert sorted(listed) == sorted(Settings.model_fields)
    assert len(listed) == len(set(listed))


def test_example_shows_required_credentials_blank_and_defaults_commented():
    text = st.render_example()
    assert "\nKITE_API_KEY=\n" in text
    assert "\n# TRADING_WEEKDAY=2\n" in text
    assert "\n# ALLOW_KITE_EXECUTION=1\n" in text
    assert "\n# STARTING_CASH=<same as ACCOUNT_VALUE>\n" in text
    assert "\n# RUNS_DIR=runs\n" in text and "\n# PORTFOLIO_FILE=runs/current_portfolio.csv\n" in text
    assert text.index("# RUNS_DIR=") < text.index("# PORTFOLIO_FILE=")  # the directory before the files it holds
    assert "# Range: at least 0, at most 6." in text
    assert "test-secret" not in text


# ── defaults and derived values ──────────────────────────────────────────


def test_defaults_match_the_former_module_constants():
    s = build()
    assert (s.allow_kite_execution, s.kill_switch, s.force_resize) == (True, False, False)
    assert (s.index_symbol, s.index_exchange, s.use_full_nifty_universe) == ("NIFTY 50", "NSE", False)
    assert (s.risk_factor, s.atr_period, s.max_weight, s.max_positions) == (0.001, 20, 0.10, 25)
    assert (s.cut_off_pct, s.exit_multiple, s.min_volume, s.max_atr_pct) == (0.20, 5.0, 10_000, 0.10)
    assert (s.trading_weekday, s.env_cashflow, s.cashflow_note) == (2, 0.0, "env-cashflow")
    assert (s.fees_pct, s.slippage_pct) == (0.0015, 0.0005)
    assert (s.kite_rps, s.kite_max_retries, s.candle_sleep_sec) == (2.0, 6, 0.15)
    assert (s.portfolio_file, s.out_file) == ("runs/current_portfolio.csv", "runs/next_portfolio.csv")
    assert (s.cash_ledger_file, s.trades_ledger_file) == ("runs/cash_ledger.csv", "runs/trades_ledger.csv")
    assert (s.state_file, s.runs_dir) == ("runs/strategy_state.json", Path("runs"))
    assert s.cache_dir == Path(".cache_candles")
    assert (s.kite_redirect_port, s.kite_open_browser, s.kite_forget_session) == (8765, True, False)
    assert s.kite_session_file == Path.home() / ".config" / "stocks-on-the-move" / "kite_session.json"


def test_state_files_default_under_runs_dir_and_follow_it():
    s = build(runs_dir="~/data/sotm")
    home = Path.home() / "data" / "sotm"
    assert s.runs_dir == home
    assert s.portfolio_file == str(home / "current_portfolio.csv")
    assert s.trades_ledger_file == str(home / "trades_ledger.csv")
    assert s.state_file == str(home / "strategy_state.json")


def test_an_explicit_state_file_path_wins_while_the_others_follow_runs_dir():
    s = build(runs_dir="x", portfolio_file="p.csv")
    assert s.portfolio_file == "p.csv"
    assert (s.out_file, s.cash_ledger_file) == ("x/next_portfolio.csv", "x/cash_ledger.csv")
    assert (s.trades_ledger_file, s.state_file) == ("x/trades_ledger.csv", "x/strategy_state.json")


def test_runs_dir_in_the_environment_moves_the_state_files_and_a_file_variable_beats_it(monkeypatch):
    for name in ("PORTFOLIO_FILE", "OUT_FILE", "CASH_LEDGER_FILE", "TRADES_LEDGER_FILE", "STATE_FILE", "RUNS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setenv("KITE_API_SECRET", "s")
    monkeypatch.setenv("RUNS_DIR", "/data/sotm")
    s = Settings.from_env()
    assert s.trades_ledger_file == "/data/sotm/trades_ledger.csv" and s.state_file == "/data/sotm/strategy_state.json"
    monkeypatch.setenv("TRADES_LEDGER_FILE", "/elsewhere/trades.csv")
    s = Settings.from_env()
    assert s.trades_ledger_file == "/elsewhere/trades.csv" and s.cash_ledger_file == "/data/sotm/cash_ledger.csv"


def test_starting_cash_defaults_to_account_value_but_explicit_zero_stays_zero():
    assert build().starting_cash == 100_000
    assert build(account_value=50_000).starting_cash == 50_000
    assert build(account_value=50_000, starting_cash=0).starting_cash == 0


def test_kite_min_interval_floors_the_rate():
    assert build(kite_rps=4).kite_min_interval == pytest.approx(0.25)
    assert build(kite_rps=0).kite_min_interval == pytest.approx(10.0)


def test_paths_expand_the_home_directory(tmp_path):
    s = build(kite_session_file="~/x/kite.json", cache_dir="~/y")
    assert s.kite_session_file == Path.home() / "x" / "kite.json"
    assert s.cache_dir == Path.home() / "y"


# ── one boolean parser, failing closed ───────────────────────────────────


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "n", "off", "f", " 0 "])
def test_falsy_strings_disable_execution(raw):
    assert build(allow_kite_execution=raw).allow_kite_execution is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "y", "on", "t", " 1 "])
def test_truthy_strings_enable_execution(raw):
    assert build(allow_kite_execution=raw).allow_kite_execution is True


def test_kill_switch_false_no_longer_crashes():
    assert build(kill_switch="false").kill_switch is False


@pytest.mark.parametrize("raw", ["maybe", "", "2", "yes please"])
def test_junk_boolean_refuses_to_start(raw):
    with pytest.raises(ValidationError, match="allow_kite_execution"):
        build(allow_kite_execution=raw)


# ── ranges where a wrong value is silently dangerous ─────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trading_weekday", 7),
        ("trading_weekday", -1),
        ("max_weight", 0),
        ("max_weight", 1.5),
        ("cut_off_pct", 0),
        ("cut_off_pct", 1.01),
        ("risk_factor", -0.001),
        ("kite_rps", -1),
        ("fees_pct", -0.1),
        ("slippage_pct", -0.1),
        ("max_positions", 0),
        ("atr_period", 0),
        ("kite_max_retries", 0),
        ("kite_redirect_port", 70_000),
        ("exit_multiple", 0),
        ("max_atr_pct", 0),
        ("starting_cash", -1),
    ],
)
def test_out_of_range_values_are_rejected(field, value):
    with pytest.raises(ValidationError, match=field):
        build(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [("trading_weekday", 0), ("trading_weekday", 6), ("max_weight", 1), ("cut_off_pct", 1), ("kite_rps", 0)],
)
def test_range_edges_are_accepted(field, value):
    assert getattr(build(**{field: value}), field) == value


# ── secrets ──────────────────────────────────────────────────────────────


def test_credentials_are_required_and_non_empty():
    with pytest.raises(ValidationError, match="kite_api_key"):
        Settings.from_values(kite_api_secret="s")
    with pytest.raises(ValidationError, match="kite_api_secret"):
        Settings.from_values(kite_api_key="k", kite_api_secret="")


def test_secrets_never_appear_in_repr_or_dumps():
    s = build(kite_api_secret="very-secret", kite_api_key="also-secret")
    for text in (repr(s), str(s), str(s.model_dump()), str(s.model_dump(mode="json"))):
        assert "very-secret" not in text
        assert "also-secret" not in text
    assert s.kite_api_secret.get_secret_value() == "very-secret"


# ── reading the environment ──────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("KITE_API_KEY", "env-key")
    monkeypatch.setenv("KITE_API_SECRET", "env-secret")
    return monkeypatch


def test_from_env_reads_and_parses(clean_env):
    clean_env.setenv("KILL_SWITCH", "false")
    clean_env.setenv("MAX_WEIGHT", "0.05")
    clean_env.setenv("cache_dir", "somewhere")  # names are case-insensitive, as before
    s = Settings.from_env()
    assert (s.kill_switch, s.max_weight, s.cache_dir) == (False, 0.05, Path("somewhere"))
    assert s.kite_api_key.get_secret_value() == "env-key"


def test_from_env_names_every_bad_variable_and_no_values(clean_env):
    clean_env.setenv("ALLOW_KITE_EXECUTION", "maybe")
    clean_env.setenv("TRADING_WEEKDAY", "7")
    clean_env.setenv("ACCOUNT_VALUE", "lots")
    with pytest.raises(SettingsError) as err:
        Settings.from_env()
    text = str(err.value)
    assert text.startswith("Configuration error in the environment:")
    assert "ALLOW_KITE_EXECUTION:" in text
    assert "TRADING_WEEKDAY:" in text
    assert "ACCOUNT_VALUE:" in text
    assert "STARTING_CASH" not in text  # derived from ACCOUNT_VALUE; not a separate problem
    for value in ("maybe", "lots", "env-secret"):
        assert value not in text


def test_from_values_ignores_the_environment(clean_env):
    clean_env.setenv("KILL_SWITCH", "1")
    assert build().kill_switch is False


# ── the command line ─────────────────────────────────────────────────────


def test_cli_example_prints_the_generator_output(capsys):
    assert st.main(["--example"]) == 0
    assert capsys.readouterr().out == st.render_example()


def test_cli_check_reports_good_and_bad_environments(clean_env, capsys):
    assert st.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "KITE_API_SECRET=**********" in out
    assert "TRADING_WEEKDAY=2" in out

    clean_env.setenv("TRADING_WEEKDAY", "9")
    assert st.main(["--check"]) == 2
    assert "TRADING_WEEKDAY:" in capsys.readouterr().err
