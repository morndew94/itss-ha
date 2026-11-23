"""ITSS-P400 CIRCADIA: Lifestyle and thermal comfort offset protocol."""

from __future__ import annotations

from datetime import datetime

from ..const import KST


class CircadiaProtocol:
    """Adjusts effective target temperature for sleep, wake, and active periods."""

    def __init__(
        self,
        *,
        sleep_offset_c: float = -0.3,
        wake_preheat_offset_c: float = 0.1,
    ) -> None:
        self.sleep_offset_c = sleep_offset_c
        self.wake_preheat_offset_c = wake_preheat_offset_c

    def compute_offset(self, now_kst: datetime | None = None) -> float:
        """Return comfort target temperature offset in degC."""
        dt = now_kst or datetime.now(tz=KST)
        hour = dt.hour
        if 23 <= hour or hour < 5:
            return self.sleep_offset_c
        if 5 <= hour < 7:
            return self.wake_preheat_offset_c
        return 0.0

