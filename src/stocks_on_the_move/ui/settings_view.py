"""The settings page: what a run would see (ADR-007), credentials removed (ADR-006), changed values marked."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stocks_on_the_move.artifacts import settings_snapshot
from stocks_on_the_move.settings import Settings


@dataclass(frozen=True)
class SettingRow:
    name: str  # the environment variable
    value: str
    default: str
    changed: bool
    description: str


def _default_of(name: str, settings: Settings) -> Any:
    field = Settings.model_fields[name]
    if field.is_required():
        return None
    # A derived default (the five state files from RUNS_DIR, ADR-029; STARTING_CASH from ACCOUNT_VALUE) is a
    # factory over the validated data, so it gets this settings object's values.
    return field.get_default(call_default_factory=True, validated_data=settings.model_dump())


def _norm(value: Any) -> str:
    """One comparable text for a value however it was typed: paths expanded, ints and floats alike."""
    if value is None:
        return ""
    if isinstance(value, Path):
        value = str(value.expanduser())
    if isinstance(value, int | float) and not isinstance(value, bool):
        value = float(value)
    return json.dumps(value, default=str, sort_keys=True)


def _show(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def settings_rows(settings: Settings) -> list[SettingRow]:
    """One row per setting in the redacted snapshot, in the snapshot's order."""
    rows: list[SettingRow] = []
    for env_name, value in settings_snapshot(settings).items():
        name = env_name.lower()
        default = _default_of(name, settings)
        rows.append(
            SettingRow(
                name=env_name,
                value=_show(value),
                default=_show(default),
                changed=_norm(value) != _norm(default),
                description=Settings.model_fields[name].description or "",
            )
        )
    return rows
