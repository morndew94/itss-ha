"""ITSS-P300 ATMOS: Outdoor weather and time-of-day heat loss compensation."""

from __future__ import annotations

from datetime import datetime

from ..const import KST


class AtmosProtocol:
    """Computes target/pre-heat offset from weather or time-of-day fallback."""

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
            # Cold-snap pre-heat compensation below 0 degC
            base = max(0.0, (0.0 - outdoor_temp) * 0.015)
            wind_extra = 0.05 if (wind_speed_ms is not None and wind_speed_ms >= 6.0) else 0.0
            return round(min(0.25, base + wind_extra), 2)

        # Fallback: time-of-day heat loss profile (KST)
        dt = now_kst or datetime.now(tz=KST)
        hour = dt.hour
        if 0 <= hour < 6:
            return 0.05   # Pre-dawn cooling compensation
        if 11 <= hour < 16:
            return -0.05  # Daytime solar heat gain compensation
        return 0.0

