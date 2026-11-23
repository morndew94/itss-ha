"""ITSS-P100 CORE: Basic control, 0.5 degC rounding, and SCADA audit helpers."""

from __future__ import annotations

import math

from ..const import THERMOSTAT_SETPOINT_STEP


def round_thermostat_setpoint(
    target_temp: float,
    *,
    action: str,
    step: float = THERMOSTAT_SETPOINT_STEP,
) -> float:
    """Round a 0.1 degC precision target to a 0.5 degC thermostat setpoint.

    When commanding HEAT, round up to ensure the thermostat actively engages.
    When commanding OFF, round down to ensure the thermostat disengages.
    """
    if action == "HEAT":
        return math.ceil(target_temp / step) * step
    if action == "OFF":
        return math.floor(target_temp / step) * step
    return round(target_temp / step) * step


def compute_opposing_interval(
    last_action: str | None,
    new_action: str,
    seconds_since_last_command: float | None,
) -> float | None:
    """Return elapsed seconds when transitioning between opposing actions (HEAT <-> OFF)."""
    if seconds_since_last_command is None or last_action is None:
        return None
    if {last_action, new_action} == {"HEAT", "OFF"}:
        return round(seconds_since_last_command, 1)
    return None

