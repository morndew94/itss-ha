"""v2.9.1 쾌적 학습: 월패드/ITSS 수동 ON 조작을 '추웠다' 신호로 모아 방별 목표·바닥 최저 온도를 제안한다.

원칙
- 제안만 한다. 제어 설정에 자동 반영하지 않는다 (사용자가 UI 에서 승인 후 적용).
- 실내 온도는 이벤트 당시 센서 원시값 + '현재' 보정값으로 다시 계산한다
  (나중에 센서 보정값을 바꿔도 과거 신호가 같은 기준으로 재해석됨).
- 제안 목표는 절대값(그때 추웠던 온도 기준)으로 계산하므로, 적용 후 같은 이벤트로 중복 상향되지 않는다.
- 신호는 '추웠다' 한 방향뿐이다. 목표를 낮추는 제안은 하지 않는다 (덥다는 신호가 없음).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from .const import (
    COMFORT_AT_TARGET_DEV_C,
    COMFORT_FLOOR_COLD_C,
    COMFORT_FLOOR_MIN_LOW_C,
    COMFORT_KEEP_DAYS,
    COMFORT_MAX_EVENTS,
    COMFORT_MIN_EVENTS,
    COMFORT_TARGET_MARGIN_C,
    COMFORT_TARGET_MAX_STEP_C,
    COMFORT_WINDOW_DAYS,
    DEFAULT_OVERHEAT_ROOM_C,
    FLOOR_MIN_HYST_C,
    FLOOR_MIN_MAX_C,
)

SOURCE_LABEL = {"WALLPAD": "월패드·외부 자동화", "ITSS_MANUAL": "ITSS 수동 ON"}
_FLOOR_SRCS = ("MEASURED", "ESTIMATED")


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(float(v), nd)


def _parse(ts: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def make_event(
    *,
    now: datetime,
    room: str,
    source: str,
    room_temp_raw: float | None,
    room_offset_c: float,
    target_temp: float,
    eff_target: float,
    floor_temp: float | None,
    floor_src: str | None,
    outdoor_temp: float | None,
) -> dict[str, Any]:
    """조작 1건 기록 (당시 원시 실내값/보정값/목표/바닥/외기)."""
    room_temp = None if room_temp_raw is None else room_temp_raw + room_offset_c
    return {
        "at": now.isoformat(timespec="seconds"),
        "room": room,
        "source": source,
        "roomTempRaw": _r(room_temp_raw),
        "roomOffsetC": _r(room_offset_c),
        "roomTemp": _r(room_temp),
        "targetTemp": _r(target_temp),
        "effTarget": _r(eff_target),
        "floorTemp": _r(floor_temp),
        "floorSrc": floor_src,
        "outdoorTemp": _r(outdoor_temp, 1),
    }


def append_event(
    events: list[dict[str, Any]],
    ev: dict[str, Any],
    *,
    now: datetime,
    keep_days: int = COMFORT_KEEP_DAYS,
    max_events: int = COMFORT_MAX_EVENTS,
) -> list[dict[str, Any]]:
    """보관 기간/건수 제한을 적용해 이벤트 추가."""
    cutoff = now - timedelta(days=keep_days)
    kept = [e for e in events if (_parse(e.get("at")) or now) >= cutoff]
    kept.append(ev)
    return kept[-max_events:]


def _peak_hours(hours: list[int]) -> tuple[str | None, float]:
    """조작이 몰린 3시간 구간 (자정 넘김 포함)과 비중."""
    if not hours:
        return None, 0.0
    best_h, best_n = 0, -1
    for h0 in range(24):
        n = sum(1 for h in hours if (h - h0) % 24 < 3)
        if n > best_n:
            best_h, best_n = h0, n
    return f"{best_h:02d}~{(best_h + 3) % 24:02d}시", best_n / len(hours)


def _ceil_half(v: float) -> float:
    return math.ceil(v * 2.0 - 1e-9) / 2.0


def analyze(
    events: list[dict[str, Any]],
    rooms: dict[str, dict[str, Any]],
    *,
    now: datetime,
    floor_min_c: float = 0.0,
    window_days: int = COMFORT_WINDOW_DAYS,
) -> dict[str, Any]:
    """최근 window_days 조작 이벤트로 방별 요약과 제안 목록을 만든다.

    분류 (조작 당시 dev = 실내 - 실효 목표):
      - dev >= -0.5 : 목표 근처인데 추움 -> 목표 상향 제안 (바닥이 차가웠으면 바닥 최저 온도 제안)
      - dev <  -0.5 : 목표보다 이미 낮음 -> 설정 문제가 아니라 난방 부족(차단/지연) 점검 안내
    """
    cutoff = now - timedelta(days=window_days)
    by_room: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        at = _parse(e.get("at"))
        if at is None or at < cutoff or e.get("room") not in rooms:
            continue
        by_room.setdefault(e["room"], []).append(e)

    summary: dict[str, dict[str, Any]] = {}
    suggestions: list[dict[str, Any]] = []
    floor_cold_temps: list[float] = []
    floor_cold_rooms: list[str] = []

    for name, evs in sorted(by_room.items()):
        cfg = rooms[name]
        cur_off = float(cfg.get("roomTempOffsetC") or 0.0)
        cur_target = float(cfg.get("targetTemp") or 0.0)
        rows: list[dict[str, Any]] = []
        for e in evs:
            raw = e.get("roomTempRaw")
            rt = (float(raw) + cur_off) if raw is not None else e.get("roomTemp")
            eff = e.get("effTarget")
            if rt is None or eff is None:
                continue
            base = e.get("targetTemp")
            dev = float(rt) - float(eff)
            rows.append({
                "rt": float(rt),
                "dev": dev,
                "schedOff": float(eff) - float(base if base is not None else eff),
                "floor": e.get("floorTemp"),
                "floorSrc": e.get("floorSrc"),
                "hour": (_parse(e.get("at")) or now).hour,
                "source": e.get("source"),
            })
        at_target = [x for x in rows if x["dev"] >= COMFORT_AT_TARGET_DEV_C]
        underheat = [x for x in rows if x["dev"] < COMFORT_AT_TARGET_DEV_C]
        floor_cold = [
            x for x in at_target
            if x["floorSrc"] in _FLOOR_SRCS and x["floor"] is not None
            and float(x["floor"]) < COMFORT_FLOOR_COLD_C and x["dev"] >= -0.3
        ]
        peak, share = _peak_hours([x["hour"] for x in rows])
        floors = [float(x["floor"]) for x in rows if x["floor"] is not None and x["floorSrc"] in _FLOOR_SRCS]
        summary[name] = {
            "count": len(evs),
            "wallpad": sum(1 for e in evs if e.get("source") == "WALLPAD"),
            "manual": sum(1 for e in evs if e.get("source") == "ITSS_MANUAL"),
            "atTarget": len(at_target),
            "underheat": len(underheat),
            "floorCold": len(floor_cold),
            "medianRoomC": _r(median([x["rt"] for x in rows])) if rows else None,
            "medianDevC": _r(median([x["dev"] for x in rows])) if rows else None,
            "medianFloorC": _r(median(floors)) if floors else None,
            "peakHours": peak if len(rows) >= COMFORT_MIN_EVENTS and share >= 0.6 else None,
            "lastAt": max(str(e.get("at")) for e in evs),
        }
        conf = "MEDIUM" if len(at_target) >= 2 * COMFORT_MIN_EVENTS else "LOW"

        if floor_cold and len(floor_cold) * 3 >= len(at_target) * 2:
            # 실내는 목표 근처, 바닥이 차가울 때 조작 -> 공기 목표보다 바닥 최저 온도 문제
            # (바닥은 방3/방2 가 공유하므로 근거 건수는 방 구분 없이 합산)
            floor_cold_temps.extend(float(x["floor"]) for x in floor_cold)
            floor_cold_rooms.append(name)
        elif len(at_target) >= COMFORT_MIN_EVENTS:
            # 제안 목표(기본 목표 기준) = 그때 실내 + 0.4 - 당시 스케줄/외기/프리셋 오프셋
            needed = [x["rt"] + COMFORT_TARGET_MARGIN_C - x["schedOff"] for x in at_target]
            cap = DEFAULT_OVERHEAT_ROOM_C - 0.5
            sug = min(round(median(needed), 1), round(cur_target + COMFORT_TARGET_MAX_STEP_C, 1), cap)
            if sug >= cur_target + 0.2:
                suggestions.append({
                    "id": f"target:{name}",
                    "type": "TARGET_UP",
                    "room": name,
                    "current": round(cur_target, 1),
                    "suggested": sug,
                    "events": len(at_target),
                    "confidence": conf,
                    "reason": (
                        f"목표 근처(중앙 편차 {median([x['dev'] for x in at_target]):+.2f}°C)에서 "
                        f"{len(at_target)}회 난방을 켬 · 당시 실내 중앙 {median([x['rt'] for x in at_target]):.1f}°C"
                        + (f" · 주로 {summary[name]['peakHours']}" if summary[name]["peakHours"] else "")
                    ),
                })
        if len(underheat) >= COMFORT_MIN_EVENTS and len(underheat) >= len(at_target):
            suggestions.append({
                "id": f"control:{name}",
                "type": "CONTROL_CHECK",
                "room": name,
                "current": None,
                "suggested": None,
                "events": len(underheat),
                "confidence": conf,
                "reason": (
                    f"목표보다 이미 {-median([x['dev'] for x in underheat]):.1f}°C 낮을 때 {len(underheat)}회 조작 — "
                    "목표 설정이 아니라 난방 부족(차단 사유·센서·가동 시간) 점검 필요"
                ),
            })

    if len(floor_cold_temps) >= COMFORT_MIN_EVENTS:
        med_floor = median(floor_cold_temps)
        sug_floor = min(FLOOR_MIN_MAX_C, max(COMFORT_FLOOR_MIN_LOW_C, _ceil_half(med_floor + FLOOR_MIN_HYST_C)))
        if sug_floor > float(floor_min_c or 0.0) + 0.25:
            suggestions.append({
                "id": "floorMin",
                "type": "FLOOR_MIN",
                "room": None,
                "rooms": floor_cold_rooms,
                "current": round(float(floor_min_c or 0.0), 1),
                "suggested": sug_floor,
                "events": len(floor_cold_temps),
                "confidence": "MEDIUM" if len(floor_cold_temps) >= 2 * COMFORT_MIN_EVENTS else "LOW",
                "reason": (
                    f"실내는 목표 근처인데 바닥 중앙 {med_floor:.1f}°C 에서 {len(floor_cold_temps)}회 난방을 켬 "
                    f"({' · '.join(floor_cold_rooms)}) -> 바닥 최저 온도 {sug_floor:.1f}°C"
                ),
            })

    return {
        "windowDays": window_days,
        "eventCount": sum(len(v) for v in by_room.values()),
        "byRoom": summary,
        "suggestions": suggestions,
        "minEvents": COMFORT_MIN_EVENTS,
    }


def recent_events(events: list[dict[str, Any]], *, since: datetime) -> list[dict[str, Any]]:
    return [e for e in events if (_parse(e.get("at")) or since) >= since]
