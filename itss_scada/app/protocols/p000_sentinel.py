"""ITSS-P000 SENTINEL: Safety management and interlock protocol."""

from __future__ import annotations

from dataclasses import dataclass

from ..const import (
    DEFAULT_GENERAL_OFF_FLOOR_C,
    DEFAULT_MAX_SENSOR_AGE_SEC,
    DEFAULT_MIN_OFF_SEC,
    DEFAULT_MIN_ON_SEC,
    DEFAULT_OVERHEAT_FLOOR_C,
    DEFAULT_OVERHEAT_ROOM_C,
    DEFAULT_VENTILATION_DROP_RATE,
)
from ..models.schema import SafetyVerdict, SensorSnapshot


@dataclass(slots=True)
class SentinelConfig:
    """Configurable safety thresholds for ITSS-P000 SENTINEL."""

    overheat_room_c: float = DEFAULT_OVERHEAT_ROOM_C
    overheat_floor_c: float = DEFAULT_OVERHEAT_FLOOR_C
    general_off_floor_c: float = DEFAULT_GENERAL_OFF_FLOOR_C
    ventilation_drop_rate_per_min: float = DEFAULT_VENTILATION_DROP_RATE
    min_on_sec: int = DEFAULT_MIN_ON_SEC
    min_off_sec: int = DEFAULT_MIN_OFF_SEC
    max_sensor_age_sec: int = DEFAULT_MAX_SENSOR_AGE_SEC


class SentinelProtocol:
    """Evaluates hard safety interlocks before any P200 prediction is applied."""

    def __init__(self, config: SentinelConfig | None = None) -> None:
        self.config = config or SentinelConfig()

    def evaluate(
        self,
        snapshot: SensorSnapshot,
        *,
        is_heating: bool,
        v_room_per_min: float = 0.0,
        seconds_since_last_switch: float | None = None,
    ) -> SafetyVerdict:
        """Run prioritized safety checks.

        Priority order:
          1. Manual override priority
          2. Sensor validity and staleness
          3. Hard overheat cutoff (room >= 27C or floor >= 29C)
          4. General floor upper limit (floor >= 28C)
          5. Ventilation rapid-drop protection
          6. Anti-chattering (minimum ON/OFF dwell time)
        """
        # 1. Respect manual user intervention above automated control
        if snapshot.manual_override_active:
            return SafetyVerdict(
                allow_heating=False,
                force_off=False,
                hold_current_state=True,
                reason_code="P000_MANUAL_OVERRIDE",
                message="Manual override active; automated control paused.",
            )

        # 2. Sensor reliability check
        if snapshot.room_temp is None or snapshot.floor_temp is None:
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_SENSOR_MISSING",
                message="Room or floor temperature sensor unavailable; forcing safe OFF.",
            )

        if (
            snapshot.room_sensor_age_sec is not None
            and snapshot.room_sensor_age_sec > self.config.max_sensor_age_sec
        ) or (
            snapshot.floor_sensor_age_sec is not None
            and snapshot.floor_sensor_age_sec > self.config.max_sensor_age_sec
        ):
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_SENSOR_STALE",
                message="Sensor reading exceeded maximum age; forcing safe OFF.",
            )

        # 3. Hard overheat cutoff
        if (
            snapshot.room_temp >= self.config.overheat_room_c
            or snapshot.floor_temp >= self.config.overheat_floor_c
        ):
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_OVERHEAT_CUTOFF",
                message=(
                    f"Overheat cutoff triggered (room={snapshot.room_temp:.1f}C, "
                    f"floor={snapshot.floor_temp:.1f}C)."
                ),
            )

        # 4. General floor OFF upper limit
        if snapshot.floor_temp >= self.config.general_off_floor_c:
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_FLOOR_LIMIT_OFF",
                message=f"Floor temperature reached {snapshot.floor_temp:.1f}C general OFF limit.",
            )

        # 5. Ventilation rapid temperature drop protection
        if v_room_per_min <= self.config.ventilation_drop_rate_per_min:
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_VENTILATION_PROTECT",
                message=f"Rapid indoor drop ({v_room_per_min:.3f} C/min) detected; pausing heat.",
            )

        # 6. Anti-chattering minimum dwell time
        if seconds_since_last_switch is not None:
            min_dwell = self.config.min_on_sec if is_heating else self.config.min_off_sec
            if seconds_since_last_switch < min_dwell:
                return SafetyVerdict(
                    allow_heating=is_heating,
                    force_off=False,
                    hold_current_state=True,
                    reason_code="P000_ANTI_CHATTER",
                    message=(
                        f"Dwell time {seconds_since_last_switch:.0f}s < {min_dwell}s; "
                        "holding current state."
                    ),
                )

        return SafetyVerdict(
            allow_heating=True,
            force_off=False,
            hold_current_state=False,
            reason_code="P000_OK",
            message="All safety checks passed.",
        )

