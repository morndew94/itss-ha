"""센서 보고 주기 감지 및 센서 시각 기준 기울기/변화량 계산.

Zigbee 등 이벤트형 온도 센서는 값이 변할 때(또는 heartbeat 주기마다)만 보고하므로,
제어 주기(예: 60초) 기준으로 기울기를 계산하면 수 분간 누적된 변화가 1분 안에
일어난 것처럼 보여 환기 감지 오탐·예측 오차가 커진다.

이 모듈은 엔티티별로 HA ``last_reported``(없으면 ``last_updated``) 시각을 추적하여
  - 관측 보고 주기(중앙값/최근 최대값)를 학습하고
  - 적응형 stale 임계값을 산출하며
  - 센서 보고 시각 기준 선형 보간으로 구간 변화량, 최근 변화율, 회귀 기울기를 계산한다.

트랙 상태는 JSON 직렬화 가능한 dict 로 유지되어 SQLite KV 에 그대로 저장된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any

from ..const import (
    DEFAULT_MAX_SENSOR_AGE_SEC,
    SENSOR_ADAPT_MIN_SPAN_SEC,
    SENSOR_LONG_GAP_SEC,
    SENSOR_MIN_INTERVALS_FOR_STALE,
    SENSOR_STALE_FACTOR,
    SENSOR_STALE_MIN_SEC,
)

LONG_GAP_KEEP_SEC = 24 * 3600.0  # 긴 공백 이력 보관 24시간
MAX_LONG_GAPS = 300

MAX_SAMPLES = 60            # 보관할 최대 보고 샘플 수
SAMPLE_KEEP_MIN = 120.0     # 샘플 보관 시간 (분)
MAX_INTERVALS = 24          # 보고 간격 이력 개수
MIN_INTERVALS_FOR_TYPICAL = 3
MIN_INTERVAL_SEC = 1.0
MAX_INTERVAL_SEC = 6 * 3600.0


def new_track() -> dict[str, Any]:
    """빈 센서 트랙 생성."""
    return {
        "lastReportAt": None,   # 마지막 보고 시각 (ISO, 센서 기준)
        "lastValue": None,
        "tsSource": None,       # last_reported / last_updated / poll
        "reportCount": 0,
        "intervals": [],        # 최근 보고 간격 (초)
        "longGaps": [],         # 최근 24시간 120초 이상 공백 [[ISO 시각, 초], ...]
        "firstReportAt": None,  # 관측 시작 시각 (적응형 임계 최소 관측기간 판정)
        "samples": [],          # [[ISO 시각, 값], ...] 센서 보고 시각 기준
    }


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def observe(
    track: dict[str, Any],
    value: float | None,
    report_ts_iso: str | None,
    now: datetime,
    ts_source: str | None = None,
) -> bool:
    """새 판독값을 반영하고, 새 보고였으면 True 를 반환한다.

    - 보고 시각이 바뀌었거나 값이 바뀌었으면 새 보고로 간주
    - 보고 시각을 모르면(가짜/구형 HA) 현재 제어 시각을 보고 시각으로 사용
    """
    if value is None:
        return False
    report_ts = _parse(report_ts_iso)
    if report_ts is None or report_ts > now + timedelta(seconds=5):
        report_ts = now
        ts_source = ts_source or "poll"

    last_ts = _parse(track.get("lastReportAt"))
    last_val = track.get("lastValue")
    is_new = last_ts is None or report_ts != last_ts or last_val != value
    if not is_new:
        return False
    if last_ts is not None and report_ts <= last_ts:
        # 같은 시각인데 값만 바뀐 경우 (시각 정보 부정확) -> 현재 시각으로 대체
        report_ts = max(now, last_ts + timedelta(seconds=MIN_INTERVAL_SEC))

    if last_ts is not None:
        gap = (report_ts - last_ts).total_seconds()
        if MIN_INTERVAL_SEC <= gap <= MAX_INTERVAL_SEC:
            intervals = list(track.get("intervals") or [])
            intervals.append(round(gap, 1))
            track["intervals"] = intervals[-MAX_INTERVALS:]
        if gap >= SENSOR_LONG_GAP_SEC:
            gaps = list(track.get("longGaps") or [])
            gaps.append([report_ts.isoformat(timespec="seconds"), round(min(gap, MAX_INTERVAL_SEC), 1)])
            track["longGaps"] = gaps[-MAX_LONG_GAPS:]
    if not track.get("firstReportAt"):
        track["firstReportAt"] = report_ts.isoformat(timespec="seconds")
    # 24시간 지난 긴 공백 이력 정리
    gap_cutoff = now - timedelta(seconds=LONG_GAP_KEEP_SEC)
    track["longGaps"] = [g for g in (track.get("longGaps") or []) if (_parse(g[0]) or now) >= gap_cutoff]

    samples = list(track.get("samples") or [])
    samples.append([report_ts.isoformat(timespec="seconds"), float(value)])
    cutoff = now - timedelta(minutes=SAMPLE_KEEP_MIN)
    kept = []
    for ts_s, v in samples[-MAX_SAMPLES:]:
        t = _parse(ts_s)
        if t is not None and t >= cutoff:
            kept.append([ts_s, v])
    # 보관 기간을 넘었더라도 보간 기준점으로 최소 1개는 유지
    if not kept and samples:
        kept = [samples[-1]]
    track["samples"] = kept
    track["lastReportAt"] = report_ts.isoformat(timespec="seconds")
    track["lastValue"] = float(value)
    track["tsSource"] = ts_source or track.get("tsSource") or "poll"
    track["reportCount"] = int(track.get("reportCount") or 0) + 1
    return True


def typical_interval_sec(track: dict[str, Any]) -> float | None:
    """관측 보고 주기 중앙값(초). 표본 부족 시 None."""
    iv = track.get("intervals") or []
    if len(iv) < MIN_INTERVALS_FOR_TYPICAL:
        return None
    return round(float(median(iv)), 1)


def max_interval_sec(track: dict[str, Any]) -> float | None:
    """최대 보고 간격(초) = max(최근 간격들, 최근 24시간 긴 공백). 이벤트형 센서의 heartbeat 공백 반영."""
    iv = track.get("intervals") or []
    if len(iv) < MIN_INTERVALS_FOR_TYPICAL:
        return None
    long_max = max((float(g[1]) for g in (track.get("longGaps") or [])), default=0.0)
    return round(max(float(max(iv)), long_max), 1)


def observed_span_sec(track: dict[str, Any]) -> float:
    """관측 시작 ~ 마지막 보고까지의 기간(초)."""
    first = _parse(track.get("firstReportAt"))
    last = _parse(track.get("lastReportAt"))
    if first is None or last is None:
        return 0.0
    return max(0.0, (last - first).total_seconds())


def stale_threshold_sec(track: dict[str, Any], default_sec: float = DEFAULT_MAX_SENSOR_AGE_SEC) -> float:
    """적응형 stale 임계값(초).

    다음을 모두 만족할 때만 clamp(최대 보고 간격 x 2.0, 600초, 기본값)으로 축소한다.
      - 시각 출처가 last_reported (last_updated 는 값 변화 간격이라 보고 주기를 알 수 없음)
      - 보고 간격 6개 이상 관측
      - 관측 기간 6시간 이상 (온도 안정 구간의 heartbeat 공백까지 학습)
    최대 보고 간격에는 최근 24시간의 긴 공백(120초 이상)이 포함되어 오탐을 막는다.
    """
    iv = track.get("intervals") or []
    if (
        track.get("tsSource") != "last_reported"
        or len(iv) < SENSOR_MIN_INTERVALS_FOR_STALE
        or observed_span_sec(track) < SENSOR_ADAPT_MIN_SPAN_SEC
    ):
        return float(default_sec)
    adaptive = float(max_interval_sec(track) or max(iv)) * SENSOR_STALE_FACTOR
    return round(max(SENSOR_STALE_MIN_SEC, min(float(default_sec), adaptive)), 1)


def report_age_sec(track: dict[str, Any], now: datetime) -> float | None:
    """마지막 보고 이후 경과 시간(초)."""
    last = _parse(track.get("lastReportAt"))
    if last is None:
        return None
    return round(max(0.0, (now - last).total_seconds()), 1)


def _points(track: dict[str, Any]) -> list[tuple[datetime, float]]:
    pts: list[tuple[datetime, float]] = []
    for ts_s, v in track.get("samples") or []:
        t = _parse(ts_s)
        if t is not None and v is not None:
            pts.append((t, float(v)))
    pts.sort(key=lambda p: p[0])
    return pts


def value_at(track: dict[str, Any], t: datetime) -> float | None:
    """센서 보고점 사이를 선형 보간한 t 시각의 추정값 (첫 보고 이전이면 None)."""
    pts = _points(track)
    if not pts or t < pts[0][0]:
        return None
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            span = (t1 - t0).total_seconds()
            if span <= 0:
                return v1
            return v0 + (v1 - v0) * ((t - t0).total_seconds() / span)
    return pts[-1][1]


def delta_over(
    track: dict[str, Any],
    now: datetime,
    window_min: float,
    *,
    min_span_min: float = 1.0,
) -> float | None:
    """최근 window_min 동안의 변화량 (최신 보고값 - 구간 시작 보간값).

    구간 시작이 첫 보고보다 이르면 첫 보고점을 기준으로 하되, 실제 span 이
    min_span_min 미만이면 None.
    """
    pts = _points(track)
    if len(pts) < 2:
        return None
    start = now - timedelta(minutes=window_min)
    base_t = max(start, pts[0][0])
    span_min = (now - base_t).total_seconds() / 60.0
    if span_min < min_span_min:
        return None
    base_v = value_at(track, base_t)
    if base_v is None:
        return None
    return round(pts[-1][1] - base_v, 3)


def recent_rate_per_min(
    track: dict[str, Any],
    now: datetime,
    *,
    max_age_min: float = 3.0,
    min_gap_min: float = 1.0,
) -> float:
    """가장 최근 보고 쌍의 변화율(°C/min). 센서 보고 시각 간격으로 나눈다.

    최근 보고가 max_age_min 보다 오래됐으면 0.0 (현재 급변 중이 아님).
    두 보고 간격이 min_gap_min 미만이면 더 이전 보고점을 사용해 과대평가를 막는다.
    """
    pts = _points(track)
    if len(pts) < 2:
        return 0.0
    t_last, v_last = pts[-1]
    if (now - t_last).total_seconds() / 60.0 > max_age_min:
        return 0.0
    for t_prev, v_prev in reversed(pts[:-1]):
        gap_min = (t_last - t_prev).total_seconds() / 60.0
        if gap_min >= min_gap_min:
            return (v_last - v_prev) / gap_min
    return 0.0


def regression_slope_per_min(
    track: dict[str, Any],
    now: datetime,
    window_min: float,
    *,
    min_span_min: float = 8.0,
    min_points: int = 3,
) -> float | None:
    """구간 내 보고점 + 구간 시작 보간점에 대한 최소자승 기울기(°C/min).

    0.1°C 양자화 잡음을 줄이기 위해 짧은 구간(min_span_min 미만)이나
    보고점 부족 시 None 을 반환한다 (호출부에서 외삽 0 처리).
    """
    pts = _points(track)
    if len(pts) < 2:
        return None
    start = now - timedelta(minutes=window_min)
    sel = [(t, v) for t, v in pts if t >= start]
    if pts[0][0] < start:
        anchor = value_at(track, start)
        if anchor is not None:
            sel.insert(0, (start, anchor))
    if len(sel) < min_points:
        return None
    span_min = (sel[-1][0] - sel[0][0]).total_seconds() / 60.0
    if span_min < min_span_min:
        return None
    t0 = sel[0][0]
    xs = [(t - t0).total_seconds() / 60.0 for t, _ in sel]
    ys = [v for _, v in sel]
    n = float(len(xs))
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / sxx


def adaptive_window_min(track: dict[str, Any], nominal_min: float, *, factor: float = 2.5, cap_min: float = 45.0) -> float:
    """보고 주기가 길면 계산 구간을 늘려 최소 2~3개 보고점이 들어오도록 한다."""
    typ = typical_interval_sec(track)
    if typ is None:
        return nominal_min
    return round(min(cap_min, max(nominal_min, factor * typ / 60.0)), 1)


def latency_rise_c(
    track: dict[str, Any],
    now: datetime,
    slope_per_min: float,
    *,
    cap_c: float = 1.0,
) -> float:
    """보고 지연 보상: 마지막 보고 이후 상승했을 것으로 추정되는 온도(°C).

    가열 중 바닥처럼 상승 추세인 센서에서, 보고 주기가 길수록 실제값이 보고값보다
    높을 수 있으므로 min(경과시간, 관측 보고 주기) x 상승 기울기 만큼 보수적으로 더한다.
    """
    if slope_per_min <= 0:
        return 0.0
    age = report_age_sec(track, now)
    if age is None:
        return 0.0
    typ = typical_interval_sec(track) or 60.0
    horizon_min = min(age, max(60.0, typ)) / 60.0
    return round(min(cap_c, slope_per_min * horizon_min), 2)


def summarize(track: dict[str, Any], now: datetime, default_stale_sec: float = DEFAULT_MAX_SENSOR_AGE_SEC) -> dict[str, Any]:
    """UI/진단용 요약."""
    return {
        "lastReportAt": track.get("lastReportAt"),
        "lastValue": track.get("lastValue"),
        "tsSource": track.get("tsSource"),
        "reportCount": int(track.get("reportCount") or 0),
        "intervalSamples": len(track.get("intervals") or []),
        "typicalIntervalSec": typical_interval_sec(track),
        "maxIntervalSec": max_interval_sec(track),
        "observedSpanHours": round(observed_span_sec(track) / 3600.0, 1),
        "staleThresholdSec": stale_threshold_sec(track, default_stale_sec),
        "ageSec": report_age_sec(track, now),
    }
