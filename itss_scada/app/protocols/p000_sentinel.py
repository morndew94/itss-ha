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
        v_floor_per_min: float = 0.0,
        seconds_since_last_switch: float | None = None,
        continuous_run_min: float | None = None,
        max_run_min: float | None = None,
        poll_interval_min: float = 1.0,
        cooldown_remaining_min: float | None = None,
        window_open: bool = False,
        drop_5m_room_c: float | None = None,
        drop_5m_floor_c: float | None = None,
        ventilation_remaining_min: float | None = None,
        effective_floor_limit_c: float | None = None,
        expected_floor_surge_c: float = 0.0,
        trend_10m_room_c: float | None = None,
        trend_10m_floor_c: float | None = None,
        room_stale_after_sec: float | None = None,
        floor_stale_after_sec: float | None = None,
        floor_latency_c: float = 0.0,
    ) -> SafetyVerdict:
        """Run prioritized safety checks.

        Priority order:
          1. Manual override priority
          2. Sensor validity and staleness
          3. Hard overheat cutoff (room >= 27C or floor >= 29C)
          4. General & dynamic/predicted floor upper limit
          5. Maximum single-run pulse duration cap (step-aware, <= 15 min) & residual cooldown
          6. Active floor residual soak / room rebound guard (blocks premature 2nd run)
          7. Ventilation rapid-drop & window-open protection
          8. Anti-chattering (minimum ON/OFF dwell time)

        v2.7.0:
          - room/floor_stale_after_sec: 센서별 관측 보고 주기 기반 적응형 stale 임계 (None이면 기본값)
          - floor_latency_c: 바닥 센서 보고 지연 동안의 추정 상승분. 바닥 상한/예측 판정에 가산
        """
        room_stale_limit = room_stale_after_sec if room_stale_after_sec is not None else self.config.max_sensor_age_sec
        floor_stale_limit = (
            floor_stale_after_sec if floor_stale_after_sec is not None else self.config.max_sensor_age_sec
        )
        latency = max(0.0, float(floor_latency_c or 0.0))
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
            and snapshot.room_sensor_age_sec > room_stale_limit
        ) or (
            snapshot.floor_sensor_age_sec is not None
            and snapshot.floor_sensor_age_sec > floor_stale_limit
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

        # 4a. Hard configured floor OFF upper limit (and dynamic target-linked limit while heating)
        floor_cap = (
            min(self.config.general_off_floor_c, effective_floor_limit_c)
            if effective_floor_limit_c is not None
            else self.config.general_off_floor_c
        )
        active_cap = floor_cap if is_heating else self.config.general_off_floor_c
        # 보고 지연 보상: 가열 중에는 마지막 보고 이후 상승분을 더해 보수적으로 판정
        floor_now_est = snapshot.floor_temp + (latency if is_heating else 0.0)
        if floor_now_est >= active_cap:
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_FLOOR_LIMIT_OFF",
                message=(
                    f"Floor temperature {snapshot.floor_temp:.1f}C (+latency {latency if is_heating else 0.0:.2f}C) "
                    f">= {active_cap:.1f}C limit."
                ),
            )

        # 4b. Predicted post-OFF floor peak guard while actively heating (prevents slab surge past floor_cap)
        if effective_floor_limit_c is not None and is_heating:
            projected_floor = max(
                floor_now_est + max(0.0, expected_floor_surge_c),
                floor_now_est + max(0.0, v_floor_per_min) * 10.0,
            )
            if projected_floor >= floor_cap:
                return SafetyVerdict(
                    allow_heating=False,
                    force_off=True,
                    hold_current_state=False,
                    reason_code="P000_FLOOR_PREDICTED_LIMIT",
                    message=(
                        f"Projected floor peak {projected_floor:.1f}C >= {floor_cap:.1f}C "
                        f"(now={snapshot.floor_temp:.1f}C, surge=+{expected_floor_surge_c:.1f}C)."
                    ),
                )

        # 5a. Max single-run heating pulse cap (step-aware so 5-min polling never overruns cap)
        if (
            is_heating
            and continuous_run_min is not None
            and max_run_min is not None
        ):
            lookahead_min = max(0.0, (poll_interval_min - 1.0) * 0.5)
            if continuous_run_min + lookahead_min >= max_run_min:
                return SafetyVerdict(
                    allow_heating=False,
                    force_off=True,
                    hold_current_state=False,
                    reason_code="P000_MAX_RUN_CUTOFF",
                    message=(
                        f"Single run reached {continuous_run_min:.1f}m (cap {max_run_min:.1f}m); "
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

        # 5c. Pre-start floor headroom guard (blocks starting a new pulse when floor is already near cap)
        if effective_floor_limit_c is not None and not is_heating and snapshot.floor_temp >= floor_cap - 0.5:
            return SafetyVerdict(
                allow_heating=False,
                force_off=True,
                hold_current_state=False,
                reason_code="P000_FLOOR_PREDICTED_LIMIT",
                message=(
                    f"Floor {snapshot.floor_temp:.1f}C already within 0.5C of {floor_cap:.1f}C limit; "
                    "blocking new heating pulse."
                ),
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

        # 6b. Ventilation rapid drop detection:
        #     - 1-min rapid slope (<= -0.15 C/min)
        #     - 5-min air-only drop (<= -0.5 C room while floor > -0.2 C)
        #     - severe 5-min drop (<= -0.8 C room regardless of simultaneous post-peak floor cooling)
        air_only_5m_drop = (
            drop_5m_room_c is not None
            and drop_5m_room_c <= -0.5
            and (drop_5m_floor_c is None or drop_5m_floor_c > -0.2)
        )
        severe_5m_drop = drop_5m_room_c is not None and drop_5m_room_c <= -0.8
        if (
            v_room_per_min <= self.config.ventilation_drop_rate_per_min
            or air_only_5m_drop
            or severe_5m_drop
        ):
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

        # 6d. Active floor residual release / room rebound guard (blocks double-run stacking)
        if not is_heating:
            floor_warmer_than_room = snapshot.floor_temp - snapshot.room_temp
            floor_still_rising = (
                trend_10m_floor_c is not None
                and trend_10m_floor_c >= 0.2
                and floor_warmer_than_room >= 0.5
            )
            room_actively_rebounding = (
                trend_10m_room_c is not None
                and trend_10m_room_c >= 0.2
                and floor_warmer_than_room >= 0.8
            )
            if floor_still_rising or room_actively_rebounding:
                return SafetyVerdict(
                    allow_heating=False,
                    force_off=True,
                    hold_current_state=False,
                    reason_code="P000_FLOOR_RESIDUAL_ACTIVE",
                    message=(
                        f"Warm floor ({snapshot.floor_temp:.1f}C vs room {snapshot.room_temp:.1f}C) "
                        f"still releasing residual heat (10m floor={trend_10m_floor_c}, room={trend_10m_room_c}); "
                        "waiting before next pulse."
                    ),
                )

        # 7. Anti-chattering minimum dwell time
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

