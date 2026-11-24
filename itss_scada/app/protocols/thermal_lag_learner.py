"""Thermal lag and residual-heat dynamics learner for ITSS-P200.

Learns real per-room Ondol dead-times and post-shutoff residual peaks from
historical KST cycle records:
  - floor_on_deadtime_min   : delay from boiler ON until floor rises >= +0.1C
  - room_on_deadtime_min    : delay from boiler ON until room rises >= +0.1C
  - floor_post_off_peak_min : delay after boiler OFF until floor peaks
  - floor_residual_rise_c   : floor temperature rise after boiler OFF
  - room_post_off_peak_min  : delay after boiler OFF until room air peaks
  - room_residual_rise_c    : room air temperature rise after boiler OFF
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import median
from typing import Any

# Conservative Korean Ondol defaults used before enough episodes are observed
DEFAULT_FLOOR_ON_DEADTIME_MIN = 5.0
DEFAULT_ROOM_ON_DEADTIME_MIN = 15.0
DEFAULT_FLOOR_POST_OFF_PEAK_MIN = 10.0
DEFAULT_ROOM_POST_OFF_PEAK_MIN = 20.0
DEFAULT_FLOOR_RESIDUAL_RISE_C = 1.2
DEFAULT_ROOM_RESIDUAL_RISE_C = 0.2


@dataclass(slots=True)
class RoomLagProfile:
    """Learned thermal delay and residual-heat parameters for one room."""

    room_name: str
    status: str  # "INSUFFICIENT_SAMPLES", "LEARNING", "READY"
    sample_count: int
    floor_on_deadtime_min: float
    room_on_deadtime_min: float
    floor_post_off_peak_min: float
    floor_residual_rise_c: float
    room_post_off_peak_min: float
    room_residual_rise_c: float
    recommended_cooldown_min: int
    recommended_horizon_min: int
    recommended_run_trim_min: float

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        return {
            "roomName": raw["room_name"],
            "status": raw["status"],
            "sampleCount": raw["sample_count"],
            "floorOnDeadtimeMin": raw["floor_on_deadtime_min"],
            "roomOnDeadtimeMin": raw["room_on_deadtime_min"],
            "floorPostOffPeakMin": raw["floor_post_off_peak_min"],
            "floorResidualRiseC": raw["floor_residual_rise_c"],
            "roomPostOffPeakMin": raw["room_post_off_peak_min"],
            "roomResidualRiseC": raw["room_residual_rise_c"],
            "recommendedCooldownMin": raw["recommended_cooldown_min"],
            "recommendedHorizonMin": raw["recommended_horizon_min"],
            "recommendedRunTrimMin": raw["recommended_run_trim_min"],
        }


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _is_heating_cycle(c: dict[str, Any]) -> bool:
    if c.get("combustionConfirmed") is True:
        return True
    if c.get("combustionConfirmed") is False:
        return False
    return c.get("commandAction") == "HEAT"


def learn_room_lag_profile(
    room_name: str,
    cycles_desc_or_asc: list[dict[str, Any]],
    *,
    base_cooldown_min: int = 20,
) -> RoomLagProfile:
    """Extract ON dead-times and post-OFF residual peaks from one room's cycles."""
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for c in cycles_desc_or_asc:
        if c.get("roomName") != room_name:
            continue
        ts = _parse_ts(c.get("executionStartedAt"))
        if ts is None or c.get("roomTemp") is None:
            continue
        rows.append((ts, c))

    rows.sort(key=lambda pair: pair[0])

    floor_on_lags: list[float] = []
    room_on_lags: list[float] = []
    floor_off_peaks: list[float] = []
    floor_off_rises: list[float] = []
    room_off_peaks: list[float] = []
    room_off_rises: list[float] = []

    n = len(rows)
    i = 0
    while i < n:
        t_start, c_start = rows[i]
        if not _is_heating_cycle(c_start):
            i += 1
            continue

        # Ensure this is the beginning of a heating episode (preceded by OFF or start)
        if i > 0 and _is_heating_cycle(rows[i - 1][1]):
            i += 1
            continue

        start_room = float(c_start["roomTemp"])
        start_floor = float(c_start["floorTemp"]) if c_start.get("floorTemp") is not None else None

        # Walk forward until heating stops
        j = i + 1
        found_floor_rise = False
        found_room_rise = False
        while j < n and _is_heating_cycle(rows[j][1]):
            tj, cj = rows[j]
            elapsed = (tj - t_start).total_seconds() / 60.0
            if elapsed > 60.0:
                break
            if not found_floor_rise and start_floor is not None and cj.get("floorTemp") is not None:
                if float(cj["floorTemp"]) >= start_floor + 0.1:
                    floor_on_lags.append(round(elapsed, 1))
                    found_floor_rise = True
            if not found_room_rise and float(cj["roomTemp"]) >= start_room + 0.1:
                room_on_lags.append(round(elapsed, 1))
                found_room_rise = True
            j += 1

        if j >= n:
            break

        # j is the first OFF sample after heating stopped
        t_off, c_off = rows[j]
        off_room = float(c_off["roomTemp"])
        off_floor = float(c_off["floorTemp"]) if c_off.get("floorTemp") is not None else None

        # Observe post-OFF residual soak window (up to 60 min while remaining OFF)
        best_room = off_room
        best_room_min = 0.0
        best_floor = off_floor
        best_floor_min = 0.0
        soak_duration = 0.0
        ventilation_contaminated = str(c_off.get("safetyReason") or "").startswith(
            ("P000_VENTILATION", "P000_WINDOW_OPEN")
        )

        k = j + 1
        while k < n and not _is_heating_cycle(rows[k][1]):
            tk, ck = rows[k]
            after_off = (tk - t_off).total_seconds() / 60.0
            if after_off > 60.0:
                break
            if str(ck.get("safetyReason") or "").startswith(("P000_VENTILATION", "P000_WINDOW_OPEN")):
                ventilation_contaminated = True
            soak_duration = after_off

            # Continue detecting delayed room rise even after boiler turned OFF
            if not found_room_rise and float(ck["roomTemp"]) >= start_room + 0.1:
                room_on_lags.append(round((tk - t_start).total_seconds() / 60.0, 1))
                found_room_rise = True

            rk = float(ck["roomTemp"])
            if rk > best_room:
                best_room = rk
                best_room_min = after_off

            if off_floor is not None and ck.get("floorTemp") is not None:
                fk = float(ck["floorTemp"])
                if best_floor is None or fk > best_floor:
                    best_floor = fk
                    best_floor_min = after_off
            k += 1

        # Record post-OFF residual episode only when soak >= 5 min and not contaminated by ventilation
        if soak_duration >= 5.0 and not ventilation_contaminated:
            room_rise = max(0.0, round(best_room - off_room, 2))
            if room_rise <= 2.5:  # Ignore abnormal sensor jumps
                room_off_rises.append(room_rise)
                if room_rise >= 0.1:
                    room_off_peaks.append(round(best_room_min, 1))
            if off_floor is not None and best_floor is not None:
                floor_rise = max(0.0, round(best_floor - off_floor, 2))
                if floor_rise <= 4.0:
                    floor_off_rises.append(floor_rise)
                    if floor_rise >= 0.1:
                        floor_off_peaks.append(round(best_floor_min, 1))

        i = k

    sample_count = len(room_off_rises)
    if sample_count < 2:
        status = "INSUFFICIENT_SAMPLES"
    elif sample_count < 5:
        status = "LEARNING"
    else:
        status = "READY"

    f_on = round(float(median(floor_on_lags)), 1) if floor_on_lags else DEFAULT_FLOOR_ON_DEADTIME_MIN
    r_on = round(float(median(room_on_lags)), 1) if room_on_lags else DEFAULT_ROOM_ON_DEADTIME_MIN
    f_peak = round(float(median(floor_off_peaks)), 1) if floor_off_peaks else DEFAULT_FLOOR_POST_OFF_PEAK_MIN
    f_rise = round(float(median(floor_off_rises)), 2) if floor_off_rises else DEFAULT_FLOOR_RESIDUAL_RISE_C
    r_peak = round(float(median(room_off_peaks)), 1) if room_off_peaks else DEFAULT_ROOM_POST_OFF_PEAK_MIN
    r_rise = round(float(median(room_off_rises)), 2) if room_off_rises else DEFAULT_ROOM_RESIDUAL_RISE_C

    # Apply learned lag directly to cooldown, P200 prediction horizon, and pulse trimming
    # Cooldown must cover both room peak and floor peak + 10 min dissipation time
    rec_cooldown = int(max(10, min(45, max(base_cooldown_min, round(r_peak), round(f_peak + 10.0)))))
    rec_horizon = int(max(15, min(45, round(max(r_peak, r_on)))))
    rec_trim = 2.0 if (status != "INSUFFICIENT_SAMPLES" and (r_rise >= 0.4 or f_rise >= 2.5)) else (
        1.0 if (status != "INSUFFICIENT_SAMPLES" and (r_rise >= 0.25 or f_rise >= 1.8)) else 0.0
    )

    return RoomLagProfile(
        room_name=room_name,
        status=status,
        sample_count=sample_count,
        floor_on_deadtime_min=f_on,
        room_on_deadtime_min=r_on,
        floor_post_off_peak_min=f_peak,
        floor_residual_rise_c=f_rise,
        room_post_off_peak_min=r_peak,
        room_residual_rise_c=r_rise,
        recommended_cooldown_min=rec_cooldown,
        recommended_horizon_min=rec_horizon,
        recommended_run_trim_min=rec_trim,
    )

