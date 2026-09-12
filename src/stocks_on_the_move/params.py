"""The strategy's parameters as one frozen value (ADR-021).

Two kinds of number meet here. The constants the code fixes: the momentum
lookbacks and their weights, the regression window, the two moving-average
periods, trading days in a year, the share floor. And the knobs the operator
sets through ``Settings`` (ADR-007): the ATR period, the risk factor, the
weight cap, the ranking cut-off, the stop multiple, the volume and ATR
filters. A rule is a function of a snapshot and one of these, so the live run
builds one from its settings and a backtest builds variants without touching
the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stocks_on_the_move.settings import Settings


def _setting_default(name: str) -> Any:
    """The default ``Settings`` gives a knob, so the two never drift apart."""
    return Settings.model_fields[name].default


@dataclass(frozen=True)
class StrategyParams:
    # ── fixed by the strategy; the values momentum.py carried (ADR-016) ──
    # Shortened from the book's 21/63/126 before version control, for a reason
    # nobody recorded; changing them is a strategy ADR with backtest evidence.
    lookback_short: int = 5
    lookback_mid: int = 15
    lookback_long: int = 45
    weight_short: float = 0.6
    weight_mid: float = 0.3
    weight_long: float = 0.1
    reg_lookback: int = 90
    trend_ma_period: int = 100  # a stock must close above this simple moving average to be ranked (ADR-024)
    regime_ma_period: int = 200  # the index must close above this simple moving average for buys
    trading_days_yr: int = 250
    min_shares: int = 1

    # ── the operator's knobs, from Settings (ADR-007) ──
    atr_period: int = _setting_default("atr_period")
    risk_factor: float = _setting_default("risk_factor")
    max_weight: float = _setting_default("max_weight")
    cut_off_pct: float = _setting_default("cut_off_pct")
    exit_multiple: float = _setting_default("exit_multiple")
    min_volume: int = _setting_default("min_volume")
    max_atr_pct: float = _setting_default("max_atr_pct")

    @classmethod
    def from_settings(cls, settings: Settings) -> StrategyParams:
        """The live run's parameters: the code's constants and the operator's knobs."""
        return cls(
            atr_period=settings.atr_period,
            risk_factor=settings.risk_factor,
            max_weight=settings.max_weight,
            cut_off_pct=settings.cut_off_pct,
            exit_multiple=settings.exit_multiple,
            min_volume=settings.min_volume,
            max_atr_pct=settings.max_atr_pct,
        )

    @property
    def history_days(self) -> int:
        """Candles a stock needs before any rule looks at it, and the window one snapshot is built from."""
        return max(self.trend_ma_period, self.lookback_long + 1, self.reg_lookback + 1)

    @property
    def score_history(self) -> int:
        """Closes the composite score needs."""
        return max(self.lookback_long, self.reg_lookback) + 1

    @property
    def stop_window(self) -> int:
        """Closes the trailing stop's rolling high looks back over."""
        return max(self.atr_period, 2 * self.atr_period)
