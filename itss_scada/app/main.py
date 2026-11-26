"""FastAPI SCADA Server & Ingress Web UI for the I.T.S.S. Home Assistant Add-on (v2.7)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import comfort
from .const import (
    CALIBRATION_MIN_REPORTS,
    CALIBRATION_MIN_SPAN_H,
    CALIBRATION_REF_SPREAD_WARN_C,
    DAILY_REPORT_DEFAULT_HOUR,
    DAILY_REPORT_MAX_CHARS,
    DAILY_REPORT_SEND_WINDOW_H,
    DEFAULT_ALLOWED_CLIENT_IPS,
    DEFAULT_CONTROL_BAND_HALF,
    DEFAULT_FLOOR_RISE_RATE_C_PER_MIN,
    DEFAULT_MAX_SENSOR_AGE_SEC,
    DEFAULT_MIN_OFF_SEC,
    DEFAULT_MIN_ON_SEC,
    DEFAULT_OVERHEAT_FLOOR_C,
    DEFAULT_OVERHEAT_ROOM_C,
    DEFAULT_RECHECK_DELAY_SEC,
    DISPATCH_RESEND_MIN,
    EXTERNAL_BOOST_DEFAULT_MIN,
    EXTERNAL_BOOST_MAX_MIN,
    FLOOR_MIN_AIR_MARGIN_C,
    FLOOR_MIN_HYST_C,
    FLOOR_MIN_MAX_C,
    FLOOR_PEAK_MARGIN_C,
    HEAT_SETPOINT_MAX_BOOST_C,
    KST,
    LOOP_FAILURE_ALERT_COUNT,
    MANUAL_HEAT_DEFAULT_MIN,
    MANUAL_HEAT_MAX_MIN,
    MIN_PULSE_MIN,
    MODEL_ADAPTIVE_RC,
    MODEL_LINEAR_V12,
    ROOM_TEMP_OFFSET_MAX_C,
    STRATEGY_HYBRID,
    THERMOSTAT_SETPOINT_STEP,
    UNDERHEAT_ALERT_MIN,
    UNDERHEAT_MARGIN_C,
    VERSION,
)
from .debug_trace import TraceWriter, configure_logging, tail_log
from .ha_client import HomeAssistantClient
from .integrations.google_gemini_advisor import (
    GeminiThermalAdvisor,
    compute_daily_kpis,
    compute_room_daily_stats,
    format_daily_report_text,
)
from .integrations.google_weather import FallbackWeatherProvider
from .integrations.notifications import PushNotifier
from .models.schema import CycleRecord, SensorSnapshot, ThermalInput, now_kst_iso
from .protocols import sensor_tracker as st
from .protocols.multi_zone import GroupMember, MultiZoneController, ZoneReading, evaluate_floor_group
from .protocols.p000_sentinel import SentinelConfig, SentinelProtocol
from .protocols.p100_core import compute_opposing_interval, round_thermostat_setpoint
from .protocols.p200_therm import (
    AdaptiveRCModel,
    LinearExtrapolationV12,
    RCParameters,
    ResidualHeatStats,
    ThermalPredictor,
)
from .protocols.p300_atmos import AtmosProtocol
from .protocols.rc_optimizer import RCObservation, fit_rc_parameters
from .protocols.thermal_lag_learner import learn_room_lag_profile
from .storage.db import ScadaDatabase

_LOGGER = logging.getLogger("itss")
_LOG_CYCLE = logging.getLogger("itss.cycle")      # 회차 요약 / 방별 판단 근거
_LOG_SENSOR = logging.getLogger("itss.sensor")    # 센서 보고 주기 / 누락
_LOG_DISPATCH = logging.getLogger("itss.dispatch")  # 조절기 명령 전송
_LOG_WEATHER = logging.getLogger("itss.weather")  # 외기 소스

STATIC_DIR = Path(__file__).parent / "static"

# HA 엔티티 ID / 방 이름 입력 검증 (XSS·경로 문제 방지용 서버측 방어)
ENTITY_ID_PATTERN = r"^[a-z_]+\.[a-z0-9_]+$"
OPTIONAL_ENTITY_ID_PATTERN = r"^$|^[a-z_]+\.[a-z0-9_]+$"
ROOM_NAME_PATTERN = r"^[^<>\"'&/\\\x00-\x1f]{1,40}$"
LABEL_PATTERN = r"^[^<>\"'&\\\x00-\x1f]{1,30}$"

PRESET_OFFSETS = {
    "AUTO_ALL": 0.0,
    "AWAY": -2.0,
    "SLEEP": -0.3,
    "PREHEAT": 0.2,
    "VENTILATION": 0.0,
    "ALL_OFF": 0.0,
}

REASON_KO = {
    "P000_OVERHEAT_CUTOFF": (
        f"긴급 과열 차단(실내 >= {DEFAULT_OVERHEAT_ROOM_C}°C 또는 바닥 >= {DEFAULT_OVERHEAT_FLOOR_C}°C)"
    ),
    "P000_FLOOR_LIMIT_OFF": "바닥 온도 상한 도달(잔열 과다 방지 차단)",
    "P000_FLOOR_PREDICTED_LIMIT": "바닥 잔열 상승 예측 상한 도달(바닥 오버슈트 선차단)",
    "P000_FLOOR_RESIDUAL_ACTIVE": "따뜻한 바닥 잔열 방출·실내 회복 중(연속 재가동 보류)",
    "P000_FLOOR_UNCONFIGURED": "마스터 바닥 온도 센서 미설정(바닥 보호 불가로 난방 차단)",
    "P000_MAX_RUN_CUTOFF": "외기·바닥 여유 연동 1회 최대 가동 시간 도달(잔열 휴지 진입)",
    "P000_RESIDUAL_COOLDOWN": "차단 후 잔열 흡수 대기 중",
    "P000_WINDOW_OPEN": "창문 열림 센서 감지(즉시 난방 중지)",
    "P000_VENTILATION_PROTECT": "환기 급락 감지(센서 보고 기준 급락/5분 급랭 -> 환기 보호 유지 시작)",
    "P000_VENTILATION_HOLD": "환기 보호 및 자연 회복 대기 중",
    "P000_SENSOR_MISSING": "온도 센서 값 누락으로 안전 차단",
    "P000_SENSOR_STALE": "온도 센서 보고 중단(보고 주기 기반 임계 초과)으로 안전 차단",
    "P000_ANTI_CHATTER": "최소 가동/휴지 시간 유지(채터링 방지)",
    "P000_MANUAL_OVERRIDE": "사용자 수동 운전 조작",
    "P000_SAFETY_CUTOFF": "다중 방 안전 차단(바닥/실내 상한)",
    "P000_GLOBAL_OVERHEAT_OR_FLOOR_LIMIT": "전체 방 과열/바닥 상한 차단",
    "P200_PREDICTIVE_ON": "P200 예측 온도가 목표 하한 이하 하락 예상(예측 기동)",
    "P200_PREDICTIVE_OFF": "P200 예측/바닥 잔열이 목표 상한 도달 예상(예측 선차단)",
    "HYBRID_COHEAT_BATCH": "타 방 보일러 가동 중 동시 난방 합류(단펄스 방지)",
    "HYBRID_PRIMARY_HEAT": "목표 하한 도달(하이브리드 기동)",
    "HYBRID_EARLY_CUTOFF": "목표 상한 도달(개별 선차단)",
    "HYBRID_IDLE": "목표 범위 내 대기",
    "SIMULTANEOUS_WEIGHTED_DEMAND": "전체 방 가중평균 하한 도달(동시 기동)",
    "SIMULTANEOUS_WEIGHTED_TARGET_REACHED": "전체 방 가중평균 상한 도달(동시 차단)",
    "SIMULTANEOUS_CONTINUE": "전체 방 가중평균 가열 지속",
    "SIMULTANEOUS_IDLE": "전체 방 목표 범위 내 대기",
    "GROUP_DEMAND_HEAT": "동시 제어 그룹 가중평균 예측이 목표 하한 이하(그룹 동시 기동)",
    "GROUP_CONTINUE": "동시 제어 그룹 가열 지속",
    "GROUP_TARGET_REACHED": "동시 제어 그룹 가중평균 예측이 목표 도달(그룹 동시 정지)",
    "GROUP_IDLE": "동시 제어 그룹 목표 범위 내 대기",
    "GROUP_NO_ROOM_DATA": "동시 제어 그룹 실내 온도 없음(대기)",
    "GROUP_MEMBER_WARM_SKIP": "그룹 가열 중이나 이 방은 목표 상한 이상(이 방만 개별 정지)",
    "GROUP_LEADER_UNAVAILABLE": "바닥 기준 방 판정 실패(동시 제어 안전 정지)",
    "MANUAL_HEAT_TIMEOUT": "수동 난방 설정 시간 경과(자동 중지 · 자동 제어 복귀)",
    "MANUAL_HEAT_CANCELLED": "수동 난방 중 외부에서 끔(자동 제어 복귀)",
    "GROUP_FLOOR_MIN_HEAT": "바닥 최저 온도 미만(바닥 기준 그룹 보충 가동)",
    "GROUP_FLOOR_MIN_CONTINUE": "바닥 최저 온도 보충 가동 지속",
    "GROUP_FLOOR_MIN_REACHED": "바닥 최저 온도 보충 목표 도달 예측(그룹 정지)",
    "EXTERNAL_BOOST": "외부 조작(월패드/다른 자동화)으로 켜짐 감지(설정 시간 가동 후 자동 중지)",
    "EXTERNAL_BOOST_END": "외부 조작 가동 시간 종료(자동 중지, 잔열 휴지 진입)",
    "EXTERNAL_BOOST_CANCELLED": "외부 조작 가동 종료(외부에서 끔 또는 조절기 자체 설정온도 도달)",
    "INTERNAL_ERROR": "내부 처리 오류(로그 확인 필요)",
}

CRITICAL_REASONS = (
    "P000_OVERHEAT_CUTOFF",
    "P000_FLOOR_LIMIT_OFF",
    "P000_FLOOR_PREDICTED_LIMIT",
    "P000_FLOOR_UNCONFIGURED",
    "P000_MAX_RUN_CUTOFF",
    "P000_WINDOW_OPEN",
    "P000_VENTILATION_PROTECT",
    "P000_SENSOR_MISSING",
    "P000_SENSOR_STALE",
)

COOLDOWN_REASONS = (
    "P000_RESIDUAL_COOLDOWN",
    "P000_MAX_RUN_CUTOFF",
    "P000_FLOOR_LIMIT_OFF",
    "P000_FLOOR_PREDICTED_LIMIT",
    "P000_OVERHEAT_CUTOFF",
    "P200_PREDICTIVE_OFF",
    "HYBRID_EARLY_CUTOFF",
    "GROUP_TARGET_REACHED",
    "EXTERNAL_BOOST_END",
    "GROUP_FLOOR_MIN_REACHED",
    "MANUAL_HEAT_TIMEOUT",
)

# 애드온 옵션 키 -> 내부 설정 키
_OPTION_SETTING_MAP: dict[str, tuple[str, Any]] = {
    "dry_run": ("dryRun", bool),
    "zone_strategy": ("zoneStrategy", str),
    "predictor_model": ("predictorModel", str),
    "master_floor_entity": ("masterFloorEntity", str),
    "outdoor_temp_entity": ("outdoorTempEntity", str),
    "weather_location": ("weatherLocation", str),
    "heat_on_setpoint": ("heatOnSetpoint", float),
    "heat_off_setpoint": ("heatOffSetpoint", float),
    "floor_off_limit_c": ("floorOffLimitC", float),
    "max_run_cap_min": ("maxRunCapMin", int),
    "cooldown_min": ("cooldownMin", int),
    "ventilation_hold_min": ("ventilationHoldMin", int),
    "history_retention_days": ("historyRetentionDays", int),
    "gas_krw_per_hour": ("gasKrwPerHour", int),
}
_OPTION_ONLY_SETTINGS = {"updateIntervalSec"}

_DEFAULT_SETTINGS: dict[str, Any] = {
    "dryRun": True,
    "zoneStrategy": STRATEGY_HYBRID,
    "predictorModel": MODEL_LINEAR_V12,
    "updateIntervalSec": 60,
    "masterFloorEntity": "",
    "outdoorTempEntity": "",
    "weatherLocation": "서울",
    "heatOnSetpoint": 24.0,
    "heatOffSetpoint": 15.0,
    "floorOffLimitC": 26.5,
    "maxRunCapMin": 15,
    "cooldownMin": 20,
    "ventilationHoldMin": 20,
    "externalBoostMin": EXTERNAL_BOOST_DEFAULT_MIN,
    "manualHeatMaxMin": MANUAL_HEAT_DEFAULT_MIN,
    "floorMinC": 0.0,
    "dailyReportEnabled": True,
    "dailyReportHour": DAILY_REPORT_DEFAULT_HOUR,
    "historyRetentionDays": 30,
    "gasKrwPerHour": 1200,
    "notifyEvents": True,
}


def _load_options() -> dict[str, Any]:
    opt_path = os.environ.get("ITSS_OPTIONS_PATH", "/data/options.json")
    if os.path.exists(opt_path):
        try:
            with open(opt_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("애드온 옵션 파일 읽기 실패: %s", opt_path)
    return {}


def resolve_settings(
    opts: dict[str, Any],
    saved: dict[str, Any] | None,
    prev_snapshot: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """설정 우선순위 결정.

    1) 기본값 <- 애드온 옵션
    2) UI 저장값(saved)이 덮어씀
    3) 단, 지난 부팅 이후 HA 애드온 설정 화면에서 값이 바뀐 옵션은 옵션이 우선
       (prev_snapshot 이 없으면 = 업그레이드 첫 부팅, 기존 UI 저장값 유지)
    """
    settings = dict(_DEFAULT_SETTINGS)
    for opt_key, (s_key, cast) in _OPTION_SETTING_MAP.items():
        if opt_key in opts and opts[opt_key] is not None:
            try:
                settings[s_key] = cast(opts[opt_key])
            except (TypeError, ValueError):
                _LOGGER.warning("옵션 형식 오류 무시: %s=%r", opt_key, opts[opt_key])
    if opts.get("update_interval_sec") is not None:
        settings["updateIntervalSec"] = int(opts["update_interval_sec"])

    reverse = {s_key: opt_key for opt_key, (s_key, _) in _OPTION_SETTING_MAP.items()}
    overridden: list[str] = []
    for s_key, val in (saved or {}).items():
        if s_key not in settings or s_key in _OPTION_ONLY_SETTINGS:
            continue
        opt_key = reverse.get(s_key)
        if (
            opt_key is not None
            and prev_snapshot is not None
            and opt_key in opts
            and prev_snapshot.get(opt_key) != opts.get(opt_key)
        ):
            overridden.append(s_key)
            continue
        settings[s_key] = val

    settings["maxRunCapMin"] = int(max(MIN_PULSE_MIN, min(15, int(settings["maxRunCapMin"]))))
    return settings, overridden


def _to_float(val: Any) -> float | None:
    try:
        return None if val is None else float(val)
    except (TypeError, ValueError):
        return None


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _normalize_ip(host: str | None) -> str | None:
    if not host:
        return None
    return host[7:] if host.startswith("::ffff:") else host


def _resolve_allowed_ips(explicit: Iterable[str] | None) -> frozenset[str] | None:
    """허용 클라이언트 IP. None 이면 필터 해제 (ITSS_ALLOWED_IPS='*', 개발용)."""
    if explicit is not None:
        return frozenset(explicit)
    env = os.environ.get("ITSS_ALLOWED_IPS")
    if env is not None:
        extra = [v.strip() for v in env.split(",") if v.strip()]
        if "*" in extra:
            return None
        return frozenset(DEFAULT_ALLOWED_CLIENT_IPS) | frozenset(extra)
    return frozenset(DEFAULT_ALLOWED_CLIENT_IPS)


class RoomCreateRequest(BaseModel):
    roomName: str = Field(..., pattern=ROOM_NAME_PATTERN)
    roomTempEntity: str = Field(..., pattern=ENTITY_ID_PATTERN)
    climateEntity: str = Field(..., pattern=r"^climate\.[a-z0-9_]+$")
    windowSensorEntity: str = Field(default="", pattern=OPTIONAL_ENTITY_ID_PATTERN)
    hasFloorSensor: bool = False
    floorEstimateEnabled: bool = True  # 바닥 센서 없는 방: 마스터 실측 + offset 추정 사용 여부
    floorOffsetC: float = Field(default=0.0, ge=-5.0, le=5.0)
    # 실내 센서 보정값 (°C): 제어용 실내 온도 = 센서 원시값 + 보정값
    # 미지정(None)이면 같은 이름 재등록 시 기존 보정값 유지
    roomTempOffsetC: float | None = Field(default=None, ge=-ROOM_TEMP_OFFSET_MAX_C, le=ROOM_TEMP_OFFSET_MAX_C)
    targetTemp: float = Field(default=23.0, ge=10.0, le=27.0)
    controlMode: str = Field(default="AUTO", pattern="^(AUTO|MANUAL)$")
    manualAction: str = Field(default="HOLD", pattern="^(HEAT|OFF|HOLD)$")
    weight: float = Field(default=1.0, gt=0.0, le=10.0)


class RoomPatchRequest(BaseModel):
    targetTemp: float | None = Field(default=None, ge=10.0, le=27.0)
    controlMode: str | None = Field(default=None, pattern="^(AUTO|MANUAL)$")
    manualAction: str | None = Field(default=None, pattern="^(HEAT|OFF|HOLD)$")
    floorOffsetC: float | None = Field(default=None, ge=-5.0, le=5.0)
    floorEstimateEnabled: bool | None = None
    roomTempOffsetC: float | None = Field(default=None, ge=-ROOM_TEMP_OFFSET_MAX_C, le=ROOM_TEMP_OFFSET_MAX_C)


class SettingsPatchRequest(BaseModel):
    dryRun: bool | None = None
    zoneStrategy: str | None = Field(default=None, pattern="^(hybrid|simultaneous)$")
    predictorModel: str | None = Field(default=None, pattern="^(linear_v12|adaptive_rc)$")
    masterFloorEntity: str | None = Field(default=None, pattern=OPTIONAL_ENTITY_ID_PATTERN)
    outdoorTempEntity: str | None = Field(default=None, pattern=OPTIONAL_ENTITY_ID_PATTERN)
    weatherLocation: str | None = Field(default=None, pattern=r"^[^<>\"'&\\\x00-\x1f]{0,40}$")
    heatOnSetpoint: float | None = Field(default=None, ge=15.0, le=30.0)
    heatOffSetpoint: float | None = Field(default=None, ge=5.0, le=25.0)
    floorOffLimitC: float | None = Field(default=None, ge=22.0, le=29.0)
    maxRunCapMin: int | None = Field(default=None, ge=3, le=15)
    cooldownMin: int | None = Field(default=None, ge=5, le=90)
    ventilationHoldMin: int | None = Field(default=None, ge=5, le=60)
    # 외부 조작 가동 유지 시간 (0 = 즉시 되돌림)
    externalBoostMin: int | None = Field(default=None, ge=0, le=EXTERNAL_BOOST_MAX_MIN)
    # ITSS 수동 HEAT 1회 최대 가동 (분)
    manualHeatMaxMin: int | None = Field(default=None, ge=5, le=MANUAL_HEAT_MAX_MIN)
    # 바닥 최저 온도 (°C, 0 = 사용 안 함)
    floorMinC: float | None = Field(default=None, ge=0.0, le=FLOOR_MIN_MAX_C)
    # Gemini 일일 리포트 자동 발송 (KST 시각)
    dailyReportEnabled: bool | None = None
    dailyReportHour: int | None = Field(default=None, ge=0, le=23)
    historyRetentionDays: int | None = Field(default=None, ge=1, le=365)
    gasKrwPerHour: int | None = Field(default=None, ge=100, le=10000)
    notifyEvents: bool | None = None


class SensorCalibrationRequest(BaseModel):
    """동시 비교 측정 구간 (센서를 한곳에 모아 둔 시간)."""

    start: str = Field(..., min_length=10, max_length=40)
    end: str = Field(..., min_length=10, max_length=40)
    # 비우면 전체 센서 중앙값(합의) 기준, 방 이름이면 그 방 센서(현재 보정 포함) 기준
    reference: str = Field(default="", max_length=40)
    # 기준 온도계 판독값 (°C, 선택) — 주면 이 값이 기준
    referenceTempC: float | None = Field(default=None, ge=0.0, le=40.0)


class PresetRequest(BaseModel):
    preset: str = Field(..., pattern="^(AUTO_ALL|AWAY|SLEEP|PREHEAT|VENTILATION|ALL_OFF)$")


class ScheduleSlot(BaseModel):
    slotId: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,20}$")
    label: str = Field(..., pattern=LABEL_PATTERN)
    startHour: int = Field(..., ge=0, le=23)
    endHour: int = Field(..., ge=0, le=23)
    offsetC: float = Field(..., ge=-5.0, le=3.0)
    enabled: bool = True


class SchedulesUpdateRequest(BaseModel):
    slots: list[ScheduleSlot] = Field(..., max_length=24)


class TestClimateRequest(BaseModel):
    action: str = Field(..., pattern="^(HEAT|OFF)$")


class RCFitRequest(BaseModel):
    observations: list[dict[str, Any]] = Field(..., max_length=10000)


def detect_boiler_running(climate_state: dict[str, Any] | None) -> bool | None:
    """Determine actual boiler/valve running state from thermostat setpoint vs current temp.

    Physical rule for Korean wallpad thermostats (e.g., Kocom):
      - state == 'off' -> False (OFF)
      - setpoint (temperature) > thermostat current_temperature -> True (Running / ON)
      - setpoint (temperature) <= thermostat current_temperature -> False (OFF)
      - fallback to hvac_action == 'heating' when temperatures are unavailable.
    """
    if not climate_state:
        return None
    mode = str(climate_state.get("state") or "").lower()
    if mode in ("off", "unavailable", "unknown"):
        return False if mode == "off" else None

    attrs = climate_state.get("attributes") or {}
    sp = attrs.get("temperature")
    cur = attrs.get("current_temperature")
    if sp is not None and cur is not None:
        try:
            return float(sp) > float(cur)
        except (TypeError, ValueError):
            pass

    hvac_action = attrs.get("hvac_action")
    if hvac_action is not None:
        return str(hvac_action).lower() == "heating"
    return None


def _row_is_boiler_on(c: dict[str, Any]) -> bool:
    """통계용 보일러 ON 판정: 조절기 추정 가동 또는 (실제 송신된) HEAT 명령."""
    if c.get("combustionConfirmed") is True:
        return True
    return c.get("commandAction") == "HEAT" and not c.get("dryRun")


def compute_boiler_stats(
    cycles: list[dict[str, Any]],
    *,
    interval_sec: int = 300,
    gas_krw_per_hour: int = 1200,
) -> dict[str, Any]:
    """Compute boiler runtime, duty cycle %, short-cycling count, and estimated KRW cost.

    v2.7.0: 회차 수 x 주기 가정 대신 실제 분 단위 슬롯 간격(최대 1주기)으로 적산한다.
    시험 운전(dryRun) 회차의 가상 HEAT 명령은 가동 시간에서 제외한다.
    """
    if not cycles:
        return {
            "totalMinutes": 0,
            "heatingMinutes": 0,
            "dutyCyclePct": 0.0,
            "shortCycles": 0,
            "estimatedGasKrw": 0,
        }

    # 같은 분(YYYY-MM-DDTHH:MM)의 여러 방은 보일러 1대로 합산
    by_slot: dict[str, bool] = {}
    for c in cycles:
        ts = str(c.get("executionStartedAt") or "")[:16]
        if len(ts) < 16:
            continue
        by_slot[ts] = by_slot.get(ts, False) or _row_is_boiler_on(c)

    keys = sorted(by_slot.keys())
    step_min = max(0.5, interval_sec / 60.0)
    slot_times: list[datetime | None] = []
    for k in keys:
        try:
            slot_times.append(datetime.fromisoformat(k))
        except ValueError:
            slot_times.append(None)

    durations: list[float] = []
    for i in range(len(keys)):
        if i + 1 < len(keys) and slot_times[i] is not None and slot_times[i + 1] is not None:
            gap = (slot_times[i + 1] - slot_times[i]).total_seconds() / 60.0
            durations.append(max(0.0, min(gap, step_min)))
        else:
            durations.append(step_min)

    total_min = sum(durations)
    heat_min = sum(d for d, k in zip(durations, keys) if by_slot[k])

    # 10분 미만 연속 가동(단펄스) 횟수
    short_cycles = 0
    run_len = 0.0
    for d, k in zip(durations, keys):
        if by_slot[k]:
            run_len += d
        elif run_len > 0:
            if run_len < 10.0:
                short_cycles += 1
            run_len = 0.0
    if 0 < run_len < 10.0:
        short_cycles += 1

    est_krw = int(round((heat_min / 60.0) * gas_krw_per_hour))
    return {
        "totalMinutes": round(total_min, 1),
        "heatingMinutes": round(heat_min, 1),
        "dutyCyclePct": round((heat_min / total_min) * 100.0, 1) if total_min > 0 else 0.0,
        "shortCycles": short_cycles,
        "estimatedGasKrw": est_krw,
    }


def _downsample_chart(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    """차트 포인트를 구간 평균으로 축약 (보일러 ON 은 구간 내 하나라도 ON 이면 ON)."""
    if len(points) <= max_points:
        return points
    bucket = math.ceil(len(points) / max_points)
    out: list[dict[str, Any]] = []
    for i in range(0, len(points), bucket):
        chunk = points[i:i + bucket]

        def _avg(key: str) -> float | None:
            vals = [p[key] for p in chunk if p.get(key) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        out.append({
            "t": chunk[-1]["t"],
            "room": _avg("room"),
            "floor": _avg("floor"),
            "target": _avg("target"),
            "heat": any(p.get("heat") for p in chunk),
        })
    return out


class ItssEngine:
    """Core runtime state and multi-zone P000-P400 control engine for the Add-on."""

    def __init__(
        self,
        db_path: str | None = None,
        ha_client: HomeAssistantClient | None = None,
        notifier: PushNotifier | None = None,
        weather_client: Any = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        opts = options if options is not None else _load_options()
        self.options = opts
        resolved_db = db_path or os.environ.get("ITSS_DB_PATH") or opts.get("db_path") or "./itss_scada.db"
        self.db = ScadaDatabase(resolved_db)
        # 로그/트레이스는 DB 와 같은 데이터 디렉터리(/data) 하위에 저장
        self.data_dir = os.path.dirname(os.path.abspath(resolved_db))
        self.log_path: str | None = None
        self.trace = TraceWriter(
            os.path.join(self.data_dir, "trace"),
            enabled=bool(opts.get("debug_trace", True)),
            retention_days=int(opts.get("debug_trace_days", 7) or 7),
        )
        self._last_weather_label: str | None = None
        self.ha = ha_client or HomeAssistantClient(token=opts.get("ha_token") or None)

        saved_settings = self.db.get_kv("settings", {}) or {}
        prev_snapshot = self.db.get_kv("options_snapshot")
        self.settings, overridden = resolve_settings(opts, saved_settings, prev_snapshot)
        if overridden:
            _LOGGER.info("HA 애드온 설정 변경 감지 -> UI 저장값 대신 애드온 옵션 적용: %s", ", ".join(overridden))
        self.db.set_kv("options_snapshot", {k: opts.get(k) for k in _OPTION_SETTING_MAP if k in opts})
        self.db.set_kv("settings", self.settings)

        self.active_preset: str = str(self.db.get_kv("active_preset", "AUTO_ALL"))
        self.room_timers: dict[str, dict[str, Any]] = self.db.get_kv("room_timers", {}) or {}
        self.sensor_tracks: dict[str, dict[str, Any]] = self.db.get_kv("sensor_tracks", {}) or {}
        self.lag_profiles: dict[str, dict[str, Any]] = {}
        # v2.9.1 쾌적 학습 이벤트 (월패드/ITSS 수동 ON = '추웠다' 신호)
        self.comfort_events: list[dict[str, Any]] = self.db.get_kv("comfort_events", []) or []
        self._report_task: asyncio.Task | None = None
        self._cycle_seq: int = 0
        self.cycle_lock = asyncio.Lock()
        self.loop_state: dict[str, Any] = {
            "bootAt": now_kst_iso(),
            "lastCycleStartedAt": None,
            "lastCycleOkAt": None,
            "lastError": None,
            "lastErrorAt": None,
            "consecutiveFailures": 0,
            "lastRoomErrors": 0,
            "lastPruneAt": None,
            "lastPrunedRows": 0,
        }

        gemini_key = str(opts.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
        self.weather_client = weather_client or FallbackWeatherProvider(
            location=self.settings["weatherLocation"],
            gemini_api_key=gemini_key,
        )
        self.advisor = GeminiThermalAdvisor(gemini_key)
        self.notifier = notifier or PushNotifier(
            ha_client=self.ha,
            notify_service=str(opts.get("notify_service", "") or ""),
            telegram_chat_id=str(opts.get("telegram_chat_id", "") or ""),
        )
        self.atmos = AtmosProtocol()

        # Recompute learned thermal lags and restore active residual cooldowns across restarts
        self.refresh_lag_profiles()
        self._restore_cooldowns_from_history()
        self.prune_history()

    # ------------------------------------------------------------------ 쾌적 학습 (v2.9.1)
    def _record_comfort_event(
        self,
        *,
        now: datetime,
        room: dict[str, Any],
        source: str,
        t: dict[str, Any],
        outdoor_temp: float | None,
    ) -> None:
        """월패드/ITSS 수동 ON 1건을 '추웠다' 신호로 저장 (제안 학습용, 제어 미반영)."""
        ev = comfort.make_event(
            now=now,
            room=room["roomName"],
            source=source,
            room_temp_raw=t.get("roomTempRaw"),
            room_offset_c=float(t.get("roomTempOffsetC") or 0.0),
            target_temp=float(room["targetTemp"]),
            eff_target=float(t["effTarget"]),
            floor_temp=t.get("guardFloor") if t.get("guardFloor") is not None else t.get("effFloor"),
            floor_src=t.get("floorSrc"),
            outdoor_temp=outdoor_temp,
        )
        self.comfort_events = comfort.append_event(self.comfort_events, ev, now=now)
        self.db.set_kv("comfort_events", self.comfort_events)
        _LOGGER.info(
            "[%s] 쾌적 학습 신호(%s): 실내 %s°C (원시 %s + 보정 %+.1f) 실효 목표 %.2f°C 바닥 %s°C(%s)",
            room["roomName"], comfort.SOURCE_LABEL.get(source, source), ev["roomTemp"], ev["roomTempRaw"],
            float(ev["roomOffsetC"] or 0.0), ev["effTarget"], ev["floorTemp"], ev["floorSrc"],
        )

    def comfort_analysis(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(tz=KST)
        rooms = {rm["roomName"]: rm for rm in self.db.list_rooms()}
        return comfort.analyze(
            self.comfort_events, rooms, now=now, floor_min_c=float(self.settings.get("floorMinC") or 0.0)
        )

    # ------------------------------------------------------------------ 실내 센서 보정 (v2.9.1)
    def compute_sensor_offsets(
        self, start: datetime, end: datetime, *, reference: str = "", reference_temp_c: float | None = None
    ) -> dict[str, Any]:
        """센서를 한곳에 모아 둔 구간의 원시값 중앙값으로 방별 보정값을 계산 (적용은 사용자가)."""
        rooms = {rm["roomName"]: rm for rm in self.db.list_rooms()}
        rows = self.db.list_cycles_since(
            start.isoformat(timespec="seconds"), until_iso=end.isoformat(timespec="seconds")
        )
        per: dict[str, dict[str, Any]] = {}
        for c in rows:
            name = c.get("roomName")
            if name not in rooms or c.get("roomTemp") is None:
                continue
            off = float(((c.get("diagnostics") or {}).get("roomTempOffsetC")) or 0.0)
            d = per.setdefault(name, {"raw": [], "reports": set()})
            d["raw"].append(float(c["roomTemp"]) - off)
            if c.get("sensorMeasuredAtRoom"):
                d["reports"].add(str(c["sensorMeasuredAtRoom"]))
        out_rooms: list[dict[str, Any]] = []
        medians: dict[str, float] = {}
        for name in rooms:
            d = per.get(name)
            if not d or not d["raw"]:
                out_rooms.append({"room": name, "status": "NO_DATA", "samples": 0, "reports": 0,
                                  "currentOffsetC": rooms[name].get("roomTempOffsetC", 0.0)})
                continue
            vals = sorted(d["raw"])
            med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2.0
            p10 = vals[int(0.1 * (len(vals) - 1))]
            p90 = vals[int(round(0.9 * (len(vals) - 1)))]
            ok = len(d["reports"]) >= CALIBRATION_MIN_REPORTS
            if ok:
                medians[name] = med
            out_rooms.append({
                "room": name,
                "status": "OK" if ok else "FEW_REPORTS",
                "samples": len(vals),
                "reports": len(d["reports"]),
                "rawMedianC": round(med, 2),
                "spreadC": round(p90 - p10, 2),
                "currentOffsetC": rooms[name].get("roomTempOffsetC", 0.0),
            })
        warnings: list[str] = []
        ref_label: str
        if reference_temp_c is not None:
            ref_val: float | None = float(reference_temp_c)
            ref_label = f"기준 온도계 {reference_temp_c:.1f}°C"
        elif reference:
            if reference not in medians:
                ref_val = None
                warnings.append(f"기준 방 '{reference}' 센서 데이터 부족")
            else:
                ref_val = medians[reference] + float(rooms[reference].get("roomTempOffsetC") or 0.0)
            ref_label = f"{reference} 센서(현재 보정 포함)"
        else:
            ms = sorted(medians.values())
            ref_val = (ms[len(ms) // 2] if len(ms) % 2 else (ms[len(ms) // 2 - 1] + ms[len(ms) // 2]) / 2.0) if ms else None
            ref_label = "전체 센서 중앙값(합의 기준 — 센서 평균이 정확하다는 가정)"
            if len(ms) < 3:
                warnings.append("합의 기준은 센서 3개 이상일 때 의미가 있습니다 (기준 온도계 또는 기준 방 지정 권장)")
        for r in out_rooms:
            if ref_val is not None and r["room"] in medians:
                r["suggestedOffsetC"] = round(max(-ROOM_TEMP_OFFSET_MAX_C, min(ROOM_TEMP_OFFSET_MAX_C, ref_val - medians[r["room"]])), 1)
            else:
                r["suggestedOffsetC"] = None
            if r.get("spreadC") is not None and r["spreadC"] > CALIBRATION_REF_SPREAD_WARN_C:
                warnings.append(
                    f"{r['room']}: 구간 중 변동 {r['spreadC']:.1f}°C — 온도가 안정된 구간(난방 정지, 밤)으로 다시 측정 권장"
                )
            if r["status"] == "FEW_REPORTS":
                warnings.append(f"{r['room']}: 보고 {r['reports']}회 — 최소 {CALIBRATION_MIN_REPORTS}회 필요 (구간 연장)")
        return {
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
            "reference": {"label": ref_label, "valueC": None if ref_val is None else round(ref_val, 2)},
            "rooms": out_rooms,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ Gemini 일일 리포트 (v2.9.1)
    def build_daily_report_context(self, now: datetime | None = None) -> dict[str, Any]:
        """지난 24시간 기록 요약 (결정론적 계산, Gemini 입력 겸 리포트 본문)."""
        now = now or datetime.now(tz=KST)
        since_dt = now - timedelta(hours=24)
        rows = self.db.stats_rows_since(since_dt.isoformat(timespec="seconds"))
        rooms = self.db.list_rooms()
        first = _parse_ts(rows[0]["executionStartedAt"]) if rows else None
        floors = [r["floorTemp"] for r in rows if r.get("floorTemp") is not None and r.get("floorSrc") == "MEASURED"]
        outs = [r["outdoorTemp"] for r in rows if r.get("outdoorTemp") is not None]
        recent = comfort.recent_events(self.comfort_events, since=since_dt)
        analysis = self.comfort_analysis(now)
        return {
            "periodStart": since_dt.isoformat(timespec="minutes"),
            "periodEnd": now.isoformat(timespec="minutes"),
            "coverageHours": round((now - first).total_seconds() / 3600.0, 1) if first else 0.0,
            "kpis": compute_daily_kpis(rows),
            "boiler": compute_boiler_stats(
                rows, interval_sec=self.settings["updateIntervalSec"], gas_krw_per_hour=self.settings["gasKrwPerHour"]
            ),
            "rooms": compute_room_daily_stats(rows, interval_sec=self.settings["updateIntervalSec"]),
            "floorRangeC": [round(min(floors), 1), round(max(floors), 1)] if floors else None,
            "outdoorRangeC": [round(min(outs), 1), round(max(outs), 1)] if outs else None,
            "userHeatRequests": [
                {
                    "at": str(e.get("at"))[11:16],
                    "room": e.get("room"),
                    "source": comfort.SOURCE_LABEL.get(str(e.get("source")), e.get("source")),
                    "roomTemp": e.get("roomTemp"),
                    "effTarget": e.get("effTarget"),
                    "floorTemp": e.get("floorTemp"),
                }
                for e in recent
            ],
            "comfortSuggestions": analysis["suggestions"],
            "roomConfig": {
                rm["roomName"]: {
                    "targetTemp": rm["targetTemp"],
                    "mode": rm["controlMode"],
                    "sensorOffsetC": rm.get("roomTempOffsetC", 0.0),
                }
                for rm in rooms
            },
            "settings": {
                k: self.settings.get(k)
                for k in ("floorMinC", "floorOffLimitC", "externalBoostMin", "manualHeatMaxMin", "maxRunCapMin",
                          "cooldownMin", "dryRun")
            },
        }

    def daily_report_due(self, now: datetime) -> bool:
        if not self.settings.get("dailyReportEnabled", True):
            return False
        hour = int(self.settings.get("dailyReportHour", DAILY_REPORT_DEFAULT_HOUR))
        if (now.hour - hour) % 24 >= DAILY_REPORT_SEND_WINDOW_H:
            return False
        last = self.db.get_kv("daily_report_last") or {}
        return last.get("date") != now.strftime("%Y-%m-%d")

    async def create_daily_report(self, now: datetime | None = None, *, send: bool = False) -> dict[str, Any]:
        """24시간 요약 + Gemini 분석(키 있을 때) 생성, send=True 면 알림 발송."""
        now = now or datetime.now(tz=KST)
        ctx = self.build_daily_report_context(now)
        ai = await asyncio.to_thread(self.advisor.generate_daily_report, ctx)
        text = format_daily_report_text(ctx, ai.get("report"))[:DAILY_REPORT_MAX_CHARS]
        result = {"kpis": ctx["kpis"], "report": ai.get("report"), "status": ai.get("status"), "text": text,
                  "context": ctx}
        if send:
            await self._safe_notify(f"📊 I.T.S.S. 일일 리포트 ({now.strftime('%m/%d')})", text)
            result["sent"] = True
        _LOGGER.info("일일 리포트 생성 (Gemini=%s, 발송=%s, %d자)", ai.get("status"), send, len(text))
        return result

    def maybe_start_daily_report(self, now: datetime | None = None) -> bool:
        """발송 시각이면 백그라운드로 리포트 발송 (제어 루프를 막지 않음, 하루 1회)."""
        now = now or datetime.now(tz=KST)
        if self._report_task is not None and not self._report_task.done():
            return False
        if not self.daily_report_due(now):
            return False
        # 실패해도 같은 날 반복 발송하지 않도록 먼저 기록
        self.db.set_kv("daily_report_last", {"date": now.strftime("%Y-%m-%d"), "startedAt": now.isoformat(timespec="seconds")})

        async def _run() -> None:
            try:
                await self.create_daily_report(now, send=True)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("일일 리포트 생성/발송 실패")

        self._report_task = asyncio.create_task(_run())
        return True

    # ------------------------------------------------------------------ 공통 유틸
    @property
    def floor_configured(self) -> bool:
        return bool(str(self.settings.get("masterFloorEntity") or "").strip())

    def _track(self, entity_id: str) -> dict[str, Any]:
        return self.sensor_tracks.setdefault(entity_id, st.new_track())

    async def _read_numeric(self, entity_id: str) -> tuple[float | None, str | None, float | None, str | None]:
        """(값, 보고 시각, 경과초, 시각 출처). 구형/가짜 클라이언트는 get_numeric_state 로 폴백."""
        if not entity_id:
            return None, None, None, None
        reader = getattr(self.ha, "get_numeric_reading", None)
        if reader is not None:
            return await reader(entity_id)
        val, ts, age = await self.ha.get_numeric_state(entity_id)
        return val, ts, age, (None if ts is None else "unknown")

    def _observe_sensor(
        self,
        entity_id: str,
        role: str,
        value: float | None,
        report_ts: str | None,
        ts_src: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        """센서 판독값을 트랙에 반영하고 보고 주기/누락 변화를 로그로 남긴다."""
        track = self._track(entity_id)
        before_typ = st.typical_interval_sec(track)
        before_stale = st.stale_threshold_sec(track)
        if value is None:
            if not track.get("missingSince"):
                track["missingSince"] = now.isoformat(timespec="seconds")
                _LOG_SENSOR.warning("%s 센서 값 누락/unavailable: %s", role, entity_id)
            return track
        if track.get("missingSince"):
            _LOG_SENSOR.info("%s 센서 값 복구: %s=%s (누락 시작 %s)", role, entity_id, value, track["missingSince"])
            track.pop("missingSince", None)

        if st.observe(track, value, report_ts, now, ts_src):
            ivs = track.get("intervals") or []
            _LOG_SENSOR.debug(
                "새 보고 %s %s=%.1f @%s 간격=%s 출처=%s",
                role, entity_id, value, track.get("lastReportAt"), f"{ivs[-1]:.0f}s" if ivs else "-", track.get("tsSource"),
            )
            after_typ = st.typical_interval_sec(track)
            after_stale = st.stale_threshold_sec(track)
            if after_typ is not None and (before_typ is None or abs(after_typ - before_typ) > 0.3 * before_typ):
                _LOG_SENSOR.info(
                    "%s 센서 보고 주기 %s: %s -> 중앙값 %.0fs, 최근 최대 %.0fs (표본 %d, 출처=%s)",
                    role, "학습" if before_typ is None else "변화", entity_id, after_typ,
                    st.max_interval_sec(track) or 0.0, len(ivs), track.get("tsSource"),
                )
            if after_stale != before_stale:
                _LOG_SENSOR.info(
                    "%s 센서 stale 임계 변경: %s %.0fs -> %.0fs", role, entity_id, before_stale, after_stale
                )
        return track

    async def _safe_notify(self, title: str, message: str) -> None:
        try:
            await self.notifier.send(title, message)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("알림 전송 실패: %s", title)

    # ------------------------------------------------------------------ 학습/이력
    def refresh_lag_profiles(self) -> dict[str, dict[str, Any]]:
        """Learn per-room floor/room rise dead-times and post-OFF residual peaks (최근 48시간)."""
        since = (datetime.now(tz=KST) - timedelta(hours=48)).isoformat(timespec="seconds")
        cycles = self.db.list_cycles_since(since)
        base_cd = int(self.settings["cooldownMin"])
        profiles: dict[str, dict[str, Any]] = {}
        for rm in self.db.list_rooms():
            r_name = rm["roomName"]
            prof = learn_room_lag_profile(r_name, cycles, base_cooldown_min=base_cd)
            profiles[r_name] = prof.to_dict()
        self.lag_profiles = profiles
        self.db.set_kv("thermal_lag_profiles", profiles)
        return profiles

    def prune_history(self) -> int:
        """보존 기간(historyRetentionDays) 이전 회차 이력 삭제."""
        days = int(self.settings.get("historyRetentionDays", 30))
        before = (datetime.now(tz=KST) - timedelta(days=days)).isoformat(timespec="seconds")
        removed = self.db.prune_cycles(before)
        self.loop_state["lastPruneAt"] = now_kst_iso()
        self.loop_state["lastPrunedRows"] = removed
        if removed:
            _LOGGER.info("회차 이력 보존 정책 적용: %d일 이전 %d건 삭제", days, removed)
        self.trace.prune()
        return removed

    def _restore_cooldowns_from_history(self) -> None:
        """Guarantee residual-heat cooldown survives add-on restarts."""
        now = datetime.now(tz=KST)
        changed = False
        for rm in self.db.list_rooms():
            r_name = rm["roomName"]
            tm = self.room_timers.setdefault(r_name, {"heatingStartedAt": None, "cooldownUntil": None})
            existing_cd = _parse_ts(tm.get("cooldownUntil"))
            if existing_cd is not None and existing_cd > now:
                continue

            recent = self.db.list_cycles(room_name=r_name, limit=60)
            if not recent:
                continue

            prof = self.lag_profiles.get(r_name) or {}
            cd_min = int(prof.get("recommendedCooldownMin", self.settings["cooldownMin"]))
            latest = recent[0]
            latest_on = (
                latest.get("combustionConfirmed") is True
                or (latest.get("combustionConfirmed") is None and latest.get("commandAction") == "HEAT")
            )
            if latest_on:
                continue

            off_boundary_ts: datetime | None = None
            for idx in range(len(recent) - 1):
                curr_c = recent[idx]
                prev_c = recent[idx + 1]
                prev_on = (
                    prev_c.get("combustionConfirmed") is True
                    or (prev_c.get("combustionConfirmed") is None and prev_c.get("commandAction") == "HEAT")
                )
                curr_off = curr_c.get("commandAction") == "OFF" or curr_c.get("safetyReason") in COOLDOWN_REASONS
                if prev_on and curr_off and curr_c.get("executionStartedAt"):
                    off_boundary_ts = _parse_ts(curr_c["executionStartedAt"])
                    break

            if off_boundary_ts is not None:
                until = off_boundary_ts + timedelta(minutes=cd_min)
                if until > now:
                    tm["cooldownUntil"] = until.isoformat(timespec="seconds")
                    tm["heatingStartedAt"] = None
                    changed = True

        if changed:
            self.db.set_kv("room_timers", self.room_timers)

    def apply_preset(self, preset: str) -> dict[str, Any]:
        """Apply one-touch preset (AUTO_ALL / AWAY / SLEEP / PREHEAT / VENTILATION / ALL_OFF)."""
        now = datetime.now(tz=KST)
        self.active_preset = "AUTO_ALL" if preset == "VENTILATION" else preset
        self.db.set_kv("active_preset", self.active_preset)
        rooms = self.db.list_rooms()
        hold_min = int(self.settings["ventilationHoldMin"])
        for rm in rooms:
            r_name = rm["roomName"]
            tm = self.room_timers.setdefault(r_name, {"heatingStartedAt": None, "cooldownUntil": None})
            if preset == "VENTILATION":
                self.db.update_room_control(r_name, control_mode="AUTO", manual_action="HOLD")
                tm["ventilationUntil"] = (now + timedelta(minutes=hold_min)).isoformat(timespec="seconds")
                tm["heatingStartedAt"] = None
            elif preset == "ALL_OFF":
                self.db.update_room_control(r_name, control_mode="MANUAL", manual_action="OFF")
            elif preset == "AUTO_ALL":
                self.db.update_room_control(r_name, control_mode="AUTO", manual_action="HOLD")
                tm["ventilationUntil"] = None
            else:
                self.db.update_room_control(r_name, control_mode="AUTO", manual_action="HOLD")
        self.db.set_kv("room_timers", self.room_timers)
        return {
            "activePreset": preset,
            "offsetC": PRESET_OFFSETS.get(preset, 0.0),
            "ventilationHoldMin": hold_min if preset == "VENTILATION" else 0,
        }

    def calibrate_rc_and_offsets(self) -> dict[str, Any]:
        """RC 파라미터 피팅(마스터 방 실측 바닥, 실제 시각 간격·회차별 외기 사용) + 슬레이브 offset 제안.

        슬레이브 방 바닥 offset 은 실내 공기 온도 차이로부터의 '참고 제안값'만 반환하며
        자동으로 덮어쓰지 않는다 (바닥 안전 상한 판정에 직접 쓰이는 값이므로).
        """
        now = datetime.now(tz=KST)
        rooms = self.db.list_rooms()
        master = next((r for r in rooms if r["hasFloorSensor"]), None)
        nominal_sec = float(self.settings["updateIntervalSec"])
        skipped = {"ventilation": 0, "missingValue": 0, "dtOutOfRange": 0, "outdoorFallback": 0}
        observations: list[RCObservation] = []
        if master is not None:
            since = (now - timedelta(hours=48)).isoformat(timespec="seconds")
            rows = self.db.list_cycles_since(since, room_name=master["roomName"])[-2881:]
            for prev, curr in zip(rows, rows[1:]):
                reasons = (str(prev.get("safetyReason") or ""), str(curr.get("safetyReason") or ""))
                if any(r.startswith(("P000_VENTILATION", "P000_WINDOW_OPEN")) for r in reasons):
                    skipped["ventilation"] += 1
                    continue
                if None in (prev.get("floorTemp"), prev.get("roomTemp"), curr.get("floorTemp"), curr.get("roomTemp")):
                    skipped["missingValue"] += 1
                    continue
                t0, t1 = _parse_ts(prev.get("executionStartedAt")), _parse_ts(curr.get("executionStartedAt"))
                if t0 is None or t1 is None:
                    skipped["missingValue"] += 1
                    continue
                dt = (t1 - t0).total_seconds()
                if not (0.5 * nominal_sec <= dt <= 2.5 * nominal_sec):
                    skipped["dtOutOfRange"] += 1
                    continue
                outdoor = _to_float(prev.get("outdoorTemp"))
                if outdoor is None:
                    outdoor = 5.0
                    skipped["outdoorFallback"] += 1
                observations.append(
                    RCObservation(
                        dt_sec=dt,
                        floor_temp=float(prev["floorTemp"]),
                        room_temp=float(prev["roomTemp"]),
                        outdoor_temp=outdoor,
                        heating_on=_row_is_boiler_on(prev),
                        next_floor_temp=float(curr["floorTemp"]),
                        next_room_temp=float(curr["roomTemp"]),
                    )
                )

        rc_result: dict[str, Any] = dict(fit_rc_parameters(observations))
        rc_result["room"] = master["roomName"] if master else None
        rc_result["skipped"] = skipped
        if rc_result.get("status") == "READY":
            self.db.set_kv("fitted_rc_params", rc_result)

        # 슬레이브 방 바닥 offset 참고 제안값 (최근 24시간 실내 평균 차이, ±2.0°C 제한)
        since24 = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        rows24 = self.db.stats_rows_since(since24)
        by_room: dict[str, list[float]] = {}
        for c in rows24:
            if c.get("roomTemp") is not None:
                by_room.setdefault(c["roomName"], []).append(float(c["roomTemp"]))
        suggested: dict[str, float] = {}
        master_temps = by_room.get(master["roomName"], []) if master else []
        if master_temps:
            master_mean = sum(master_temps) / len(master_temps)
            for rm in rooms:
                if rm["hasFloorSensor"]:
                    continue
                temps = by_room.get(rm["roomName"], [])
                if len(temps) >= 3:
                    suggested[rm["roomName"]] = max(-2.0, min(2.0, round(sum(temps) / len(temps) - master_mean, 1)))

        # UI 에서 방별 '제안값 적용' 버튼으로 쓰도록 보관 (자동 적용 안 함)
        self.db.set_kv(
            "suggested_floor_offsets",
            {"computedAt": now.isoformat(timespec="seconds"), "offsets": suggested},
        )
        lag_profiles = self.refresh_lag_profiles()
        return {
            "rcFit": rc_result,
            "suggestedFloorOffsets": suggested,
            "floorOffsetsApplied": False,
            "thermalLagProfiles": lag_profiles,
        }

    async def _read_outdoor_weather(self) -> tuple[float | None, float | None, str]:
        """외기 판독 + 소스가 바뀔 때만 INFO 로그 (HA센서 -> API 폴백 전환 추적)."""
        temp, wind, label = await self._read_outdoor_weather_raw()
        base_label = label.split("·")[0]
        if base_label != self._last_weather_label:
            _LOG_WEATHER.info("외기 소스 변경: %s -> %s (외기=%s°C, 풍속=%s m/s)", self._last_weather_label, label, temp, wind)
            self._last_weather_label = base_label
        _LOG_WEATHER.debug("외기 %s°C 풍속 %s m/s (%s)", temp, wind, label)
        return temp, wind, label

    async def _read_outdoor_weather_raw(self) -> tuple[float | None, float | None, str]:
        """Read outdoor temp/wind from HA sensor (e.g., Naver Weather), else fallback to API."""
        ent = str(self.settings.get("outdoorTempEntity") or "").strip()
        if ent:
            raw = await self.ha.get_state(ent)
            if raw and raw.get("state") not in (None, "unknown", "unavailable"):
                if ent.startswith("weather."):
                    attrs = raw.get("attributes") or {}
                    t = attrs.get("temperature")
                    w = attrs.get("wind_speed")
                    if t is not None:
                        try:
                            return round(float(t), 1), (float(w) if w is not None else None), "HA센서"
                        except (TypeError, ValueError):
                            pass
                else:
                    val, _, _, _ = await self._read_numeric(ent)
                    if val is not None:
                        return val, None, "HA센서"

        if self.weather_client is not None:
            w = await asyncio.to_thread(self.weather_client.get_weather)
            if w.outdoor_temp_c is not None:
                loc = self.settings.get("weatherLocation", "서울")
                label = "Gemini API" if w.source == "GEMINI_SEARCH_GROUNDING" else "Open-Meteo API"
                return w.outdoor_temp_c, w.wind_speed_ms, f"{label}·{loc}"
        return None, None, "시간대폴백"

    def _timer_metrics(self, room_name: str, now: datetime) -> tuple[float, float, float]:
        """Return (continuous_run_min, cooldown_remaining_min, ventilation_remaining_min) for one room."""
        tm = self.room_timers.get(room_name, {})
        run_min = cd_min = vent_min = 0.0
        started = _parse_ts(tm.get("heatingStartedAt"))
        if started is not None:
            run_min = max(0.0, (now - started).total_seconds() / 60.0)
        cd_until = _parse_ts(tm.get("cooldownUntil"))
        if cd_until is not None:
            cd_min = max(0.0, (cd_until - now).total_seconds() / 60.0)
        vent_until = _parse_ts(tm.get("ventilationUntil"))
        if vent_until is not None:
            vent_min = max(0.0, (vent_until - now).total_seconds() / 60.0)
        return round(run_min, 1), round(cd_min, 1), round(vent_min, 1)

    def _compute_effective_floor_limit(self, eff_target: float, outdoor_temp: float | None) -> float:
        """Compute target-linked dynamic floor ceiling bounded by configured floorOffLimitC."""
        configured = float(self.settings["floorOffLimitC"])
        if outdoor_temp is not None and outdoor_temp <= -5.0:
            margin = 4.5
        elif outdoor_temp is not None and outdoor_temp > 5.0:
            margin = 3.0
        else:
            margin = 3.5
        return round(min(configured, eff_target + margin), 1)

    def floor_cap_c(self, eff_floor_limit: float) -> float:
        """바닥 상한 = min(설정 바닥 OFF 상한, 목표 연동 동적 상한) — sentinel 4a/4b 와 동일 기준."""
        return min(float(self.settings["floorOffLimitC"]), eff_floor_limit)

    @staticmethod
    def _compute_floor_limited_max_run(
        *,
        base_max_run_min: float,
        start_floor_c: float | None,
        floor_cap_c: float,
        rise_rate_c_per_min: float,
    ) -> float:
        """바닥 상승률 기반 1회 최대 가동 (v2.9.0).

        OFF 후 예측 피크 = 가동 시작 바닥 + rate x 가동시간 이 (상한 - 여유) 에 닿는 시점까지.
        sentinel 4b(가동 중 예측 차단)·시작 전 가드와 같은 식이므로, 결과가 MIN_PULSE_MIN 미만이면
        시작 자체가 차단된다 (1분 펄스 방지).
        """
        if start_floor_c is None or rise_rate_c_per_min <= 0:
            return base_max_run_min
        headroom = floor_cap_c - FLOOR_PEAK_MARGIN_C - start_floor_c
        limit = max(0.0, headroom / rise_rate_c_per_min)
        return round(min(base_max_run_min, limit), 1)

    @staticmethod
    def _room_floor(
        rm: dict[str, Any],
        master_floor: float | None,
        floor_configured: bool,
    ) -> tuple[float | None, str, float | None]:
        """방 바닥값 결정 -> (제어/표시용 바닥, 출처, 안전 판정용 바닥).

        - MEASURED     : 바닥 센서 방 = 마스터 실측 (동시 제어 그룹의 기준 방)
        - ESTIMATED    : 바닥 센서 없는 방 + 바닥 기준 공유 ON = 마스터 실측 + offset
                         (v2.9.0: 바닥 센서 방과 동시 제어 그룹으로 묶여 같은 시점에 ON/OFF)
        - ESTIMATE_OFF : 바닥 센서 없는 방 + 바닥 기준 공유 OFF = 독립 제어 방.
                         바닥값·안전 판정용 바닥 모두 없음 (v2.8 의 마스터 대리 판정 폐기).
                         실내 과열·최대 가동·잔열 휴지·창문/환기로 보호. 마스터 센서 미설정/누락과 무관.
        - MISSING / UNCONFIGURED : 마스터 바닥 미수신 / 미설정
        """
        if not rm["hasFloorSensor"] and not bool(rm.get("floorEstimateEnabled", True)):
            return None, "ESTIMATE_OFF", None
        if not floor_configured:
            return None, "UNCONFIGURED", None
        if master_floor is None:
            return None, "MISSING", None
        if rm["hasFloorSensor"]:
            v = round(master_floor, 1)
            return v, "MEASURED", v
        v = round(master_floor + float(rm["floorOffsetC"]), 1)
        return v, "ESTIMATED", v

    @staticmethod
    def _floor_group_plan(rooms: list[dict[str, Any]], telemetry: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """바닥 기준을 공유하는 AUTO 방(실측 + 추정)을 동시 제어 그룹으로 묶는다.

        기준 방(leader) = 바닥 센서 방(없으면 첫 추정 방). 기준 방이 먼저 판정하고
        추종 방(follower)은 같은 명령을 따른다. 추종 방은 기준 방이 가열할 때만 가열할 수 있다
        (바닥 센서가 보지 못하는 가열 금지).
        """
        members = [
            rm for rm in rooms
            if rm["controlMode"] == "AUTO"
            and telemetry[rm["roomName"]]["floorSrc"] in ("MEASURED", "ESTIMATED")
            # 단독 외부 조작 가동 방은 그룹에서 제외 (바닥 공유 방 연동 외부 조작은 그룹으로 함께 가동)
            and not (
                telemetry[rm["roomName"]].get("externalBoost")
                and not telemetry[rm["roomName"]].get("externalBoostGroup")
            )
        ]
        if not members:
            return None
        leader = next((rm for rm in members if rm["hasFloorSensor"]), members[0])
        return {
            "leader": leader["roomName"],
            "members": [rm["roomName"] for rm in members],
            "result": None,
        }

    def _build_predictor(self) -> ThermalPredictor:
        """Instantiate the configured P200 predictor (Linear v1.2 or Adaptive RC)."""
        if self.settings.get("predictorModel") == MODEL_ADAPTIVE_RC:
            fitted = self.db.get_kv("fitted_rc_params") or {}
            if fitted.get("status") == "READY":
                params = RCParameters(
                    c_floor=float(fitted.get("c_floor", 1800.0)),
                    c_room=float(fitted.get("c_room", 450.0)),
                    r_floor_room=float(fitted.get("r_floor_room", 2.2)),
                    r_room_ambient=float(fitted.get("r_room_ambient", 12.0)),
                    q_heat_kw=float(fitted.get("q_heat_kw", 1.8)),
                )
                return AdaptiveRCModel(params=params)
            return AdaptiveRCModel()
        return LinearExtrapolationV12()

    # ------------------------------------------------------------------ 조절기 명령
    def compute_command_setpoint(self, action: str, climate_state: dict[str, Any] | None) -> float:
        """HEAT/OFF 스윙 설정온도.

        - HEAT: 기본 heatOnSetpoint. 조절기 현재온도+0.5 가 이를 넘으면 0.5 단위 올림으로 상향
                (최대 +3.0°C, 조절기 max_temp 이내) -> 목표 > 24°C 에서도 밸브 개방 보장
        - OFF : 기본 heatOffSetpoint. 조절기 현재온도-1.0 보다 높으면 내림으로 하향
                (조절기 min_temp 이상) -> 추운 방에서 OFF 명령이 오히려 가열하는 문제 방지
        """
        attrs = (climate_state or {}).get("attributes") or {}
        step = _to_float(attrs.get("target_temp_step")) or THERMOSTAT_SETPOINT_STEP
        cur = _to_float(attrs.get("current_temperature"))
        min_t = _to_float(attrs.get("min_temp"))
        max_t = _to_float(attrs.get("max_temp"))
        on_sp = float(self.settings["heatOnSetpoint"])
        off_sp = float(self.settings["heatOffSetpoint"])
        if action == "HEAT":
            sp = on_sp
            if cur is not None and cur + 0.5 > on_sp:
                sp = min(on_sp + HEAT_SETPOINT_MAX_BOOST_C, round_thermostat_setpoint(cur + 1.0, action="HEAT", step=step))
            if max_t is not None:
                sp = min(sp, max_t)
        else:
            sp = off_sp
            if cur is not None:
                sp = min(sp, round_thermostat_setpoint(cur - 1.0, action="OFF", step=step))
            if min_t is not None:
                sp = max(sp, min_t)
        return round(sp, 1)

    def _dispatch_needed(
        self,
        action: str,
        climate_state: dict[str, Any] | None,
        setpoint: float,
        tm: dict[str, Any],
        now: datetime,
    ) -> bool:
        """조절기 상태가 이미 목표와 같으면 RS485 버스 부하를 줄이기 위해 생략 (30분마다 재동기화)."""
        if not climate_state:
            return True
        mode = str(climate_state.get("state") or "").lower()
        if mode in ("", "unknown", "unavailable"):
            return True
        sp = _to_float((climate_state.get("attributes") or {}).get("temperature"))
        if action == "HEAT":
            in_target = mode == "heat" and sp is not None and sp >= setpoint - 0.05
        else:
            in_target = mode == "off" or (sp is not None and sp <= setpoint + 0.05)
        if not in_target:
            return True
        last = _parse_ts(tm.get("lastDispatchAt"))
        if last is None or tm.get("lastDispatchAction") != action:
            # 이미 목표 상태: 재동기화 기준 시각만 기록
            tm["lastDispatchAt"] = now.isoformat(timespec="seconds")
            tm["lastDispatchAction"] = action
            return False
        return (now - last) >= timedelta(minutes=DISPATCH_RESEND_MIN)

    async def _dispatch(
        self,
        climate_entity: str,
        record: CycleRecord,
        climate_state: dict[str, Any] | None,
        tm: dict[str, Any],
        now: datetime,
    ) -> None:
        """Issue climate command to HA (RS485/Kocom-safe sequencing) and re-read."""
        record.control_issued_at = now_kst_iso()
        if self.settings["dryRun"] or not self.ha.is_configured:
            record.dispatch_result = "DRY_RUN"
            record.control_confirmed_at = now_kst_iso()
            record.observed_at = now_kst_iso()
            _LOG_DISPATCH.debug("DRY_RUN %s %s sp=%s (미전송)", climate_entity, record.command_action, record.command_setpoint)
            return
        if not self._dispatch_needed(record.command_action or "", climate_state, record.command_setpoint or 0.0, tm, now):
            record.dispatch_result = "NO_CHANGE"
            record.control_issued_at = None
            c_attrs = (climate_state or {}).get("attributes") or {}
            _LOG_DISPATCH.debug(
                "NO_CHANGE %s %s sp=%s (현재 state=%s sp=%s, 마지막 전송 %s)",
                climate_entity, record.command_action, record.command_setpoint,
                (climate_state or {}).get("state"), c_attrs.get("temperature"), tm.get("lastDispatchAt"),
            )
            return

        attrs = (climate_state or {}).get("attributes") or {}
        calls: list[str] = []
        record.diagnostics["dispatchCalls"] = calls
        t_disp = time.perf_counter()
        try:
            if record.command_action == "HEAT":
                if record.device_mode_event != "heat":
                    try:
                        calls.append("climate.set_hvac_mode(heat)")
                        await self.ha.call_service(
                            "climate", "set_hvac_mode", {"entity_id": climate_entity, "hvac_mode": "heat"}
                        )
                        await asyncio.sleep(0.8)  # RS485 월패드 명령 충돌 방지
                    except Exception as err:  # noqa: BLE001
                        calls[-1] += f"=ERR:{err}"
                        _LOG_DISPATCH.debug("set_hvac_mode(heat) 미지원/실패 (%s): %s", climate_entity, err)
                calls.append(f"climate.set_temperature({record.command_setpoint})")
                await self.ha.call_service(
                    "climate", "set_temperature", {"entity_id": climate_entity, "temperature": record.command_setpoint}
                )
            elif record.command_action == "OFF":
                cur = _to_float(attrs.get("current_temperature"))
                hvac_modes = [str(m).lower() for m in (attrs.get("hvac_modes") or [])]
                # 설정 가능한 최저 온도로도 현재온도 아래로 못 내리면 모드 OFF 사용
                use_mode_off = (
                    cur is not None
                    and record.command_setpoint is not None
                    and record.command_setpoint >= cur - 0.4
                    and "off" in hvac_modes
                    and record.device_mode_event != "off"
                )
                if use_mode_off:
                    calls.append("climate.set_hvac_mode(off)")
                    await self.ha.call_service(
                        "climate", "set_hvac_mode", {"entity_id": climate_entity, "hvac_mode": "off"}
                    )
                else:
                    try:
                        calls.append(f"climate.set_temperature({record.command_setpoint})")
                        await self.ha.call_service(
                            "climate",
                            "set_temperature",
                            {"entity_id": climate_entity, "temperature": record.command_setpoint},
                        )
                    except Exception as err:  # noqa: BLE001
                        calls[-1] += f"=ERR:{err}"
                        if record.device_mode_event != "off":
                            calls.append("climate.set_hvac_mode(off)")
                            await self.ha.call_service(
                                "climate", "set_hvac_mode", {"entity_id": climate_entity, "hvac_mode": "off"}
                            )
                        else:
                            raise
            record.control_confirmed_at = now_kst_iso()
            tm["lastDispatchAt"] = now.isoformat(timespec="seconds")
            tm["lastDispatchAction"] = record.command_action

            await asyncio.sleep(DEFAULT_RECHECK_DELAY_SEC)
            recheck = await self.ha.get_state(climate_entity)
            record.observed_at = now_kst_iso()
            if recheck:
                record.device_mode_event = recheck.get("state")
                record.combustion_confirmed = detect_boiler_running(recheck)
            record.dispatch_result = "SENT"
            r_attrs = (recheck or {}).get("attributes") or {}
            _LOG_DISPATCH.info(
                "SENT %s %s sp=%s [%s] -> 재조회 state=%s sp=%s cur=%s 가동추정=%s (%.1fs)",
                climate_entity, record.command_action, record.command_setpoint, ", ".join(calls),
                record.device_mode_event, r_attrs.get("temperature"), r_attrs.get("current_temperature"),
                record.combustion_confirmed, time.perf_counter() - t_disp,
            )
        except Exception as err:  # noqa: BLE001
            self.ha.last_error = f"Climate dispatch failed ({climate_entity}): {err}"
            _LOG_DISPATCH.error(
                "FAILED %s %s sp=%s [%s]: %s", climate_entity, record.command_action, record.command_setpoint,
                ", ".join(calls), err,
            )
            record.execution_status = "FAILED"
            record.dispatch_result = "FAILED"
            record.observed_at = now_kst_iso()

    # ------------------------------------------------------------------ 알림
    async def _notify_state_change(
        self,
        *,
        room_name: str,
        action: str,
        reason_code: str,
        room_temp: float | None,
        floor_temp: float | None,
        target_temp: float,
        pred_room: float | None,
        setpoint: float | None,
        run_min: float,
        max_run_min: float,
        dispatch_result: str | None,
        now: datetime,
        eff_target: float | None = None,
        floor_label: str | None = None,
        extra_lines: list[str] | None = None,
    ) -> None:
        """보일러 ON/OFF 결정 변화·치명 사유·명령 실패 시 Telegram/HA 푸시.

        v2.9.0: 바닥 없는 독립 방은 'None°C' 대신 '미사용(독립 제어)' 표기, 실효 목표 병기,
        동시 제어 그룹은 기준 방/그룹 평균 편차를 extra_lines 로 표기.
        """
        if not self.settings.get("notifyEvents", True):
            return
        tm = self.room_timers.setdefault(room_name, {})
        dry_tag = " [시험운전]" if self.settings.get("dryRun") else ""
        floor_str = floor_label if floor_label is not None else (
            f"{floor_temp}°C" if floor_temp is not None else "--"
        )
        target_str = f"{target_temp}°C" + (
            f" (실효 {eff_target}°C)" if eff_target is not None and abs(eff_target - target_temp) >= 0.01 else ""
        )

        if dispatch_result == "FAILED":
            # 명령 실패는 '켜짐/꺼짐' 대신 실패 경보 (30분 내 중복 억제), 성공 시 정상 알림이 다시 나가도록 상태 미갱신
            last_fail = _parse_ts(tm.get("lastFailNotifiedAt"))
            if last_fail is None or (now - last_fail) >= timedelta(minutes=30):
                tm["lastFailNotifiedAt"] = now.isoformat(timespec="seconds")
                await self._safe_notify(
                    f"⚠ I.T.S.S. 조절기 명령 실패 ({room_name}){dry_tag}",
                    f"명령: {action} ({setpoint}°C)\n오류: {getattr(self.ha, 'last_error', None)}\n"
                    f"실내: {room_temp}°C / 바닥: {floor_str}",
                )
            return

        prev_action = tm.get("lastNotifiedAction")
        prev_reason = tm.get("lastNotifiedReason")
        is_critical = reason_code in CRITICAL_REASONS
        if action == prev_action and (not is_critical or reason_code == prev_reason):
            return

        tm["lastNotifiedAction"] = action
        tm["lastNotifiedReason"] = reason_code
        reason_ko = REASON_KO.get(reason_code, reason_code)

        if action == "HEAT":
            title = f"🔥 I.T.S.S. 보일러 켜짐 ({room_name}){dry_tag}"
            lines = [
                f"이유: {reason_ko}",
                f"실내: {room_temp}°C / 바닥: {floor_str} / 목표: {target_str}",
            ]
            lines.extend(extra_lines or [])
            if pred_room is not None:
                lines.append(f"30분 예측 실내: {pred_room}°C")
            lines.append(f"조절기 설정: {setpoint}°C · 이번 최대 가동: {max_run_min}분")
        else:
            title = f"❄️ I.T.S.S. 보일러 꺼짐 ({room_name}){dry_tag}"
            lines = [
                f"이유: {reason_ko}",
                f"실내: {room_temp}°C / 바닥: {floor_str} / 목표: {target_str}",
            ]
            lines.extend(extra_lines or [])
            if pred_room is not None:
                lines.append(f"30분 예측 실내: {pred_room}°C")
            if run_min > 0:
                lines.append(f"이번 가동 시간: {run_min}분 · 조절기 설정: {setpoint}°C")
            else:
                lines.append(f"조절기 설정: {setpoint}°C")

        await self._safe_notify(title, "\n".join(lines))

    def _update_underheat(
        self,
        tm: dict[str, Any],
        *,
        is_auto: bool,
        room_temp: float | None,
        eff_target: float,
        reason: str,
        now: datetime,
    ) -> list[tuple[str, int]] | None:
        """목표 대비 0.3°C 이상 미달이 90분 지속되면 1회 경보용 차단 사유 상위 목록 반환."""
        if not is_auto or room_temp is None:
            for k in ("underheatSince", "underheatReasons", "underheatNotified"):
                tm.pop(k, None)
            return None
        if room_temp < eff_target - UNDERHEAT_MARGIN_C:
            since = _parse_ts(tm.get("underheatSince"))
            if since is None:
                tm["underheatSince"] = now.isoformat(timespec="seconds")
                tm["underheatReasons"] = {}
                since = now
            reasons = tm.setdefault("underheatReasons", {})
            reasons[reason] = int(reasons.get(reason, 0)) + 1
            elapsed = (now - since).total_seconds() / 60.0
            if elapsed >= UNDERHEAT_ALERT_MIN and not tm.get("underheatNotified"):
                tm["underheatNotified"] = True
                return sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
        elif room_temp >= eff_target - DEFAULT_CONTROL_BAND_HALF:
            for k in ("underheatSince", "underheatReasons", "underheatNotified"):
                tm.pop(k, None)
        return None

    @staticmethod
    def _clear_external_boost(tm: dict[str, Any]) -> None:
        tm.pop("externalBoostUntil", None)
        tm.pop("externalBoostStartedAt", None)
        tm.pop("externalBoostGroup", None)

    def _update_external_boost(
        self, rm: dict[str, Any], tm: dict[str, Any], *, is_heating: bool, now: datetime
    ) -> str | None:
        """v2.9.1 외부 조작(월패드/다른 HA 자동화)으로 켜진 난방 감지와 유지 상태.

        감지 (AUTO 방): 직전 회차 관측 비가동 -> 이번 관측 가동 이면서 ITSS 마지막 명령이 HEAT 가 아님.
        ITSS 의 OFF 명령이 조절기에 반영되지 않은 경우는 직전 관측도 '가동' 이므로 외부 조작으로 오인하지 않는다.
        반환: START(감지) / ACTIVE(유지 중) / END(시간 종료) / CANCELLED(외부에서 끔) / None
        """
        boost_min = int(self.settings.get("externalBoostMin", EXTERNAL_BOOST_DEFAULT_MIN) or 0)
        until = _parse_ts(tm.get("externalBoostUntil"))
        if rm["controlMode"] != "AUTO" or boost_min <= 0:
            if until is not None:
                self._clear_external_boost(tm)
            return None
        if until is not None:
            if now >= until:
                return "END"
            # 가동이 한 번이라도 관측된 뒤 꺼진 경우만 '외부에서 끔' (연동 방의 ITSS HEAT 미반영은 재전송)
            if not is_heating and tm.get("lastObservedHeating") is True:
                return "CANCELLED"
            return "ACTIVE"
        if is_heating and tm.get("lastObservedHeating") is False and tm.get("lastCommandAction") != "HEAT":
            tm["externalBoostStartedAt"] = now.isoformat(timespec="seconds")
            tm["externalBoostUntil"] = (now + timedelta(minutes=boost_min)).isoformat(timespec="seconds")
            return "START"
        return None

    def _link_floor_group_boost(self, rooms: list[dict[str, Any]], telemetry: dict[str, dict[str, Any]]) -> None:
        names = [
            rm["roomName"] for rm in rooms
            if rm["controlMode"] == "AUTO" and telemetry[rm["roomName"]]["floorSrc"] in ("MEASURED", "ESTIMATED")
        ]
        events = {n: telemetry[n].get("externalBoost") for n in names if telemetry[n].get("externalBoost")}
        if not events:
            return
        vals = set(events.values())
        if "CANCELLED" in vals:
            g_event = "CANCELLED"
        elif "END" in vals:
            g_event = "END"
        elif "ACTIVE" in vals:
            g_event = "ACTIVE"  # 진행 중 연동에 새로 들어온 방은 같은 종료 시각으로 합류
        else:
            g_event = "START"
        # 시작/진행 중이면 종료 시각 기준 방 (START 방 또는 이미 진행 중인 방)
        src = next((n for n in names if events.get(n) == g_event), None)
        src_tm = self.room_timers.get(src, {}) if src else {}
        for n in names:
            tm_n = self.room_timers.setdefault(n, {"heatingStartedAt": None, "cooldownUntil": None})
            if g_event in ("START", "ACTIVE"):
                tm_n["externalBoostUntil"] = src_tm.get("externalBoostUntil")
                tm_n["externalBoostStartedAt"] = src_tm.get("externalBoostStartedAt")
            tm_n["externalBoostGroup"] = True
            telemetry[n]["externalBoost"] = g_event
            telemetry[n]["externalBoostGroup"] = True

    @staticmethod
    def _apply_floor_min(
        action: str,
        reason: str,
        *,
        floor_min_c: float,
        floor_c: float | None,
        dev_c: float | None,
        is_heating: bool,
        start_floor_c: float | None,
        run_min: float,
        rise_rate_c_per_min: float,
    ) -> tuple[str, str]:
        """바닥 최저 온도 유지 (그룹 수요 보정).

        - 비가열: 바닥 < floorMinC 이고 그룹 실내 편차 < +0.3°C 이면 보충 가동 시작
        - 가열 중: 상승률 모델 예측 피크(시작 바닥 + rate x 가동 분)가 floorMinC + 1.0°C 에 닿을 때까지 유지
        바닥 상한/과열/잔열 휴지 등 안전 판정은 기준 방 sentinel 이 그대로 적용한다.
        """
        if floor_min_c <= 0 or floor_c is None or dev_c is None or action == "HEAT":
            return action, reason
        if dev_c >= FLOOR_MIN_AIR_MARGIN_C:
            return action, reason
        target_peak = floor_min_c + FLOOR_MIN_HYST_C
        if not is_heating:
            if floor_c < floor_min_c:
                return "HEAT", "GROUP_FLOOR_MIN_HEAT"
            return action, reason
        start = start_floor_c if start_floor_c is not None else floor_c
        if start >= target_peak:
            return action, reason  # 바닥 최저와 무관한 가동 -> 실내 판정 그대로
        if start + rise_rate_c_per_min * run_min < target_peak:
            return "HEAT", "GROUP_FLOOR_MIN_CONTINUE"
        return "OFF", "GROUP_FLOOR_MIN_REACHED"

    def _manual_heat_block_reason(
        self,
        t: dict[str, Any],
        tm: dict[str, Any],
        *,
        floor_age: float | None,
        floor_stale_sec: float,
        floor_cap: float | None = None,
        floor_rate: float | None = None,
        poll_interval_min: float = 1.0,
    ) -> str | None:
        """수동 ON 에도 적용되는 최소 안전 조건 (센서 누락/stale, 바닥 상한 + 1.0°C 히스테리시스).

        v2.9.1: 자동과 같은 바닥 상승률 예측 상한(시작 전/가동 중) 적용, 추정 방은 실측보다 낮게 보지 않음.

        v2.9.0: 독립 제어 방(ESTIMATE_OFF)은 바닥 기준이 없으므로 실내 센서 조건만 본다.
        """
        if t["floorSrc"] == "ESTIMATE_OFF":
            if t["roomTemp"] is None:
                return "P000_SENSOR_MISSING"
            if t["roomAge"] is not None and t["roomAge"] > t["roomStaleSec"]:
                return "P000_SENSOR_STALE"
            tm["manualFloorLatched"] = False
            return None
        if t["floorSrc"] == "UNCONFIGURED":
            return "P000_FLOOR_UNCONFIGURED"
        if t["roomTemp"] is None or t["guardFloor"] is None:
            return "P000_SENSOR_MISSING"
        if (t["roomAge"] is not None and t["roomAge"] > t["roomStaleSec"]) or (
            floor_age is not None and floor_age > floor_stale_sec
        ):
            return "P000_SENSOR_STALE"
        # 같은 바닥 공유 추정 방: 음수 offset 으로 실측보다 낮게 추정되어 혼자 재가열하던 문제 방지
        adj = 0.0
        if t["floorSrc"] == "ESTIMATED":
            adj = max(0.0, -float((t.get("room") or {}).get("floorOffsetC") or 0.0))
        g = float(t["guardFloor"]) + adj
        limit = float(self.settings["floorOffLimitC"])
        latched = bool(tm.get("manualFloorLatched"))
        if g + t["floorLatency"] >= limit:
            latched = True
        elif g <= limit - 1.0:
            latched = False
        tm["manualFloorLatched"] = latched
        if latched:
            return "P000_FLOOR_LIMIT_OFF"
        if floor_cap is not None and floor_rate is not None:
            pred_limit = floor_cap - FLOOR_PEAK_MARGIN_C
            if t["isHeating"]:
                start = tm.get("heatingStartFloor")
                start_c = (float(start) + adj) if start is not None else g
                peak = max(g, start_c + floor_rate * (float(t["runMin"] or 0.0) + poll_interval_min))
            else:
                peak = g + floor_rate * MIN_PULSE_MIN
            if peak >= pred_limit:
                return "P000_FLOOR_PREDICTED_LIMIT"
        return None

    # ------------------------------------------------------------------ 제어 루프
    async def run_control_cycle(self) -> list[dict[str, Any]]:
        """한 회차 실행. 루프/수동 실행/프리셋이 겹쳐도 lock 으로 직렬화한다."""
        async with self.cycle_lock:
            self.loop_state["lastCycleStartedAt"] = now_kst_iso()
            try:
                results = await self._run_cycle_locked()
            except Exception as err:
                await self._on_cycle_failure(err)
                raise
            await self._on_cycle_success()
            return results

    async def _on_cycle_failure(self, err: BaseException) -> None:
        ls = self.loop_state
        ls["consecutiveFailures"] = int(ls["consecutiveFailures"]) + 1
        ls["lastError"] = f"{type(err).__name__}: {err}"
        ls["lastErrorAt"] = now_kst_iso()
        _LOGGER.exception("제어 회차 실패 (연속 %d회)", ls["consecutiveFailures"])
        if ls["consecutiveFailures"] == LOOP_FAILURE_ALERT_COUNT:
            await self._safe_notify(
                "⚠ I.T.S.S. 제어 루프 연속 실패",
                f"제어 회차가 {LOOP_FAILURE_ALERT_COUNT}회 연속 실패했습니다.\n최근 오류: {ls['lastError']}",
            )

    async def _on_cycle_success(self) -> None:
        ls = self.loop_state
        if int(ls["consecutiveFailures"]) >= LOOP_FAILURE_ALERT_COUNT:
            await self._safe_notify("✅ I.T.S.S. 제어 루프 복구", "제어 회차가 다시 정상 실행되고 있습니다.")
        ls["consecutiveFailures"] = 0
        ls["lastCycleOkAt"] = now_kst_iso()

    def health_status(self) -> dict[str, Any]:
        """감시용 상태: 연속 실패 또는 3주기 이상 정상 회차가 없으면 degraded."""
        now = datetime.now(tz=KST)
        ls = self.loop_state
        interval = float(self.settings["updateIntervalSec"])
        grace = timedelta(seconds=3 * interval + 60)
        problems: list[str] = []
        if int(ls["consecutiveFailures"]) >= LOOP_FAILURE_ALERT_COUNT:
            problems.append(f"제어 회차 연속 실패 {ls['consecutiveFailures']}회")
        boot = _parse_ts(ls["bootAt"]) or now
        last_ok = _parse_ts(ls["lastCycleOkAt"])
        has_rooms = bool(self.db.list_rooms())
        if has_rooms and (now - boot) > grace and (last_ok is None or (now - last_ok) > grace):
            problems.append("최근 3주기 동안 정상 제어 회차 없음")
        warnings: list[str] = []
        if not self.floor_configured:
            warnings.append("마스터 바닥 온도 센서 미설정 (자동 난방 차단 중)")
        if float(self.settings["floorOffLimitC"]) >= DEFAULT_OVERHEAT_FLOOR_C:
            warnings.append(
                f"바닥 OFF 상한 {self.settings['floorOffLimitC']}°C 가 바닥 과열 차단 {DEFAULT_OVERHEAT_FLOOR_C}°C 이상 "
                "-> 과열 차단이 먼저 작동합니다"
            )
        return {
            "status": "degraded" if problems else "ok",
            "problems": problems,
            "warnings": warnings,
            **ls,
        }

    def sensor_health(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """바닥/실내 센서별 관측 보고 주기·stale 임계 요약."""
        now = now or datetime.now(tz=KST)
        out: list[dict[str, Any]] = []
        floor_ent = str(self.settings.get("masterFloorEntity") or "").strip()
        if floor_ent:
            out.append({"entityId": floor_ent, "role": "FLOOR_MASTER", "roomName": None,
                        **st.summarize(self._track(floor_ent), now)})
        for rm in self.db.list_rooms():
            ent = rm["roomTempEntity"]
            out.append({"entityId": ent, "role": "ROOM", "roomName": rm["roomName"],
                        **st.summarize(self.sensor_tracks.get(ent) or st.new_track(), now)})
        return out

    async def _run_cycle_locked(self) -> list[dict[str, Any]]:
        """Execute one coordinated P000-P400 + P200 predictive multi-zone cycle."""
        t0 = time.perf_counter()
        now = datetime.now(tz=KST)
        scheduled_at = now.isoformat(timespec="seconds")
        rooms = self.db.list_rooms()
        if not rooms:
            return []
        self._cycle_seq += 1
        seq = self._cycle_seq
        dry_run = bool(self.settings["dryRun"])

        # ---- 마스터 바닥 센서: 보고 주기 감지 + 센서 시각 기준 추세/지연 보상
        floor_entity = str(self.settings.get("masterFloorEntity") or "").strip()
        floor_configured = bool(floor_entity)
        master_floor: float | None = None
        floor_ts: str | None = None
        floor_age: float | None = None
        floor_track: dict[str, Any] | None = None
        floor_ts_src: str | None = None
        if floor_configured:
            master_floor, floor_ts, floor_age, floor_ts_src = await self._read_numeric(floor_entity)
            floor_track = self._observe_sensor(floor_entity, "바닥(마스터)", master_floor, floor_ts, floor_ts_src, now)

        if floor_track is not None:
            floor_stale_sec = st.stale_threshold_sec(floor_track)
            floor_slope = st.regression_slope_per_min(floor_track, now, st.adaptive_window_min(floor_track, 16.0))
            v_floor_master = floor_slope if floor_slope is not None else 0.0
            floor_trend_master = st.delta_over(
                floor_track, now, st.adaptive_window_min(floor_track, 10.0, factor=2.0, cap_min=30.0), min_span_min=3.0
            )
            floor_drop5_master = st.delta_over(floor_track, now, 5.0)
            floor_latency_master = st.latency_rise_c(floor_track, now, v_floor_master)
            floor_interval = st.typical_interval_sec(floor_track)
        else:
            floor_stale_sec = float(DEFAULT_MAX_SENSOR_AGE_SEC)
            v_floor_master = 0.0
            floor_trend_master = floor_drop5_master = None
            floor_latency_master = 0.0
            floor_interval = None

        outdoor_temp, wind_speed, weather_label = await self._read_outdoor_weather()

        atmos_offset = self.atmos.compute_offset(outdoor_temp=outdoor_temp, wind_speed_ms=wind_speed, now_kst=now)
        max_run_min = self.atmos.compute_max_run_minutes(
            outdoor_temp=outdoor_temp,
            now_kst=now,
            hard_cap_min=float(self.settings["maxRunCapMin"]),
        )
        floor_off_limit = float(self.settings["floorOffLimitC"])
        sentinel = SentinelProtocol(SentinelConfig(general_off_floor_c=floor_off_limit))
        predictor = self._build_predictor()
        schedule_offset, schedule_label = self.db.active_schedule_offset(now.hour)
        preset_offset = PRESET_OFFSETS.get(self.active_preset, 0.0)

        # 트레이스용 회차 공통 컨텍스트 (방별 JSONL 레코드에 포함)
        cycle_ctx: dict[str, Any] = {
            "seq": seq,
            "scheduledAt": scheduled_at,
            "dryRun": dry_run,
            "strategy": self.settings["zoneStrategy"],
            "predictor": self.settings["predictorModel"],
            "outdoorTemp": outdoor_temp,
            "windSpeedMs": wind_speed,
            "weatherSource": weather_label,
            "atmosOffsetC": atmos_offset,
            "schedule": {"label": schedule_label, "offsetC": schedule_offset},
            "preset": {"name": self.active_preset, "offsetC": preset_offset},
            "outdoorMaxRunMin": max_run_min,
            "floorOffLimitC": floor_off_limit,
            "floor": {
                "entity": floor_entity or None,
                "value": master_floor,
                "reportAt": floor_ts,
                "ageSec": floor_age,
                "tsSource": floor_ts_src,
                "typicalIntervalSec": floor_interval,
                "maxIntervalSec": st.max_interval_sec(floor_track) if floor_track else None,
                "staleThresholdSec": floor_stale_sec,
                "slopePerMin": round(v_floor_master, 4),
                "trendC": floor_trend_master,
                "drop5mC": floor_drop5_master,
                "latencyC": floor_latency_master,
            },
        }

        # 주기적 학습/보존 정책
        if seq % 30 == 0 or not self.lag_profiles:
            self.refresh_lag_profiles()
        if seq % 60 == 0:
            self.prune_history()

        telemetry: dict[str, dict[str, Any]] = {}
        climate_states: dict[str, dict[str, Any] | None] = {}
        auto_zone_readings: list[ZoneReading] = []
        p200_by_room: dict[str, Any] = {}

        for rm in rooms:
            r_name = rm["roomName"]
            r_temp, r_ts, r_age, r_ts_src = await self._read_numeric(rm["roomTempEntity"])
            # 센서 트랙(보고 주기/기울기)은 원시값으로 유지 — 보정값 변경 시 가짜 계단 변화 방지
            r_track = self._observe_sensor(rm["roomTempEntity"], f"실내[{r_name}]", r_temp, r_ts, r_ts_src, now)
            r_raw = r_temp
            r_off = float(rm.get("roomTempOffsetC") or 0.0)
            if r_temp is not None and r_off:
                r_temp = round(r_temp + r_off, 2)
            c_state = await self.ha.get_state(rm["climateEntity"])
            climate_states[r_name] = c_state
            hvac_mode = c_state.get("state") if c_state else None
            combustion = detect_boiler_running(c_state)
            is_heating = bool(combustion) if combustion is not None else (hvac_mode == "heat")

            eff_floor, floor_src, guard_floor = self._room_floor(rm, master_floor, floor_configured)
            estimate_on = bool(rm.get("floorEstimateEnabled", True))

            tm = self.room_timers.setdefault(r_name, {"heatingStartedAt": None, "cooldownUntil": None})
            # v2.6 이하의 제어주기 기준 샘플 키 정리 (센서 시각 기준 sensor_tracks 로 대체)
            for old_key in ("prevSampleAt", "prevRoomTemp", "prevFloorTemp", "recentSamples"):
                tm.pop(old_key, None)

            # 센서 보고 시각 기준 변화율/변화량/회귀 기울기
            room_slope = st.regression_slope_per_min(r_track, now, st.adaptive_window_min(r_track, 16.0))
            v_room_smooth = room_slope if room_slope is not None else 0.0
            v_room_rate = st.recent_rate_per_min(r_track, now)
            drop_5m_room = st.delta_over(r_track, now, 5.0)
            trend_room = st.delta_over(
                r_track, now, st.adaptive_window_min(r_track, 10.0, factor=2.0, cap_min=30.0), min_span_min=3.0
            )
            room_stale_sec = st.stale_threshold_sec(r_track)
            # 바닥 동특성(기울기/추세/지연)은 안전 판정에 쓰는 바닥값(실측·추정·대리)이 있을 때만 사용
            has_eff_floor = guard_floor is not None

            # 창문 접점 센서 (열림 즉시 차단 + 닫힘 후 10분 회복 대기)
            window_open = False
            win_ent = str(rm.get("windowSensorEntity") or "").strip()
            if win_ent:
                w_raw = await self.ha.get_state(win_ent)
                if w_raw:
                    window_open = str(w_raw.get("state") or "").lower() in ("on", "open")
            if bool(tm.get("wasWindowOpen", False)) and not window_open:
                rec_until = now + timedelta(minutes=10)
                cur_v = _parse_ts(tm.get("ventilationUntil"))
                if cur_v is None or cur_v < rec_until:
                    tm["ventilationUntil"] = rec_until.isoformat(timespec="seconds")
            tm["wasWindowOpen"] = window_open

            # v2.9.1 외부 조작 가동 감지 (가동 시작 시각/시작 바닥은 아래 공통 추적이 기록)
            boost_event = self._update_external_boost(rm, tm, is_heating=bool(is_heating), now=now)

            # Track continuous heating duration per room based on real boiler state
            if is_heating:
                if not tm.get("heatingStartedAt"):
                    tm["heatingStartedAt"] = now.isoformat(timespec="seconds")
                # 가동 시작 시 바닥 (상승률 모델 기준점, v2.9.0 이전 가동분은 현재값으로 보정)
                if tm.get("heatingStartFloor") is None:
                    tm["heatingStartFloor"] = guard_floor
            else:
                tm["heatingStartedAt"] = None
                tm.pop("heatingStartFloor", None)
            run_min, cd_min, vent_min = self._timer_metrics(r_name, now)

            eff_target = round(rm["targetTemp"] + atmos_offset + schedule_offset + preset_offset, 2)

            lag_prof = self.lag_profiles.get(r_name) or {}
            learned_horizon = int(lag_prof.get("recommendedHorizonMin", 30))
            learned_stats = ResidualHeatStats(
                sample_count=int(lag_prof.get("sampleCount", 0)),
                mean_rise_c=float(lag_prof.get("roomResidualRiseC", 0.0)),
            )
            if r_temp is not None and eff_floor is not None:
                p200_by_room[r_name] = predictor.predict(
                    ThermalInput(
                        room_temp=r_temp,
                        floor_temp=eff_floor,
                        target_temp=rm["targetTemp"],
                        is_heating=is_heating,
                        v_floor_per_min=v_floor_master,
                        v_room_per_min=v_room_smooth,
                        outdoor_temp=outdoor_temp,
                        atmos_loss_offset_c=atmos_offset,
                        circadia_target_offset_c=schedule_offset + preset_offset,
                        horizon_min=learned_horizon,
                    ),
                    residual_stats=learned_stats,
                )

            c_attrs = (c_state or {}).get("attributes") or {}
            telemetry[r_name] = {
                "room": rm,
                "roomTemp": r_temp,
                "roomTempRaw": r_raw,
                "roomTempOffsetC": r_off,
                "roomTs": r_ts,
                "roomAge": r_age,
                "roomTsSource": r_track.get("tsSource"),
                "roomMaxInterval": st.max_interval_sec(r_track),
                "climate": {
                    "state": hvac_mode,
                    "setpoint": c_attrs.get("temperature"),
                    "current": c_attrs.get("current_temperature"),
                    "hvacAction": c_attrs.get("hvac_action"),
                },
                "roomStaleSec": room_stale_sec,
                "roomInterval": st.typical_interval_sec(r_track),
                "effFloor": eff_floor,
                "guardFloor": guard_floor,
                "floorSrc": floor_src,
                "floorEstimateEnabled": estimate_on,
                "vRoomRate": v_room_rate,
                "vRoomSmooth": v_room_smooth,
                "vFloor": v_floor_master if has_eff_floor else 0.0,
                "floorLatency": floor_latency_master if has_eff_floor else 0.0,
                "hvacMode": hvac_mode,
                "isHeating": is_heating,
                "combustion": combustion,
                "effTarget": eff_target,
                "runMin": run_min,
                "cdMin": cd_min,
                "ventMin": vent_min,
                "windowOpen": window_open,
                "drop5mRoom": drop_5m_room,
                "drop5mFloor": floor_drop5_master if has_eff_floor else None,
                "trend10mRoom": trend_room,
                "trend10mFloor": floor_trend_master if has_eff_floor else None,
                # 동시 제어 그룹 판정 입력 (P200 무가열 예측, 학습 실내 잔열)
                "projectedCoast": (
                    (p200_by_room[r_name].diagnostics or {}).get("projected_coast") if r_name in p200_by_room else None
                ),
                "roomResidualRise": float(lag_prof.get("roomResidualRiseC", 0.0) or 0.0),
                "externalBoost": boost_event,
            }
            if boost_event == "START":
                # 월패드/외부 자동화로 켬 = '추웠다' 신호 (감지한 방만, 연동 방 제외)
                self._record_comfort_event(
                    now=now, room=rm, source="WALLPAD", t=telemetry[r_name], outdoor_temp=outdoor_temp
                )

            # 다중 방(hybrid/simultaneous) 계획은 바닥 기준 없는 독립 방만 대상 (그룹 방은 그룹 판정)
            if (
                rm["controlMode"] == "AUTO" and r_temp is not None and floor_src == "ESTIMATE_OFF"
                and boost_event is None  # 외부 조작 가동 방은 다중 방 계획(동시 합류 등)에서 제외
            ):
                auto_zone_readings.append(
                    ZoneReading(
                        room_name=r_name,
                        room_temp=r_temp,
                        target_temp=eff_target,
                        is_heating=is_heating,
                        has_floor_sensor=rm["hasFloorSensor"],
                        floor_offset_from_master_c=rm["floorOffsetC"],
                        weight=rm["weight"],
                        floor_estimate_enabled=estimate_on,
                    )
                )

        # 바닥 공유 방(실측 + 추정, AUTO) 외부 조작 연동: 한 방이 월패드/자동화로 켜지면 같은 바닥 방 전체를
        # 같은 종료 시각으로 함께 가동 (그룹 판정 경로 사용 -> 기준 방 바닥 안전/타이머 동기 그대로)
        self._link_floor_group_boost(rooms, telemetry)

        # 독립 방 다중 방 계획 (마스터 바닥과 무관 — 바닥 센서 누락/과열에도 독립 방은 실내 기준 제어)
        mz_by_room: dict[str, Any] = {}
        if auto_zone_readings:
            mz_ctrl = MultiZoneController(
                strategy=self.settings["zoneStrategy"],
                general_off_floor_c=floor_off_limit,
            )
            mz_plan = mz_ctrl.evaluate(auto_zone_readings, master_floor_temp=None)
            mz_by_room = {c.room_name: c for c in mz_plan.commands}

        # 같은 바닥 기준(실측 방 + 추정 방) 동시 제어 그룹: 가중 평균 예측 편차로 그룹 수요 판정
        group_plan = self._floor_group_plan(rooms, telemetry)
        if group_plan is not None:
            weights = {rm["roomName"]: float(rm.get("weight") or 1.0) for rm in rooms}
            gd = evaluate_floor_group(
                [
                    GroupMember(
                        room_name=n,
                        room_temp=telemetry[n]["roomTemp"],
                        target_temp=telemetry[n]["effTarget"],
                        weight=weights.get(n, 1.0),
                        projected_coast_c=telemetry[n]["projectedCoast"],
                        residual_rise_c=telemetry[n]["roomResidualRise"],
                    )
                    for n in group_plan["members"]
                ],
                is_heating=bool(telemetry[group_plan["leader"]]["isHeating"]),
            )
            demand, demand_reason = gd.action, gd.reason
            lead_name = group_plan["leader"]
            lead_t = telemetry[lead_name]
            lead_tm = self.room_timers.get(lead_name, {})
            lead_lag = self.lag_profiles.get(lead_name) or {}
            demand, demand_reason = self._apply_floor_min(
                demand,
                demand_reason,
                floor_min_c=float(self.settings.get("floorMinC") or 0.0),
                floor_c=lead_t["guardFloor"],
                dev_c=gd.weighted_dev_c,
                is_heating=bool(lead_t["isHeating"]),
                start_floor_c=lead_tm.get("heatingStartFloor"),
                run_min=float(lead_t["runMin"] or 0.0),
                rise_rate_c_per_min=float(lead_lag.get("floorRiseRateCPerMin") or DEFAULT_FLOOR_RISE_RATE_C_PER_MIN),
            )
            g_boost = lead_t.get("externalBoost") if lead_t.get("externalBoostGroup") else None
            if g_boost in ("START", "ACTIVE"):
                demand, demand_reason = "HEAT", "EXTERNAL_BOOST"
            elif g_boost == "END":
                demand, demand_reason = "OFF", "EXTERNAL_BOOST_END"
            elif g_boost == "CANCELLED":
                demand, demand_reason = "OFF", "EXTERNAL_BOOST_CANCELLED"
            group_plan.update(
                {"demand": demand, "demandReason": demand_reason, "weightedDevC": gd.weighted_dev_c,
                 "membersUsed": gd.members_used, "externalBoost": g_boost}
            )

        # 기준 방을 먼저 판정해야 추종 방이 같은 명령을 따를 수 있음 (결과는 원래 방 순서로 반환)
        order = list(range(len(rooms)))
        if group_plan is not None:
            lead_idx = next(i for i, rm in enumerate(rooms) if rm["roomName"] == group_plan["leader"])
            order = [lead_idx] + [i for i in order if i != lead_idx]

        results_by_idx: dict[int, dict[str, Any]] = {}
        room_errors = 0
        try:
            for idx in order:
                rm = rooms[idx]
                r_name = rm["roomName"]
                role = None
                if group_plan is not None and r_name in group_plan["members"]:
                    role = "leader" if r_name == group_plan["leader"] else "follower"
                try:
                    payload = await self._decide_room(
                        rm=rm,
                        t=telemetry[r_name],
                        c_state=climate_states.get(r_name),
                        pred=p200_by_room.get(r_name),
                        mz_cmd=mz_by_room.get(r_name),
                        group=group_plan if role else None,
                        group_role=role,
                        sentinel=sentinel,
                        now=now,
                        seq=seq,
                        scheduled_at=scheduled_at,
                        dry_run=dry_run,
                        outdoor_temp=outdoor_temp,
                        max_run_min=max_run_min,
                        floor_ts=floor_ts,
                        floor_age=floor_age,
                        floor_stale_sec=floor_stale_sec,
                        floor_interval=floor_interval,
                        cycle_ctx=cycle_ctx,
                    )
                except Exception as err:  # noqa: BLE001
                    room_errors += 1
                    _LOGGER.exception("방 '%s' 제어 처리 실패", r_name)
                    rec = CycleRecord(
                        cycle_id=f"{now.strftime('%Y%m%d-%H%M%S')}-{seq:05d}-{r_name}",
                        room_name=r_name,
                        scheduled_at=scheduled_at,
                        execution_started_at=now_kst_iso(),
                        execution_status="FAILED",
                        safety_reason="INTERNAL_ERROR",
                        dry_run=dry_run,
                        dispatch_result="NONE",
                        diagnostics={"error": f"{type(err).__name__}: {err}"},
                    )
                    rec.saved_at = now_kst_iso()
                    payload = rec.to_payload()
                    try:
                        self.db.insert_cycle(payload)
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("실패 회차 기록 저장 실패")
                results_by_idx[idx] = payload
        finally:
            # 사용 중인 센서만 트랙 유지
            used = {rm["roomTempEntity"] for rm in rooms}
            if floor_entity:
                used.add(floor_entity)
            self.sensor_tracks = {k: v for k, v in self.sensor_tracks.items() if k in used}
            self.db.set_kv("room_timers", self.room_timers)
            self.db.set_kv("sensor_tracks", self.sensor_tracks)

        results = [results_by_idx[i] for i in sorted(results_by_idx)]
        self.loop_state["lastRoomErrors"] = room_errors
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.loop_state["lastCycleElapsedMs"] = round(elapsed_ms, 1)
        summary = " ".join(
            f"{p.get('roomName')}={p.get('commandAction') or '-'}({p.get('safetyReason') or '-'}/{p.get('dispatchResult') or '-'})"
            for p in results
        )
        _LOG_CYCLE.info(
            "회차 #%d %.0fms %s 바닥=%s 외기=%s %s%s",
            seq,
            elapsed_ms,
            "DRY" if dry_run else "LIVE",
            master_floor,
            outdoor_temp,
            summary,
            f" 방오류={room_errors}" if room_errors else "",
        )
        if room_errors and room_errors == len(rooms):
            raise RuntimeError(f"모든 방({room_errors}개) 제어 처리 실패")
        return results

    async def _decide_room(
        self,
        *,
        rm: dict[str, Any],
        t: dict[str, Any],
        c_state: dict[str, Any] | None,
        pred: Any,
        mz_cmd: Any,
        sentinel: SentinelProtocol,
        now: datetime,
        seq: int,
        scheduled_at: str,
        dry_run: bool,
        outdoor_temp: float | None,
        max_run_min: float,
        floor_ts: str | None,
        floor_age: float | None,
        floor_stale_sec: float,
        floor_interval: float | None,
        cycle_ctx: dict[str, Any] | None = None,
        group: dict[str, Any] | None = None,
        group_role: str | None = None,
    ) -> dict[str, Any]:
        """한 방의 P000 -> (수동/동시 제어 그룹/독립 다중방) 판단, 명령 전송, 이력 저장 + 디버그 트레이스.

        group_role:
          - "leader"   : 바닥 센서 방. 그룹 공통 바닥으로 안전 판정 + 그룹 수요(가중 평균)로 ON/OFF.
          - "follower" : 같은 바닥 기준의 추정 방. 실내 기준 안전(과열/창문/환기/센서)만 자체 판정하고
                         ON/OFF·타이머는 기준 방을 그대로 따른다 (기준 방 OFF 인데 혼자 가열 금지).
          - None       : 독립 제어 방(바닥 없음) 또는 수동/미구성 방.
        """
        t_room0 = time.perf_counter()
        r_name = rm["roomName"]
        exec_started = now_kst_iso()
        cycle_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{seq:05d}-{r_name}"
        eff_floor = t["effFloor"]
        guard_floor = t["guardFloor"]  # 안전 판정용 바닥 (독립 방은 None)
        floor_src = t["floorSrc"]
        is_manual = rm["controlMode"] == "MANUAL"
        is_leader = group is not None and group_role == "leader"
        is_follower = group is not None and group_role == "follower"
        # 독립 방은 바닥 없음, 추종 방은 바닥 판정을 기준 방에 위임
        floor_optional = floor_src == "ESTIMATE_OFF" or is_follower
        sentinel_floor = None if is_follower else guard_floor
        floor_age_eff = floor_age if guard_floor is not None else None

        snapshot = SensorSnapshot(
            room_name=r_name,
            room_temp=t["roomTemp"],
            floor_temp=sentinel_floor,
            outdoor_temp=outdoor_temp,
            thermostat_hvac_mode=t["hvacMode"],
            combustion_confirmed=t["combustion"],
            manual_override_active=False,
            sensor_measured_at_room=t["roomTs"],
            sensor_measured_at_floor=floor_ts,
            room_sensor_age_sec=t["roomAge"],
            floor_sensor_age_sec=floor_age_eff if sentinel_floor is not None else None,
        )

        record = CycleRecord(
            cycle_id=cycle_id,
            room_name=r_name,
            scheduled_at=scheduled_at,
            execution_started_at=exec_started,
            sensor_measured_at_room=t["roomTs"],
            sensor_measured_at_floor=floor_ts,
            room_temp=t["roomTemp"],
            floor_temp=eff_floor,
            floor_sensor_source=floor_src,
            target_temp=rm["targetTemp"],
            predicted_room_30m=pred.predicted_room_temp if pred else None,
            predicted_floor_30m=pred.predicted_floor_temp if pred else None,
            predictor_model="manual" if is_manual else self.settings["predictorModel"],
            residual_learning_status=pred.residual_learning_status if pred else None,
            device_mode_event=t["hvacMode"],
            combustion_confirmed=t["combustion"],
            dry_run=dry_run,
            effective_target_temp=t["effTarget"],
            outdoor_temp=outdoor_temp,
        )
        record.diagnostics = {
            "floorReportIntervalSec": floor_interval,
            "floorStaleThresholdSec": floor_stale_sec,
            "floorLatencyC": t["floorLatency"],
            "roomReportIntervalSec": t["roomInterval"],
            "roomStaleThresholdSec": t["roomStaleSec"],
            "vRoomRate": round(t["vRoomRate"], 4),
            "vRoomSmooth": round(t["vRoomSmooth"], 4),
            "vFloor": round(t["vFloor"], 4),
            "drop5mRoom": t["drop5mRoom"],
            "trend10mRoom": t["trend10mRoom"],
            "trend10mFloor": t["trend10mFloor"],
            "floorEstimateEnabled": t["floorEstimateEnabled"],
            "guardFloor": guard_floor,
            "groupRole": group_role,
            "groupLeader": group["leader"] if group is not None else None,
        }

        # 과열 기준은 const.py 단일 출처 (수동 운전에도 항상 적용, 독립 방은 실내만 판정)
        overheat = (
            (t["roomTemp"] is not None and t["roomTemp"] >= DEFAULT_OVERHEAT_ROOM_C)
            or (guard_floor is not None and guard_floor >= DEFAULT_OVERHEAT_FLOOR_C)
        )
        lag_prof = self.lag_profiles.get(r_name) or {}
        eff_floor_limit = self._compute_effective_floor_limit(t["effTarget"], outdoor_temp)
        floor_cap = self.floor_cap_c(eff_floor_limit)
        # 레거시 표시용 (v2.8 이하 단일 잔열 상승폭) — v2.9.0 판정은 상승률(floor_rate) 사용
        learned_floor_surge = max(1.2, min(4.0, float(lag_prof.get("floorResidualRiseC", 1.2))))
        floor_rate = float(lag_prof.get("floorRiseRateCPerMin") or DEFAULT_FLOOR_RISE_RATE_C_PER_MIN)
        tm = self.room_timers.setdefault(r_name, {"heatingStartedAt": None, "cooldownUntil": None})
        # 외기 연동 1회 최대 가동 (학습 단축 반영) — sentinel 5a 기준
        base_room_max_run = max(
            MIN_PULSE_MIN,
            min(15.0, max_run_min - float(lag_prof.get("recommendedRunTrimMin", 0.0))),
        )
        # 바닥 상승률로 계산한 실효 최대 가동 (표시/알림용, 바닥 차단 자체는 sentinel 4b/6e 가 같은 식으로 판정)
        start_floor_ref = (tm.get("heatingStartFloor") if t["isHeating"] else None)
        if start_floor_ref is None:
            start_floor_ref = guard_floor
        if guard_floor is not None and not is_follower:
            room_max_run_min = self._compute_floor_limited_max_run(
                base_max_run_min=base_room_max_run,
                start_floor_c=start_floor_ref,
                floor_cap_c=floor_cap,
                rise_rate_c_per_min=floor_rate,
            )
        else:
            room_max_run_min = base_room_max_run
        if is_follower and (group or {}).get("result"):
            room_max_run_min = group["result"].get("maxRunMin", room_max_run_min)
        room_cooldown_min = int(lag_prof.get("recommendedCooldownMin", self.settings["cooldownMin"]))
        poll_interval_min = max(0.5, float(self.settings["updateIntervalSec"]) / 60.0)
        decision_reason = "P000_OK"
        boost_event = t.get("externalBoost")
        boost_grouped = bool(t.get("externalBoostGroup"))  # 바닥 공유 방 연동 외부 조작 (그룹 경로로 판정)
        group_boost_any = boost_grouped and bool(boost_event)  # 연동 외부 조작 회차: 최대 가동/휴지 우회
        boost_hold = False  # True: 외부 조작 설정값 유지 (ITSS 미전송)
        if boost_event:
            record.diagnostics["externalBoost"] = boost_event
            if boost_grouped:
                record.diagnostics["externalBoostGroup"] = True
        manual_heat = is_manual and rm["manualAction"] == "HEAT"
        manual_end: str | None = None  # 수동 HEAT 종료 사유 (자동 제어 복귀)
        if not manual_heat:
            tm.pop("manualHeatSince", None)

        last_cmd = tm.get("lastCommandAction")
        last_switch = _parse_ts(tm.get("lastSwitchAt"))
        secs_since_switch = (now - last_switch).total_seconds() if last_switch is not None else None
        verdict = None
        timers_before = {
            "runMin": t["runMin"],
            "cooldownRemainMin": t["cdMin"],
            "ventilationRemainMin": t["ventMin"],
            "lastCommand": last_cmd,
            "secsSinceSwitch": round(secs_since_switch, 1) if secs_since_switch is not None else None,
        }

        if overheat:
            decision_reason = "P000_OVERHEAT_CUTOFF"
            record.safety_reason = decision_reason
            record.command_action = "OFF"
            record.execution_status = "SKIPPED_SAFETY"
            tm["heatingStartedAt"] = None
            tm["cooldownUntil"] = (now + timedelta(minutes=room_cooldown_min)).isoformat(timespec="seconds")
            if manual_heat:
                manual_end = decision_reason
        elif boost_event in ("START", "ACTIVE") and not boost_grouped:
            # v2.9.1 외부 조작 가동 유지: 잔열 휴지·1회 최대 가동·그룹/다중 방 수요 대신 설정 시간 유지.
            # 실내 과열/창문/환기/센서 + 바닥 상승률 예측 상한(바닥 기준 방)은 그대로 적용.
            verdict = sentinel.evaluate(
                snapshot,
                is_heating=True,
                v_room_per_min=t["vRoomRate"],
                v_floor_per_min=t["vFloor"],
                continuous_run_min=t["runMin"],
                max_run_min=None,
                poll_interval_min=poll_interval_min,
                cooldown_remaining_min=None,
                window_open=t["windowOpen"],
                drop_5m_room_c=t["drop5mRoom"],
                drop_5m_floor_c=t["drop5mFloor"],
                ventilation_remaining_min=t["ventMin"],
                effective_floor_limit_c=None if sentinel_floor is None else eff_floor_limit,
                expected_floor_surge_c=learned_floor_surge,
                trend_10m_room_c=t["trend10mRoom"],
                trend_10m_floor_c=t["trend10mFloor"],
                room_stale_after_sec=t["roomStaleSec"],
                floor_stale_after_sec=floor_stale_sec,
                floor_latency_c=t["floorLatency"],
                floor_optional=floor_optional,
                floor_rise_rate_c_per_min=floor_rate if sentinel_floor is not None else None,
                heating_start_floor_c=tm.get("heatingStartFloor"),
                min_pulse_min=MIN_PULSE_MIN,
            )
            record.safety_reason = verdict.reason_code
            if verdict.force_off:
                decision_reason = verdict.reason_code
                record.command_action = "OFF"
                record.execution_status = "SKIPPED_SAFETY"
                if verdict.reason_code == "P000_VENTILATION_PROTECT":
                    tm["ventilationUntil"] = (
                        now + timedelta(minutes=int(self.settings["ventilationHoldMin"]))
                    ).isoformat(timespec="seconds")
            else:
                decision_reason = "EXTERNAL_BOOST"
                record.command_action = "HEAT"
                record.execution_status = "EXECUTED"
                record.safety_reason = decision_reason
                boost_hold = True
        elif boost_event in ("END", "CANCELLED") and not boost_grouped:
            decision_reason = "EXTERNAL_BOOST_END" if boost_event == "END" else "EXTERNAL_BOOST_CANCELLED"
            record.command_action = "OFF"
            record.execution_status = "EXECUTED"
            record.safety_reason = decision_reason
        elif is_manual:
            manual_action = rm["manualAction"]
            block = None
            if manual_heat:
                block = self._manual_heat_block_reason(
                    t, tm, floor_age=floor_age_eff, floor_stale_sec=floor_stale_sec,
                    floor_cap=floor_cap if guard_floor is not None else None,
                    floor_rate=floor_rate if guard_floor is not None else None,
                    poll_interval_min=poll_interval_min,
                )
                since = _parse_ts(tm.get("manualHeatSince"))
                limit_min = int(self.settings.get("manualHeatMaxMin", MANUAL_HEAT_DEFAULT_MIN))
                if (
                    since is not None and last_cmd == "HEAT" and tm.get("lastObservedHeating") is True
                    and not t["isHeating"]
                ):
                    manual_end = "MANUAL_HEAT_CANCELLED"  # 월패드/자동화로 끔 -> 되살리지 않음
                elif since is not None and (now - since) >= timedelta(minutes=limit_min):
                    manual_end = "MANUAL_HEAT_TIMEOUT"
                elif block in ("P000_FLOOR_LIMIT_OFF", "P000_FLOOR_PREDICTED_LIMIT"):
                    manual_end = block
            if manual_end:
                decision_reason = manual_end
                record.command_action = "OFF"
                record.execution_status = "EXECUTED"
            elif block:
                decision_reason = block
                record.command_action = "OFF"
                record.execution_status = "SKIPPED_SAFETY"
            else:
                decision_reason = "P000_MANUAL_OVERRIDE"
                record.command_action = manual_action
                record.execution_status = "EXECUTED"
                if manual_heat and not tm.get("manualHeatSince"):
                    tm["manualHeatSince"] = now.isoformat(timespec="seconds")
                    self._record_comfort_event(
                        now=now, room=rm, source="ITSS_MANUAL", t=t, outdoor_temp=outdoor_temp
                    )
            record.safety_reason = decision_reason
        elif floor_src == "UNCONFIGURED":
            # 바닥 센서 없이 자동 난방하면 바닥 상한/과열 보호가 불가능 -> 차단
            decision_reason = "P000_FLOOR_UNCONFIGURED"
            record.safety_reason = decision_reason
            record.command_action = "OFF"
            record.execution_status = "SKIPPED_SAFETY"
        else:
            # 추종 방: 바닥·가동시간·휴지는 기준 방이 판정 (여기서는 실내 기준 안전만)
            verdict = sentinel.evaluate(
                snapshot,
                is_heating=t["isHeating"],
                v_room_per_min=t["vRoomRate"],
                v_floor_per_min=t["vFloor"],
                continuous_run_min=None if is_follower else t["runMin"],
                max_run_min=None if (is_follower or group_boost_any) else base_room_max_run,
                poll_interval_min=poll_interval_min,
                cooldown_remaining_min=None if (is_follower or group_boost_any) else t["cdMin"],
                window_open=t["windowOpen"],
                drop_5m_room_c=t["drop5mRoom"],
                drop_5m_floor_c=t["drop5mFloor"],
                ventilation_remaining_min=t["ventMin"],
                effective_floor_limit_c=None if sentinel_floor is None else eff_floor_limit,
                expected_floor_surge_c=learned_floor_surge,
                trend_10m_room_c=t["trend10mRoom"],
                trend_10m_floor_c=t["trend10mFloor"],
                room_stale_after_sec=t["roomStaleSec"],
                floor_stale_after_sec=floor_stale_sec,
                floor_latency_c=t["floorLatency"],
                floor_optional=floor_optional,
                floor_rise_rate_c_per_min=floor_rate if sentinel_floor is not None else None,
                heating_start_floor_c=tm.get("heatingStartFloor") if t["isHeating"] else None,
                min_pulse_min=MIN_PULSE_MIN,
            )
            record.safety_reason = verdict.reason_code
            if verdict.force_off:
                decision_reason = verdict.reason_code
                record.command_action = "OFF"
                record.execution_status = "SKIPPED_SAFETY"
                if verdict.reason_code in (
                    "P000_MAX_RUN_CUTOFF",
                    "P000_FLOOR_LIMIT_OFF",
                    "P000_FLOOR_PREDICTED_LIMIT",
                ):
                    if t["isHeating"] or verdict.reason_code == "P000_FLOOR_LIMIT_OFF":
                        tm["cooldownUntil"] = (now + timedelta(minutes=room_cooldown_min)).isoformat(
                            timespec="seconds"
                        )
                    tm["heatingStartedAt"] = None
                elif verdict.reason_code == "P000_VENTILATION_PROTECT":
                    tm["heatingStartedAt"] = None
                    tm["ventilationUntil"] = (
                        now + timedelta(minutes=int(self.settings["ventilationHoldMin"]))
                    ).isoformat(timespec="seconds")
            elif verdict.hold_current_state:
                decision_reason = verdict.reason_code
                record.command_action = "HOLD"
                record.execution_status = "SKIPPED_SAFETY"
            elif is_leader:
                # 동시 제어 그룹 기준 방: 그룹 가중 평균 예측 편차로 ON/OFF
                record.command_action = group["demand"]
                decision_reason = group["demandReason"]
                record.safety_reason = decision_reason
            elif is_follower:
                lead = group.get("result")
                upper = t["effTarget"] + DEFAULT_CONTROL_BAND_HALF
                if lead is None or (lead["action"] == "HEAT" and lead.get("dispatch") == "FAILED"):
                    # 기준 방 판정/명령 실패 시 추종 방 단독 가열 금지
                    record.command_action = "OFF"
                    decision_reason = "GROUP_LEADER_UNAVAILABLE"
                elif (
                    lead["action"] == "HEAT" and lead.get("reason") != "EXTERNAL_BOOST"
                    and t["roomTemp"] is not None and t["roomTemp"] >= upper
                ):
                    # 그룹 가열 중이라도 이 방만 상한 이상이면 개별 정지 (덜 가열 = 바닥 추정 보수 방향)
                    record.command_action = "OFF"
                    decision_reason = "GROUP_MEMBER_WARM_SKIP"
                else:
                    record.command_action = lead["action"]
                    decision_reason = lead["reason"]
                record.safety_reason = decision_reason
            elif mz_cmd is None:
                decision_reason = verdict.reason_code
                record.command_action = "HOLD"
                record.execution_status = "SKIPPED_SAFETY"
            else:
                record.command_action = mz_cmd.action
                decision_reason = mz_cmd.reason
                record.safety_reason = decision_reason

            # 채터링 방지: 안전 차단이 아닌 '재량' 명령이 직전 명령과 반대 방향으로 바뀔 때만
            # 최소 가동(HEAT->OFF 5분)/휴지(OFF->HEAT 10분) 시간을 보장
            # (추종 방은 기준 방 명령을 즉시 따라야 하므로 제외)
            if (
                not verdict.force_off
                and not is_follower
                and not (boost_grouped and boost_event)  # 외부 조작 연동 시작/종료는 사용자 의도 우선
                and record.command_action in ("HEAT", "OFF")
                and last_cmd in ("HEAT", "OFF")
                and record.command_action != last_cmd
                and secs_since_switch is not None
            ):
                min_dwell = DEFAULT_MIN_ON_SEC if last_cmd == "HEAT" else DEFAULT_MIN_OFF_SEC
                if secs_since_switch < min_dwell:
                    record.diagnostics["antiChatterBlocked"] = f"{last_cmd}->{record.command_action}({decision_reason})"
                    decision_reason = "P000_ANTI_CHATTER"
                    record.safety_reason = decision_reason
                    record.command_action = "HOLD"
                    record.execution_status = "SKIPPED_SAFETY"

        if t.get("roomTempOffsetC"):
            # 동시 비교 측정 보정값 계산 시 원시값 복원용
            record.diagnostics["roomTempOffsetC"] = t["roomTempOffsetC"]
        prev_run_min = t["runMin"]
        if manual_end:
            m_since = _parse_ts(tm.pop("manualHeatSince", None))
            m_ran = (now - m_since).total_seconds() / 60.0 if m_since is not None else 0.0
            if m_ran >= MIN_PULSE_MIN:
                tm["cooldownUntil"] = (now + timedelta(minutes=room_cooldown_min)).isoformat(timespec="seconds")
            tm["manualFloorLatched"] = False
            self.db.update_room_control(r_name, control_mode="AUTO", manual_action="HOLD")
            record.diagnostics["manualEnded"] = manual_end
            _LOG_CYCLE.info("[%s] 수동 HEAT 종료(%s, %.1f분) -> 자동 제어 복귀", r_name, manual_end, m_ran)
        boost_total_min = None
        if boost_event:
            b_start = _parse_ts(tm.get("externalBoostStartedAt"))
            b_until = _parse_ts(tm.get("externalBoostUntil"))
            if b_start is not None and b_until is not None:
                boost_total_min = round((b_until - b_start).total_seconds() / 60.0, 1)
            if record.command_action == "OFF":
                ran_min = (now - b_start).total_seconds() / 60.0 if b_start is not None else 0.0
                self._clear_external_boost(tm)
                if ran_min >= MIN_PULSE_MIN:
                    tm["cooldownUntil"] = (now + timedelta(minutes=room_cooldown_min)).isoformat(timespec="seconds")
        was_heating = t["isHeating"] or (tm.get("heatingStartedAt") is not None)
        if is_follower:
            # 추종 방 타이머는 기준 방과 동기 (가동 시작 시각·시작 바닥·잔열 휴지)
            lead_tm = self.room_timers.get(group["leader"], {})
            tm["cooldownUntil"] = lead_tm.get("cooldownUntil")
            if record.command_action == "HEAT":
                tm["heatingStartedAt"] = lead_tm.get("heatingStartedAt") or now.isoformat(timespec="seconds")
                tm["heatingStartFloor"] = lead_tm.get("heatingStartFloor")
            elif record.command_action == "OFF":
                tm["heatingStartedAt"] = None
                tm.pop("heatingStartFloor", None)
        elif record.command_action == "HEAT" and not tm.get("heatingStartedAt"):
            tm["heatingStartedAt"] = now.isoformat(timespec="seconds")
            tm["heatingStartFloor"] = guard_floor
        elif record.command_action == "OFF":
            # Enter residual-heat soak cooldown on ANY HEAT -> OFF transition (survives restarts)
            if was_heating and not is_manual and t["cdMin"] <= 0:
                tm["cooldownUntil"] = (now + timedelta(minutes=room_cooldown_min)).isoformat(timespec="seconds")
            tm["heatingStartedAt"] = None
            tm.pop("heatingStartFloor", None)

        if record.command_action in ("HEAT", "OFF"):
            # 채터링 방지 기준점: 명령 방향(HEAT<->OFF)이 바뀐 시각
            if last_cmd in ("HEAT", "OFF") and last_cmd != record.command_action:
                record.opposing_command_interval_sec = compute_opposing_interval(
                    last_cmd, record.command_action, secs_since_switch
                )
                tm["lastSwitchAt"] = now.isoformat(timespec="seconds")
            tm["lastCommandAction"] = record.command_action

            if boost_hold:
                # 외부 조작(월패드/자동화) 설정값 유지 — ITSS 는 전송하지 않음
                record.command_setpoint = _to_float((t.get("climate") or {}).get("setpoint"))
                record.dispatch_result = "NO_CHANGE"
            else:
                record.command_setpoint = self.compute_command_setpoint(record.command_action, c_state)
                await self._dispatch(rm["climateEntity"], record, c_state, tm, now)
        else:
            record.dispatch_result = "NONE"

        # 다음 회차 외부 조작 감지 기준: 이번 회차 최종 관측 가동 여부 (명령 후 재조회값 우선)
        observed = record.combustion_confirmed
        tm["lastObservedHeating"] = bool(observed) if observed is not None else bool(t["isHeating"])

        # 기준 방 결과를 추종 방에 전달 (명령 실패 포함 — 실패 시 추종 방 단독 가열 금지)
        if is_leader:
            group["result"] = {
                "action": record.command_action,
                "reason": decision_reason,
                "dispatch": record.dispatch_result,
                "maxRunMin": room_max_run_min,
            }

        if record.command_action in ("HEAT", "OFF"):
            if floor_src == "ESTIMATE_OFF":
                floor_label = "미사용(독립 제어)"
            elif is_follower:
                floor_label = f"{eff_floor}°C(추정 · {group['leader']} 기준 동시 제어)"
            elif eff_floor is not None:
                floor_label = f"{eff_floor}°C"
            else:
                floor_label = "--"
            extra: list[str] = []
            if (
                record.command_action == "HEAT" and boost_event == "START"
                and tm.get("externalBoostUntil") and boost_total_min is not None
            ):
                linked = f"({' · '.join(group['members'])} 연동)" if boost_grouped and group is not None else ""
                extra.append(
                    f"외부 조작 감지{linked}: {tm['externalBoostUntil'][11:16]}까지 {boost_total_min:g}분 유지 후 자동 중지"
                )
            if manual_heat and record.command_action == "HEAT" and tm.get("manualHeatSince"):
                m_lim = int(self.settings.get("manualHeatMaxMin", MANUAL_HEAT_DEFAULT_MIN))
                m_end = (_parse_ts(tm["manualHeatSince"]) or now) + timedelta(minutes=m_lim)
                extra.append(f"수동 난방: {m_end.strftime('%H:%M')}까지 최대 {m_lim}분 후 자동 제어 복귀")
            if manual_end:
                extra.append("수동 난방 종료 → 자동 제어(AUTO) 복귀")
            if group is not None:
                dev = group.get("weightedDevC")
                extra.append(
                    f"동시 제어: {' · '.join(group['members'])}"
                    + (f" (그룹 평균 편차 {dev:+.2f}°C)" if dev is not None else "")
                )
            await self._notify_state_change(
                room_name=r_name,
                action=record.command_action,
                reason_code=decision_reason,
                room_temp=t["roomTemp"],
                floor_temp=eff_floor,
                target_temp=rm["targetTemp"],
                # 그룹 방은 가중 평균 편차로 판정하므로 개별 P200 예측값은 알림에서 생략 (트레이스에는 기록)
                pred_room=None if group is not None else record.predicted_room_30m,
                setpoint=record.command_setpoint,
                run_min=prev_run_min,
                max_run_min=(
                    boost_total_min
                    if record.command_action == "HEAT" and boost_event in ("START", "ACTIVE") and boost_total_min is not None
                    else int(self.settings.get("manualHeatMaxMin", MANUAL_HEAT_DEFAULT_MIN)) if manual_heat
                    else room_max_run_min
                ),
                dispatch_result=record.dispatch_result,
                now=now,
                eff_target=t["effTarget"],
                floor_label=floor_label,
                extra_lines=extra,
            )

        top_reasons = self._update_underheat(
            tm,
            is_auto=not is_manual,
            room_temp=t["roomTemp"],
            eff_target=t["effTarget"],
            reason=decision_reason,
            now=now,
        )
        if top_reasons and self.settings.get("notifyEvents", True):
            reason_lines = "\n".join(f"- {REASON_KO.get(k, k)}: {n}회" for k, n in top_reasons)
            await self._safe_notify(
                f"🥶 I.T.S.S. 난방 부족 감지 ({r_name})",
                f"실내 {t['roomTemp']}°C 가 목표 {t['effTarget']}°C 보다 {UNDERHEAT_MARGIN_C}°C 이상 낮은 상태가 "
                f"{int(UNDERHEAT_ALERT_MIN)}분 이상 지속되었습니다.\n주요 차단/판단 사유:\n{reason_lines}",
            )

        record.saved_at = now_kst_iso()
        payload = record.to_payload()
        self.db.insert_cycle(payload)

        # ---- 실운전 분석용 로그/트레이스
        elapsed_ms = round((time.perf_counter() - t_room0) * 1000.0, 1)
        decision_key = f"{record.command_action}|{decision_reason}"
        if tm.get("lastDecisionKey") != decision_key:
            _LOG_CYCLE.info(
                "[%s] 판단 변경 %s -> %s | 실내 %s°C 바닥 %s°C(%s) 실효목표 %.2f°C 예측 %s°C | 설정 %s°C 전송 %s",
                r_name,
                tm.get("lastDecisionKey"),
                decision_key,
                t["roomTemp"],
                eff_floor,
                floor_src,
                t["effTarget"],
                record.predicted_room_30m,
                record.command_setpoint,
                record.dispatch_result,
            )
            tm["lastDecisionKey"] = decision_key
        _LOG_CYCLE.debug(
            "[%s] %s r=%s f=%s(%s) tgt=%.2f heat=%s run=%.1f/%.1f cd=%.1f vent=%.1f | vR=%+.4f rate=%+.4f vF=%+.4f "
            "d5R=%s tR=%s tF=%s lat=%.2f | age r/f=%s/%s stale r/f=%.0f/%.0f | p200=%s mz=%s p000=%s -> %s(%s) sp=%s %s",
            r_name, rm["controlMode"], t["roomTemp"], eff_floor, floor_src, t["effTarget"], t["isHeating"],
            t["runMin"], room_max_run_min, t["cdMin"], t["ventMin"],
            t["vRoomSmooth"], t["vRoomRate"], t["vFloor"],
            t["drop5mRoom"], t["trend10mRoom"], t["trend10mFloor"], t["floorLatency"],
            t["roomAge"], floor_age_eff, t["roomStaleSec"], floor_stale_sec,
            f"{pred.recommended_action}({pred.predicted_room_temp})" if pred else None,
            f"{mz_cmd.action}({mz_cmd.reason})" if mz_cmd else None,
            verdict.reason_code if verdict else None,
            record.command_action, decision_reason, record.command_setpoint, record.dispatch_result,
        )
        self.trace.write(
            {
                "ts": exec_started,
                "cycleId": cycle_id,
                "room": r_name,
                "mode": rm["controlMode"],
                "manualAction": rm["manualAction"],
                "cycle": cycle_ctx,
                "inputs": {
                    "roomTemp": t["roomTemp"],
                    "roomTempRaw": t.get("roomTempRaw"),
                    "roomTempOffsetC": t.get("roomTempOffsetC"),
                    "roomReportAt": t["roomTs"],
                    "roomAgeSec": t["roomAge"],
                    "roomTsSource": t.get("roomTsSource"),
                    "roomIntervalSec": t["roomInterval"],
                    "roomMaxIntervalSec": t.get("roomMaxInterval"),
                    "roomStaleSec": t["roomStaleSec"],
                    "floorTemp": eff_floor,
                    "guardFloor": guard_floor,
                    "floorSrc": floor_src,
                    "floorEstimateEnabled": t["floorEstimateEnabled"],
                    "floorOffsetC": rm["floorOffsetC"],
                    "floorAgeSec": floor_age_eff,
                    "floorStaleSec": floor_stale_sec,
                    "targetTemp": rm["targetTemp"],
                    "effTarget": t["effTarget"],
                    "climate": t.get("climate"),
                    "isHeating": t["isHeating"],
                    "combustion": t["combustion"],
                    "windowOpen": t["windowOpen"],
                },
                "dynamics": {
                    "vRoomSmooth": round(t["vRoomSmooth"], 4),
                    "vRoomRate": round(t["vRoomRate"], 4),
                    "vFloor": round(t["vFloor"], 4),
                    "drop5mRoom": t["drop5mRoom"],
                    "drop5mFloor": t["drop5mFloor"],
                    "trend10mRoom": t["trend10mRoom"],
                    "trend10mFloor": t["trend10mFloor"],
                    "floorLatencyC": t["floorLatency"],
                },
                "limits": {
                    "overheatRoomC": DEFAULT_OVERHEAT_ROOM_C,
                    "overheatFloorC": DEFAULT_OVERHEAT_FLOOR_C,
                    "effFloorLimitC": eff_floor_limit,
                    "floorCapC": floor_cap if guard_floor is not None else None,
                    "floorRiseRateCPerMin": floor_rate if guard_floor is not None else None,
                    "heatingStartFloorC": tm.get("heatingStartFloor"),
                    "learnedFloorSurgeC": learned_floor_surge,
                    "baseMaxRunMin": base_room_max_run,
                    "roomMaxRunMin": room_max_run_min,
                    "cooldownMin": room_cooldown_min,
                    "pollIntervalMin": poll_interval_min,
                },
                "group": (
                    {
                        "role": group_role,
                        "leader": group["leader"],
                        "members": group["members"],
                        "demand": group.get("demand"),
                        "demandReason": group.get("demandReason"),
                        "weightedDevC": group.get("weightedDevC"),
                        "leaderResult": group.get("result") if is_follower else None,
                    }
                    if group is not None
                    else None
                ),
                "lagProfile": {
                    k: lag_prof.get(k)
                    for k in (
                        "status", "sampleCount", "floorResidualRiseC", "roomResidualRiseC",
                        "recommendedCooldownMin", "recommendedHorizonMin", "recommendedRunTrimMin",
                        "floorRiseRateCPerMin", "floorRateSamples",
                    )
                },
                "timersBefore": timers_before,
                "externalBoost": (
                    {"event": boost_event, "until": tm.get("externalBoostUntil")} if boost_event else None
                ),
                "p200": (
                    {
                        "model": pred.model_name,
                        "action": pred.recommended_action,
                        "predRoom": pred.predicted_room_temp,
                        "predFloor": pred.predicted_floor_temp,
                        "effTarget": pred.effective_target_temp,
                        "learning": pred.residual_learning_status,
                        "residualRiseC": pred.estimated_residual_rise_c,
                        "diag": pred.diagnostics,
                    }
                    if pred
                    else None
                ),
                "multiZone": {"action": mz_cmd.action, "reason": mz_cmd.reason} if mz_cmd else None,
                "sentinel": (
                    {
                        "reason": verdict.reason_code,
                        "forceOff": verdict.force_off,
                        "hold": verdict.hold_current_state,
                        "message": verdict.message,
                    }
                    if verdict
                    else None
                ),
                "decision": {
                    "action": record.command_action,
                    "reason": decision_reason,
                    "executionStatus": record.execution_status,
                    "antiChatterBlocked": record.diagnostics.get("antiChatterBlocked"),
                },
                "command": {
                    "setpoint": record.command_setpoint,
                    "dispatch": record.dispatch_result,
                    "calls": record.diagnostics.get("dispatchCalls"),
                    "issuedAt": record.control_issued_at,
                    "confirmedAt": record.control_confirmed_at,
                    "observedAt": record.observed_at,
                    "deviceModeAfter": record.device_mode_event,
                    "runningAfter": record.combustion_confirmed,
                    "opposingIntervalSec": record.opposing_command_interval_sec,
                },
                "timersAfter": {
                    k: tm.get(k)
                    for k in ("heatingStartedAt", "cooldownUntil", "ventilationUntil", "lastSwitchAt", "underheatSince")
                },
                "elapsedMs": elapsed_ms,
            },
            now,
        )
        return payload


def create_app(
    db_path: str | None = None,
    ha_client: HomeAssistantClient | None = None,
    notifier: PushNotifier | None = None,
    weather_client: Any = None,
    options: dict[str, Any] | None = None,
    allowed_client_ips: Iterable[str] | None = None,
) -> FastAPI:
    opts = options if options is not None else _load_options()
    configure_logging(opts.get("log_level"))
    engine = ItssEngine(
        db_path=db_path,
        ha_client=ha_client,
        notifier=notifier,
        weather_client=weather_client,
        options=opts,
    )
    # 텍스트 로그 파일: <data>/logs/itss.log (2MB x 5개 회전)
    engine.log_path = configure_logging(opts.get("log_level"), os.path.join(engine.data_dir, "logs"))
    allowed_ips = _resolve_allowed_ips(allowed_client_ips)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        _LOGGER.info(
            "Starting I.T.S.S. Thermal SCADA v%s (build=%s, interval=%ss, dryRun=%s, haConnected=%s, "
            "floorConfigured=%s, allowedIps=%s)",
            VERSION,
            os.environ.get("ITSS_BUILD_VERSION", VERSION),
            engine.settings["updateIntervalSec"],
            engine.settings["dryRun"],
            engine.ha.is_configured,
            engine.floor_configured,
            "ANY" if allowed_ips is None else ",".join(sorted(allowed_ips)),
        )
        _LOGGER.info(
            "로그 레벨=%s, 로그 파일=%s, 트레이스=%s (보존 %d일, 경로 %s)",
            logging.getLevelName(logging.getLogger("itss").level),
            engine.log_path,
            "ON" if engine.trace.enabled else "OFF",
            engine.trace.retention_days,
            engine.trace.trace_dir,
        )
        if not engine.floor_configured:
            _LOGGER.warning("마스터 바닥 온도 센서가 설정되지 않아 AUTO 난방이 차단됩니다.")
        stop_event = asyncio.Event()

        async def _loop():
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=engine.settings["updateIntervalSec"])
                except asyncio.TimeoutError:
                    try:
                        await engine.run_control_cycle()
                    except Exception:  # noqa: BLE001
                        # run_control_cycle 내부에서 로깅·연속 실패 집계·경보 처리 완료
                        pass
                    try:
                        engine.maybe_start_daily_report()
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("일일 리포트 예약 확인 실패")

        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            stop_event.set()
            task.cancel()
            if engine._report_task is not None and not engine._report_task.done():
                engine._report_task.cancel()

    app = FastAPI(title="I.T.S.S. Thermal SCADA", version=VERSION, lifespan=lifespan)
    app.state.engine = engine
    app.state.allowed_ips = allowed_ips

    no_cache_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.middleware("http")
    async def disable_ingress_cache(request: Request, call_next):
        response = await call_next(request)
        for k, v in no_cache_headers.items():
            response.headers[k] = v
        return response

    @app.middleware("http")
    async def ingress_ip_guard(request: Request, call_next):
        """HA 애드온 보안 지침: Ingress 프록시(172.30.32.2) 외 접속 거부."""
        if allowed_ips is not None:
            client_ip = _normalize_ip(request.client.host if request.client else None)
            if client_ip not in allowed_ips:
                _LOGGER.warning("허용되지 않은 접속 차단: %s %s", client_ip, request.url.path)
                return JSONResponse(status_code=403, content={"detail": "Forbidden: HA Ingress only"})
        return await call_next(request)

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers=no_cache_headers)

    @app.get("/health")
    def health() -> dict[str, Any]:
        h = engine.health_status()
        return {
            "status": h["status"],
            "problems": h["problems"],
            "warnings": h["warnings"],
            "appVersion": VERSION,
            "timeKst": now_kst_iso(),
            "haConnected": engine.ha.is_configured,
            "tokenSource": getattr(engine.ha, "token_source", "mock"),
            "lastCycleOkAt": h["lastCycleOkAt"],
            "consecutiveFailures": h["consecutiveFailures"],
        }

    @app.get("/api/v1/status")
    async def get_status() -> dict[str, Any]:
        rooms = engine.db.list_rooms()
        now_kst = datetime.now(tz=KST)
        since24 = (now_kst - timedelta(hours=24)).isoformat(timespec="seconds")
        stats_rows = engine.db.stats_rows_since(since24)
        latest_by_room = engine.db.latest_cycle_by_room([r["roomName"] for r in rooms])

        sched_offset, sched_label = engine.db.active_schedule_offset(now_kst.hour)
        master_floor = None
        if engine.floor_configured:
            master_floor, _, _, _ = await engine._read_numeric(engine.settings["masterFloorEntity"])
        outdoor_temp, wind_speed, outdoor_source = await engine._read_outdoor_weather()
        atmos_offset = engine.atmos.compute_offset(outdoor_temp=outdoor_temp, wind_speed_ms=wind_speed, now_kst=now_kst)
        active_max_run = engine.atmos.compute_max_run_minutes(
            outdoor_temp=outdoor_temp,
            now_kst=now_kst,
            hard_cap_min=float(engine.settings["maxRunCapMin"]),
        )
        live_by_room: dict[str, dict[str, Any]] = {}
        # 동시 제어 그룹 구성 (제어 루프와 같은 규칙: AUTO + 실측/추정 방, 기준 방 = 바닥 센서 방)
        floor_srcs = {
            rm["roomName"]: engine._room_floor(rm, master_floor, engine.floor_configured)[1] for rm in rooms
        }
        live_group = ItssEngine._floor_group_plan(
            rooms,
            {
                n: {
                    "floorSrc": s,
                    "externalBoost": (engine.room_timers.get(n) or {}).get("externalBoostUntil"),
                    "externalBoostGroup": (engine.room_timers.get(n) or {}).get("externalBoostGroup"),
                }
                for n, s in floor_srcs.items()
            },
        )
        for rm in rooms:
            r_name = rm["roomName"]
            r_raw, _, _, _ = await engine._read_numeric(rm["roomTempEntity"])
            r_off = float(rm.get("roomTempOffsetC") or 0.0)
            r_temp = round(r_raw + r_off, 2) if r_raw is not None else None
            c_state = await engine.ha.get_state(rm["climateEntity"])
            eff_floor, floor_src, guard_floor = engine._room_floor(rm, master_floor, engine.floor_configured)
            c_attrs = (c_state.get("attributes") or {}) if c_state else {}
            run_min, cd_min, vent_min = engine._timer_metrics(r_name, now_kst)
            win_open = False
            win_ent = str(rm.get("windowSensorEntity") or "").strip()
            if win_ent:
                w_raw = await engine.ha.get_state(win_ent)
                if w_raw:
                    win_open = str(w_raw.get("state") or "").lower() in ("on", "open")
            eff_target_live = round(
                rm["targetTemp"] + atmos_offset + sched_offset + PRESET_OFFSETS.get(engine.active_preset, 0.0), 2
            )
            eff_floor_cap = engine._compute_effective_floor_limit(eff_target_live, outdoor_temp)
            lag_p = engine.lag_profiles.get(r_name) or {}
            tm = engine.room_timers.get(r_name, {})
            rate_live = float(lag_p.get("floorRiseRateCPerMin") or DEFAULT_FLOOR_RISE_RATE_C_PER_MIN)
            base_live = max(
                MIN_PULSE_MIN, min(15.0, active_max_run - float(lag_p.get("recommendedRunTrimMin", 0.0)))
            )
            start_live = tm.get("heatingStartFloor") if tm.get("heatingStartedAt") else None
            eff_max_run = (
                engine._compute_floor_limited_max_run(
                    base_max_run_min=base_live,
                    start_floor_c=start_live if start_live is not None else guard_floor,
                    floor_cap_c=engine.floor_cap_c(eff_floor_cap),
                    rise_rate_c_per_min=rate_live,
                )
                if guard_floor is not None
                else base_live
            )
            g_role = None
            if live_group and r_name in live_group["members"]:
                g_role = "LEADER" if r_name == live_group["leader"] else "FOLLOWER"
            elif floor_src == "ESTIMATE_OFF":
                g_role = "INDEPENDENT"
            uh_since = _parse_ts(tm.get("underheatSince"))
            live_by_room[r_name] = {
                "roomTemp": r_temp,
                "roomTempRaw": r_raw,
                "roomTempOffsetC": r_off,
                "floorTemp": eff_floor,
                "floorSrc": floor_src,
                "guardFloorTemp": guard_floor,
                "effectiveTargetTemp": eff_target_live,
                "climateState": c_state.get("state") if c_state else None,
                "climateSetpoint": c_attrs.get("temperature"),
                "climateCurrentTemp": c_attrs.get("current_temperature"),
                "boilerRunning": detect_boiler_running(c_state),
                "runtimeMin": run_min,
                "effectiveMaxRunMin": eff_max_run,
                "effectiveFloorLimitC": eff_floor_cap,
                "floorRiseRateCPerMin": rate_live if guard_floor is not None else None,
                "groupRole": g_role,
                "groupLeader": live_group["leader"] if g_role in ("LEADER", "FOLLOWER") else None,
                "cooldownRemainMin": cd_min,
                "ventilationRemainMin": vent_min,
                "windowOpen": win_open,
                "underheatMin": round((now_kst - uh_since).total_seconds() / 60.0, 1) if uh_since else 0.0,
                "externalBoostUntil": tm.get("externalBoostUntil"),
                "manualHeatUntil": (
                    (_parse_ts(tm.get("manualHeatSince")) + timedelta(
                        minutes=int(engine.settings.get("manualHeatMaxMin", MANUAL_HEAT_DEFAULT_MIN))
                    )).isoformat(timespec="seconds")
                    if tm.get("manualHeatSince") and _parse_ts(tm.get("manualHeatSince")) is not None
                    else None
                ),
            }

        first_ts = _parse_ts(stats_rows[0]["executionStartedAt"]) if stats_rows else None
        coverage_h = round((now_kst - first_ts).total_seconds() / 3600.0, 1) if first_ts else 0.0
        return {
            "appVersion": VERSION,
            "timeKst": now_kst_iso(),
            "haConnected": engine.ha.is_configured,
            "tokenSource": getattr(engine.ha, "token_source", "mock"),
            "haLastError": getattr(engine.ha, "last_error", None),
            "floorConfigured": engine.floor_configured,
            "masterFloorTemp": master_floor,
            "outdoorTemp": outdoor_temp,
            "outdoorTempSource": outdoor_source,
            "activeMaxRunMin": active_max_run,
            "activePreset": engine.active_preset,
            "activeSchedule": {"label": sched_label, "offsetC": sched_offset},
            "settings": engine.settings,
            "rooms": rooms,
            "liveByRoom": live_by_room,
            "latestByRoom": latest_by_room,
            "statsWindowHours": 24,
            "statsCoverageHours": coverage_h,
            "kpis": compute_daily_kpis(stats_rows),
            "boilerStats": compute_boiler_stats(
                stats_rows,
                interval_sec=engine.settings["updateIntervalSec"],
                gas_krw_per_hour=engine.settings["gasKrwPerHour"],
            ),
            "sensorHealth": engine.sensor_health(now_kst),
            "controlLoop": engine.health_status(),
            "fittedRcParams": engine.db.get_kv("fitted_rc_params"),
            "suggestedFloorOffsets": engine.db.get_kv("suggested_floor_offsets"),
            "thermalLagProfiles": engine.lag_profiles,
            "comfort": engine.comfort_analysis(now_kst),
            "dailyReportLast": engine.db.get_kv("daily_report_last"),
        }

    @app.get("/api/v1/ha/entities")
    async def list_ha_entities() -> dict[str, Any]:
        return await engine.ha.list_entities()

    @app.get("/api/v1/ha/diagnose")
    async def diagnose_ha() -> dict[str, Any]:
        """Return live diagnostic dump of rooms, climate attributes, sensors, and HA services."""
        rooms = engine.db.list_rooms()
        room_diags = []
        for rm in rooms:
            r_raw = await engine.ha.get_state(rm["roomTempEntity"])
            c_raw = await engine.ha.get_state(rm["climateEntity"])
            room_diags.append({
                "roomName": rm["roomName"],
                "controlMode": rm["controlMode"],
                "roomTempEntity": rm["roomTempEntity"],
                "roomTempState": r_raw.get("state") if r_raw else None,
                "roomTempLastReported": r_raw.get("last_reported") if r_raw else None,
                "climateEntity": rm["climateEntity"],
                "climateState": c_raw.get("state") if c_raw else None,
                "climateAttrs": (c_raw.get("attributes") or {}) if c_raw else None,
            })
        mf_raw = await engine.ha.get_state(engine.settings["masterFloorEntity"]) if engine.floor_configured else None
        services = await engine.ha.list_services() if hasattr(engine.ha, "list_services") else {}
        return {
            "haConnected": engine.ha.is_configured,
            "tokenSource": getattr(engine.ha, "token_source", "mock"),
            "dryRun": engine.settings["dryRun"],
            "heatOnSetpoint": engine.settings["heatOnSetpoint"],
            "heatOffSetpoint": engine.settings["heatOffSetpoint"],
            "masterFloorEntity": engine.settings["masterFloorEntity"],
            "masterFloorState": mf_raw.get("state") if mf_raw else None,
            "masterFloorLastReported": mf_raw.get("last_reported") if mf_raw else None,
            "lastError": getattr(engine.ha, "last_error", None),
            "controlLoop": engine.health_status(),
            "sensorHealth": engine.sensor_health(),
            "rooms": room_diags,
            "availableServices": services,
            "notifyConfig": {
                "notifyService": engine.notifier.notify_service,
                "telegramChatId": engine.notifier.telegram_chat_id,
            },
        }

    @app.post("/api/v1/rooms/{room_name}/test-climate")
    async def test_room_climate(room_name: str, req: TestClimateRequest) -> dict[str, Any]:
        """Directly dispatch a live HEAT/OFF test to one room's climate entity (제어 회차와 직렬화)."""
        rm = engine.db.get_room(room_name)
        if not rm:
            raise HTTPException(status_code=404, detail="Room not found")
        cid = rm["climateEntity"]
        async with engine.cycle_lock:
            before = await engine.ha.get_state(cid)
            setpoint = engine.compute_command_setpoint(req.action, before)
            steps: list[dict[str, Any]] = []

            if req.action == "HEAT":
                try:
                    await engine.ha.call_service("climate", "set_hvac_mode", {"entity_id": cid, "hvac_mode": "heat"})
                    steps.append({"call": "climate.set_hvac_mode(heat)", "status": "ok"})
                    await asyncio.sleep(0.8)
                except Exception as err:  # noqa: BLE001
                    steps.append({"call": "climate.set_hvac_mode(heat)", "status": f"error:{err}"})

            try:
                await engine.ha.call_service("climate", "set_temperature", {"entity_id": cid, "temperature": setpoint})
                steps.append({"call": f"climate.set_temperature({setpoint})", "status": "ok"})
            except Exception as err:  # noqa: BLE001
                steps.append({"call": f"climate.set_temperature({setpoint})", "status": f"error:{err}"})

            await asyncio.sleep(DEFAULT_RECHECK_DELAY_SEC)
            after = await engine.ha.get_state(cid)
        return {
            "roomName": room_name,
            "climateEntity": cid,
            "action": req.action,
            "targetSetpoint": setpoint,
            "before": {
                "state": before.get("state") if before else None,
                "temperature": (before.get("attributes") or {}).get("temperature") if before else None,
            },
            "steps": steps,
            "after": {
                "state": after.get("state") if after else None,
                "temperature": (after.get("attributes") or {}).get("temperature") if after else None,
                "current_temperature": (after.get("attributes") or {}).get("current_temperature") if after else None,
                "boiler_running": detect_boiler_running(after),
            },
        }

    @app.get("/api/v1/rooms")
    def list_rooms() -> list[dict[str, Any]]:
        return engine.db.list_rooms()

    @app.post("/api/v1/rooms")
    def create_or_update_room(req: RoomCreateRequest) -> dict[str, Any]:
        data = req.model_dump()
        if data.get("roomTempOffsetC") is None:
            prev = engine.db.get_room(req.roomName)
            data["roomTempOffsetC"] = float((prev or {}).get("roomTempOffsetC") or 0.0)
        return engine.db.upsert_room(data)

    @app.patch("/api/v1/rooms/{room_name}")
    async def patch_room(room_name: str, req: RoomPatchRequest) -> dict[str, Any]:
        before = engine.db.get_room(room_name)
        if not before:
            raise HTTPException(status_code=404, detail="Room not found")
        if before["hasFloorSensor"] and (req.floorEstimateEnabled is not None or req.floorOffsetC is not None):
            raise HTTPException(
                status_code=409,
                detail="바닥 센서(마스터) 방은 실측값을 사용하므로 바닥 추정/offset 을 설정할 수 없습니다.",
            )
        updated = engine.db.update_room_control(
            room_name,
            target_temp=req.targetTemp,
            control_mode=req.controlMode,
            manual_action=req.manualAction,
            floor_offset_c=req.floorOffsetC,
            floor_estimate_enabled=req.floorEstimateEnabled,
            room_temp_offset_c=req.roomTempOffsetC,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Room not found")
        if float(updated.get("roomTempOffsetC") or 0.0) != float(before.get("roomTempOffsetC") or 0.0):
            _LOGGER.info(
                "[%s] 실내 센서 보정값 변경 %+.2f -> %+.2f°C", room_name,
                float(before.get("roomTempOffsetC") or 0.0), float(updated["roomTempOffsetC"]),
            )
        if updated["floorEstimateEnabled"] != before["floorEstimateEnabled"]:
            _LOGGER.info(
                "[%s] 바닥 추정 %s (offset %+.2f°C)",
                room_name,
                "ON: 마스터 실측 + offset 사용" if updated["floorEstimateEnabled"] else "OFF: 실내 기준 제어, 안전은 마스터 실측 대리 판정",
                updated["floorOffsetC"],
            )
        if updated["floorOffsetC"] != before["floorOffsetC"]:
            _LOGGER.info("[%s] 바닥 추정 offset 변경 %+.2f -> %+.2f°C", room_name, before["floorOffsetC"], updated["floorOffsetC"])
        # 수동/자동 전환 시 즉시 1회 제어
        if req.controlMode is not None or req.manualAction is not None:
            await engine.run_control_cycle()
        return updated

    @app.delete("/api/v1/rooms/{room_name}")
    async def delete_room(room_name: str) -> dict[str, Any]:
        async with engine.cycle_lock:
            if not engine.db.delete_room(room_name):
                raise HTTPException(status_code=404, detail="Room not found")
            engine.room_timers.pop(room_name, None)
            engine.db.set_kv("room_timers", engine.room_timers)
        return {"deleted": room_name}

    @app.patch("/api/v1/settings")
    async def patch_settings(req: SettingsPatchRequest) -> dict[str, Any]:
        patch = req.model_dump(exclude_none=True)
        if "masterFloorEntity" in patch:
            patch["masterFloorEntity"] = patch["masterFloorEntity"].strip()
        if "maxRunCapMin" in patch:
            patch["maxRunCapMin"] = int(max(MIN_PULSE_MIN, patch["maxRunCapMin"]))
        merged = {**engine.settings, **patch}
        if float(merged["heatOffSetpoint"]) > float(merged["heatOnSetpoint"]) - 3.0:
            raise HTTPException(
                status_code=422,
                detail="OFF 설정온도는 ON 설정온도보다 3°C 이상 낮아야 합니다.",
            )
        if not merged["dryRun"] and not str(merged.get("masterFloorEntity") or "").strip():
            raise HTTPException(
                status_code=409,
                detail="마스터 바닥 온도 센서를 설정해야 실제 보일러 제어(LIVE)로 전환할 수 있습니다.",
            )
        async with engine.cycle_lock:
            was_dry = bool(engine.settings["dryRun"])
            engine.settings.update(patch)
            if "weatherLocation" in patch and hasattr(engine.weather_client, "set_location"):
                engine.weather_client.set_location(patch["weatherLocation"])
            engine.db.set_kv("settings", engine.settings)
        if was_dry != bool(engine.settings["dryRun"]):
            _LOGGER.warning("운전 모드 변경: %s", "LIVE(실제 보일러 제어)" if not engine.settings["dryRun"] else "시험 운전")
        return engine.settings

    @app.post("/api/v1/preset")
    async def set_preset(req: PresetRequest) -> dict[str, Any]:
        async with engine.cycle_lock:
            res = engine.apply_preset(req.preset)
        if engine.settings.get("notifyEvents", True):
            await engine._safe_notify(
                "🎛 I.T.S.S. 프리셋 변경",
                f"프리셋이 '{req.preset}' (목표 오프셋 {res['offsetC']}°C)으로 변경되었습니다.",
            )
        await engine.run_control_cycle()
        return res

    @app.get("/api/v1/schedules")
    def get_schedules() -> list[dict[str, Any]]:
        return engine.db.list_schedules()

    @app.put("/api/v1/schedules")
    def update_schedules(req: SchedulesUpdateRequest) -> list[dict[str, Any]]:
        return engine.db.replace_schedules([s.model_dump() for s in req.slots])

    @app.post("/api/v1/calibrate/auto")
    def calibrate_auto() -> dict[str, Any]:
        return engine.calibrate_rc_and_offsets()

    @app.post("/api/v1/notify/test")
    async def test_notify() -> dict[str, Any]:
        return await engine.notifier.send("I.T.S.S. 테스트 알림", "모바일 푸시 알림 연동이 정상 작동합니다.")

    @app.post("/api/v1/control/run-now")
    async def run_now() -> dict[str, Any]:
        records = await engine.run_control_cycle()
        return {"executed": len(records), "records": records}

    @app.get("/api/v1/cycles")
    def get_cycles(
        room_name: str | None = Query(default=None, max_length=40),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        items = engine.db.list_cycles(room_name=room_name, limit=limit)
        return {"count": len(items), "items": items}

    @app.get("/api/v1/chart")
    def get_chart(
        room_name: str = Query(..., min_length=1, max_length=40),
        hours: int = Query(default=24, ge=1, le=168),
        max_points: int = Query(default=360, ge=50, le=2000),
    ) -> dict[str, Any]:
        since = (datetime.now(tz=KST) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = engine.db.chart_rows_since(room_name, since)
        return {
            "roomName": room_name,
            "hours": hours,
            "rawCount": len(rows),
            "points": _downsample_chart(rows, max_points),
        }

    @app.post("/api/v1/report/daily")
    async def daily_report(send: bool = Query(default=False)) -> dict[str, Any]:
        """지난 24시간 리포트 (Gemini 키 없으면 KPI 요약만). send=true 면 알림으로도 발송."""
        return await engine.create_daily_report(send=send)

    # ---------------- 쾌적 학습 (제안 전용)
    @app.get("/api/v1/comfort")
    def get_comfort(limit: int = Query(default=30, ge=1, le=500)) -> dict[str, Any]:
        res = engine.comfort_analysis()
        res["events"] = engine.comfort_events[-limit:][::-1]
        return res

    @app.post("/api/v1/comfort/reset")
    def reset_comfort() -> dict[str, Any]:
        n = len(engine.comfort_events)
        engine.comfort_events = []
        engine.db.set_kv("comfort_events", [])
        _LOGGER.info("쾌적 학습 이벤트 초기화 (%d건 삭제)", n)
        return {"deleted": n}

    # ---------------- 실내 센서 동시 비교 측정 -> 보정값 계산 (적용은 방 PATCH)
    @app.post("/api/v1/calibration/room-sensors")
    def calibrate_room_sensors(req: SensorCalibrationRequest) -> dict[str, Any]:
        start, end = _parse_ts(req.start), _parse_ts(req.end)
        if start is None or end is None:
            raise HTTPException(status_code=422, detail="start/end 는 ISO 시각이어야 합니다")
        if start.tzinfo is None:
            start = start.replace(tzinfo=KST)
        if end.tzinfo is None:
            end = end.replace(tzinfo=KST)
        if (end - start) < timedelta(hours=CALIBRATION_MIN_SPAN_H):
            raise HTTPException(status_code=422, detail=f"측정 구간은 {CALIBRATION_MIN_SPAN_H:g}시간 이상이어야 합니다")
        if req.reference and engine.db.get_room(req.reference) is None:
            raise HTTPException(status_code=404, detail=f"기준 방 없음: {req.reference}")
        return engine.compute_sensor_offsets(
            start, end, reference=req.reference, reference_temp_c=req.referenceTempC
        )

    # ---------------- 실운전 분석용 디버그 엔드포인트 (Ingress 전용)
    @app.get("/api/v1/debug/logs")
    def debug_logs(lines: int = Query(default=300, ge=10, le=5000)) -> dict[str, Any]:
        """텍스트 로그 파일 마지막 N줄."""
        return {"logPath": engine.log_path, "lines": tail_log(engine.log_path, lines)}

    @app.get("/api/v1/debug/trace/files")
    def debug_trace_files() -> dict[str, Any]:
        return {
            "enabled": engine.trace.enabled,
            "retentionDays": engine.trace.retention_days,
            "dir": engine.trace.trace_dir,
            "lastError": engine.trace.last_error,
            "files": engine.trace.list_files(),
        }

    @app.get("/api/v1/debug/trace")
    def debug_trace(
        date: str = Query(default="today", pattern=r"^(today|\d{8})$"),
        room_name: str | None = Query(default=None, max_length=40),
        limit: int | None = Query(default=None, ge=1, le=200000),
    ):
        """일자별 판단 근거 JSONL 다운로드 (pandas.read_json(lines=True) 로 분석)."""
        day = datetime.now(tz=KST).strftime("%Y%m%d") if date == "today" else date
        lines = engine.trace.read(day, room_name=room_name, limit=limit)
        if lines is None:
            raise HTTPException(status_code=404, detail=f"트레이스 파일 없음: {day}")
        return PlainTextResponse(
            "\n".join(lines) + ("\n" if lines else ""),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="itss-trace-{day}.jsonl"'},
        )

    @app.post("/api/v1/rc/fit")
    def fit_rc(req: RCFitRequest) -> dict[str, Any]:
        try:
            obs = [
                RCObservation(
                    dt_sec=float(o["dtSec"]),
                    floor_temp=float(o["floorTemp"]),
                    room_temp=float(o["roomTemp"]),
                    outdoor_temp=float(o["outdoorTemp"]),
                    heating_on=bool(o["heatingOn"]),
                    next_floor_temp=float(o["nextFloorTemp"]),
                    next_room_temp=float(o["nextRoomTemp"]),
                )
                for o in req.observations
            ]
        except (KeyError, TypeError, ValueError) as err:
            raise HTTPException(status_code=422, detail=f"Invalid observation: {err}") from err
        return fit_rc_parameters(obs)

    return app
