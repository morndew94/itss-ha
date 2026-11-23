"""ITSS-P400 CIRCADIA Google Calendar adapter for occupancy and pre-heat offsets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..const import KST


@dataclass(slots=True)
class CalendarThermalAdvice:
    """Comfort target temperature offset derived from Google Calendar events."""

    mode: str  # "AWAY", "PREHEAT", "HOME", "SLEEP", "DEFAULT"
    target_offset_c: float
    matched_event: str | None = None


class GoogleCalendarAdvisor:
    """Maps Google Calendar event titles/windows to P400 thermal comfort offsets."""

    AWAY_KEYWORDS = ("외출", "출장", "여행", "휴가", "away", "trip")
    PREHEAT_KEYWORDS = ("귀가", "방문", "손님", "preheat", "home")
    SLEEP_KEYWORDS = ("취침", "수면", "sleep")

    def __init__(
        self,
        *,
        away_offset_c: float = -2.0,
        preheat_offset_c: float = 0.2,
        sleep_offset_c: float = -0.3,
        preheat_lead_min: int = 90,
    ) -> None:
        self.away_offset_c = away_offset_c
        self.preheat_offset_c = preheat_offset_c
        self.sleep_offset_c = sleep_offset_c
        self.preheat_lead_min = preheat_lead_min

    def evaluate_events(
        self,
        events: list[dict[str, Any]],
        *,
        now_kst: datetime | None = None,
    ) -> CalendarThermalAdvice:
        """Evaluate Google Calendar API 'items' list for the current KST timestamp."""
        now = now_kst or datetime.now(tz=KST)

        for ev in events:
            summary = str(ev.get("summary", "")).strip()
            low = summary.lower()
            start_dt = self._parse_event_dt(ev.get("start", {}))
            end_dt = self._parse_event_dt(ev.get("end", {}))
            if start_dt is None or end_dt is None:
                continue

            # Pre-heat window before a scheduled return/guest event
            mins_to_start = (start_dt - now).total_seconds() / 60.0
            if 0 < mins_to_start <= self.preheat_lead_min and any(k in low for k in self.PREHEAT_KEYWORDS):
                return CalendarThermalAdvice("PREHEAT", self.preheat_offset_c, summary)

            # Active event window
            if start_dt <= now <= end_dt:
                if any(k in low for k in self.AWAY_KEYWORDS):
                    return CalendarThermalAdvice("AWAY", self.away_offset_c, summary)
                if any(k in low for k in self.SLEEP_KEYWORDS):
                    return CalendarThermalAdvice("SLEEP", self.sleep_offset_c, summary)
                if any(k in low for k in self.PREHEAT_KEYWORDS):
                    return CalendarThermalAdvice("HOME", 0.0, summary)

        return CalendarThermalAdvice("DEFAULT", 0.0, None)

    @staticmethod
    def _parse_event_dt(node: dict[str, Any]) -> datetime | None:
        raw = node.get("dateTime")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt.astimezone(KST) if dt.tzinfo else dt.replace(tzinfo=KST)
        except ValueError:
            return None
