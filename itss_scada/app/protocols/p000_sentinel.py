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
        continuous_run_min: float | None = None,
        max_run_min: float | None = None,
        cooldown_remaining_min: float | None = None,
        window_open: bool = False,
        drop_5m_room_c: float | None = None,
        drop_5m_floor_c: float | None = None,
        ventilation_remaining_min: float | None = None,
    ) -> SafetyVerdict:
        """Run prioritized safety checks.

        Priority order:
          1. Manual override priority
          2. Sensor validity and staleness
          3. Hard overheat cutoff (room >= 27C or floor >= 29C)
          4. General floor upper limit (configurable, e.g. floor >= 26.5C)
          5. Maximum single-run pulse duration cap (<= 15 min) & residual-heat cooldown
          6. Ventilation rapid-drop protection
          7. Anti-chattering (minimum ON/OFF dwell time)
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

        # 5a. Max single-run heating pulse cap (outdoor-adaptive, hard-capped <= 15 min)
        if (
            is_heating
            and continuous_run_min is not None
            and max_run_min is not None
            and continuous_run_min >= max_run_min
        ):
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_MAX_RUN_CUTOFF",
                message=(
                    f"Single run reached {continuous_run_min:.1f}m >= {max_run_min:.1f}m cap; "
                    "cutting off for residual heat soak."
                ),
            )

        # 5b. Post-cutoff residual heat soak cooldown
        if cooldown_remaining_min is not None and cooldown_remaining_min > 0:
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_RESIDUAL_COOLDOWN",
                message=f"Residual heat soak cooldown active ({cooldown_remaining_min:.1f}m remaining).",
            )

        # 6a. Physical window contact sensor open (0-second immediate cutoff)
        if window_open:
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_WINDOW_OPEN",
                message="Window open sensor active; pausing heating immediately.",
            )

        # 6b. Ventilation rapid drop detection (1-min slope OR 5-min air-only drop cross-checked with floor)
        air_only_5m_drop = (
            drop_5m_room_c is not None
            and drop_5m_room_c <= -0.5
            and (drop_5m_floor_c is None or drop_5m_floor_c > -0.2)
        )
        if v_room_per_min <= self.config.ventilation_drop_rate_per_min or air_only_5m_drop:
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_VENTILATION_PROTECT",
                message=(
                    f"Ventilation drop detected (1m={v_room_per_min:.3f} C/min, "
                    f"5m={drop_5m_room_c}); latching ventilation hold."
                ),
            )

        # 6c. Active ventilation hold / post-window-close natural recovery wait
        if ventilation_remaining_min is not None and ventilation_remaining_min > 0:
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_VENTILATION_HOLD",
                message=f"Ventilation recovery hold active ({ventilation_remaining_min:.1f}m remaining).",
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

