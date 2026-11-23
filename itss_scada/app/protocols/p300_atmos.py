"""ITSS-P300 ATMOS: Outdoor weather heat-loss offset and adaptive single-run cap."""

from __future__ import annotations

from datetime import datetime

from ..const import KST


class AtmosProtocol:
    """Computes thermal loss offset and outdoor-adaptive max single heating duration."""

    def compute_offset(
        self,
        *,
        outdoor_temp: float | None = None,
        wind_speed_ms: float | None = None,
        now_kst: datetime | None = None,
    ) -> float:
        """Return thermal loss compensation offset in degC.

        When outdoor weather telemetry is unavailable, falls back to a
        time-of-day heat loss profile (late-night cooling vs daytime solar gain).
        """
        if outdoor_temp is not None:
            base = max(0.0, (0.0 - outdoor_temp) * 0.015)
            wind_extra = 0.05 if (wind_speed_ms is not None and wind_speed_ms >= 6.0) else 0.0
            return round(min(0.25, base + wind_extra), 2)

        dt = now_kst or datetime.now(tz=KST)
        hour = dt.hour
        if 0 <= hour < 6:
            return 0.05
        if 11 <= hour < 16:
            return -0.05
        return 0.0

    def compute_max_run_minutes(
        self,
        *,
        outdoor_temp: float | None = None,
        now_kst: datetime | None = None,
        hard_cap_min: float = 15.0,
    ) -> float:
        """Compute maximum single-cycle heating duration (minutes) by outdoor temperature.

        Always bounded by hard_cap_min (default 15.0 min) to prevent Ondol floor
        residual-heat overshoot.
        """
        cap = max(3.0, min(15.0, float(hard_cap_min)))
        if outdoor_temp is not None:
            if outdoor_temp <= -5.0:
                planned = 15.0
            elif outdoor_temp <= 0.0:
                planned = 13.0
            elif outdoor_temp <= 5.0:
                planned = 11.0
            elif outdoor_temp <= 10.0:
                planned = 9.0
            else:
                planned = 7.0
            return min(cap, planned)

        # Fallback when outdoor temperature is unavailable: time-of-day schedule
        dt = now_kst or datetime.now(tz=KST)
        hour = dt.hour
        if 0 <= hour < 6:
            planned = 12.0
        elif 11 <= hour < 16:
            planned = 8.0
        else:
            planned = 10.0
        return min(cap, planned)
