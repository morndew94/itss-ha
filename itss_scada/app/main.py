"""FastAPI SCADA Server & Ingress Web UI for the I.T.S.S. Home Assistant Add-on (v2.0)."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .const import (
    DEFAULT_RECHECK_DELAY_SEC,
    KST,
    MODEL_ADAPTIVE_RC,
    MODEL_LINEAR_V12,
    STRATEGY_HYBRID,
)
from .ha_client import HomeAssistantClient
from .integrations.google_alerts import GeminiTTSAlert
from .integrations.google_gemini_advisor import GeminiThermalAdvisor, compute_daily_kpis
from .integrations.google_weather import FallbackWeatherProvider
from .integrations.notifications import PushNotifier
from .models.schema import CycleRecord, SensorSnapshot, ThermalInput, now_kst_iso
from .protocols.multi_zone import MultiZoneController, ZoneReading
from .protocols.p000_sentinel import SentinelConfig, SentinelProtocol
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

STATIC_DIR = Path(__file__).parent / "static"

PRESET_OFFSETS = {
    "AUTO_ALL": 0.0,
    "AWAY": -2.0,
    "SLEEP": -0.3,
    "PREHEAT": 0.2,
    "ALL_OFF": 0.0,
}

REASON_KO = {
    "P000_OVERHEAT_CUTOFF": "긴급 과열 차단(실내 >= 27°C 또는 바닥 >= 29°C)",
    "P000_FLOOR_LIMIT_OFF": "바닥 온도 상한 도달(잔열 과다 방지 차단)",
    "P000_MAX_RUN_CUTOFF": "외기 연동 1회 최대 가동 시간 도달(잔열 휴지 진입)",
    "P000_RESIDUAL_COOLDOWN": "차단 후 잔열 흡수 대기 중",
    "P000_VENTILATION_PROTECT": "환기 급락 감지(난방 일시 중지)",
    "P000_SENSOR_MISSING": "온도 센서 값 누락으로 안전 차단",
    "P000_SENSOR_STALE": "온도 센서 장시간 미갱신으로 안전 차단",
    "P000_ANTI_CHATTER": "최소 가동/휴지 시간 유지(채터링 방지)",
    "P000_MANUAL_OVERRIDE": "사용자 수동 운전 조작",
    "P200_PREDICTIVE_ON": "P200 예측 온도가 목표 하한 이하 하락 예상(예측 기동)",
    "P200_PREDICTIVE_OFF": "P200 30분 예측/잔열이 목표 상한 도달 예상(예측 선차단)",
    "HYBRID_COHEAT_BATCH": "타 방 보일러 가동 중 동시 난방 합류(단펄스 방지)",
    "HYBRID_EARLY_CUTOFF": "목표 상한 도달(개별 선차단)",
    "SIMULTANEOUS_WEIGHTED_DEMAND": "전체 방 가중평균 하한 도달(동시 기동)",
    "SIMULTANEOUS_WEIGHTED_TARGET_REACHED": "전체 방 가중평균 상한 도달(동시 차단)",
}


def _load_options() -> dict[str, Any]:
    opt_path = os.environ.get("ITSS_OPTIONS_PATH", "/data/options.json")
    if os.path.exists(opt_path):
        try:
            with open(opt_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    return {}


class RoomCreateRequest(BaseModel):
    roomName: str = Field(..., min_length=1)
    roomTempEntity: str = Field(..., min_length=1)
    climateEntity: str = Field(..., min_length=1)
    hasFloorSensor: bool = False
    floorOffsetC: float = 0.0
    targetTemp: float = Field(default=23.0, ge=10.0, le=27.0)
    controlMode: str = Field(default="AUTO", pattern="^(AUTO|MANUAL)$")
    manualAction: str = Field(default="HOLD", pattern="^(HEAT|OFF|HOLD)$")
    weight: float = Field(default=1.0, gt=0.0)


class RoomPatchRequest(BaseModel):
    targetTemp: float | None = Field(default=None, ge=10.0, le=27.0)
    controlMode: str | None = Field(default=None, pattern="^(AUTO|MANUAL)$")
    manualAction: str | None = Field(default=None, pattern="^(HEAT|OFF|HOLD)$")
    floorOffsetC: float | None = None


class SettingsPatchRequest(BaseModel):
    dryRun: bool | None = None
    zoneStrategy: str | None = Field(default=None, pattern="^(hybrid|simultaneous)$")
    predictorModel: str | None = Field(default=None, pattern="^(linear_v12|adaptive_rc)$")
    masterFloorEntity: str | None = None
    outdoorTempEntity: str | None = None
    weatherLocation: str | None = None
    heatOnSetpoint: float | None = Field(default=None, ge=15.0, le=30.0)
    heatOffSetpoint: float | None = Field(default=None, ge=5.0, le=25.0)
    floorOffLimitC: float | None = Field(default=None, ge=22.0, le=29.0)
    maxRunCapMin: int | None = Field(default=None, ge=3, le=15)
    cooldownMin: int | None = Field(default=None, ge=5, le=90)
    gasKrwPerHour: int | None = Field(default=None, ge=100, le=10000)
    notifyEvents: bool | None = None


class PresetRequest(BaseModel):
    preset: str = Field(..., pattern="^(AUTO_ALL|AWAY|SLEEP|PREHEAT|ALL_OFF)$")


class ScheduleSlot(BaseModel):
    slotId: str
    label: str
    startHour: int = Field(..., ge=0, le=23)
    endHour: int = Field(..., ge=0, le=23)
    offsetC: float = Field(..., ge=-5.0, le=3.0)
    enabled: bool = True


class SchedulesUpdateRequest(BaseModel):
    slots: list[ScheduleSlot]


class TestClimateRequest(BaseModel):
    action: str = Field(..., pattern="^(HEAT|OFF)$")


class RCFitRequest(BaseModel):
    observations: list[dict[str, Any]]


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


def compute_boiler_stats(
    cycles: list[dict[str, Any]],
    *,
    interval_sec: int = 300,
    gas_krw_per_hour: int = 1200,
) -> dict[str, Any]:
    """Compute daily boiler runtime, duty cycle %, short-cycling count, and estimated KRW cost."""
    if not cycles:
        return {
            "totalMinutes": 0,
            "heatingMinutes": 0,
            "dutyCyclePct": 0.0,
            "shortCycles": 0,
            "estimatedGasKrw": 0,
        }

    # Group by cycle timestamp prefix (YYYY-MM-DDTHH:MM) so simultaneous rooms count once for boiler
    by_slot: dict[str, bool] = {}
    for c in sorted(cycles, key=lambda x: x.get("executionStartedAt") or ""):
        ts = str(c.get("executionStartedAt") or "")[:16]
        if not ts:
            continue
        is_on = c.get("commandAction") == "HEAT" or c.get("combustionConfirmed") is True
        by_slot[ts] = by_slot.get(ts, False) or is_on

    ordered = [by_slot[k] for k in sorted(by_slot.keys())]
    step_min = interval_sec / 60.0
    total_min = round(len(ordered) * step_min, 1)
    heat_slots = sum(1 for on in ordered if on)
    heat_min = round(heat_slots * step_min, 1)
    duty_pct = round((heat_slots / len(ordered)) * 100.0, 1) if ordered else 0.0

    # Count short ON runs (< 10 minutes)
    short_cycles = 0
    run_len = 0
    for on in ordered:
        if on:
            run_len += 1
        elif run_len > 0:
            if (run_len * step_min) < 10.0:
                short_cycles += 1
            run_len = 0
    if run_len > 0 and (run_len * step_min) < 10.0:
        short_cycles += 1

    est_krw = int(round((heat_min / 60.0) * gas_krw_per_hour))
    return {
        "totalMinutes": total_min,
        "heatingMinutes": heat_min,
        "dutyCyclePct": duty_pct,
        "shortCycles": short_cycles,
        "estimatedGasKrw": est_krw,
    }


class ItssEngine:
    """Core runtime state and multi-zone P000-P400 control engine for the Add-on."""

    def __init__(
        self,
        db_path: str | None = None,
        ha_client: HomeAssistantClient | None = None,
        notifier: PushNotifier | None = None,
    ) -> None:
        opts = _load_options()
        resolved_db = db_path or os.environ.get("ITSS_DB_PATH") or opts.get("db_path") or "./itss_scada.db"
        self.db = ScadaDatabase(resolved_db)
        self.ha = ha_client or HomeAssistantClient(token=opts.get("ha_token") or None)

        saved_settings = self.db.get_kv("settings", {})
        self.settings: dict[str, Any] = {
            "dryRun": bool(saved_settings.get("dryRun", opts.get("dry_run", True))),
            "zoneStrategy": str(saved_settings.get("zoneStrategy", opts.get("zone_strategy", STRATEGY_HYBRID))),
            "predictorModel": str(saved_settings.get("predictorModel", opts.get("predictor_model", MODEL_LINEAR_V12))),
            "updateIntervalSec": int(opts.get("update_interval_sec", 60)),
            "masterFloorEntity": str(saved_settings.get("masterFloorEntity", opts.get("master_floor_entity", ""))),
            "outdoorTempEntity": str(saved_settings.get("outdoorTempEntity", opts.get("outdoor_temp_entity", ""))),
            "weatherLocation": str(saved_settings.get("weatherLocation", opts.get("weather_location", "서울"))),
            "heatOnSetpoint": float(saved_settings.get("heatOnSetpoint", opts.get("heat_on_setpoint", 24.0))),
            "heatOffSetpoint": float(saved_settings.get("heatOffSetpoint", opts.get("heat_off_setpoint", 15.0))),
            "floorOffLimitC": float(saved_settings.get("floorOffLimitC", opts.get("floor_off_limit_c", 26.5))),
            "maxRunCapMin": min(15, int(saved_settings.get("maxRunCapMin", opts.get("max_run_cap_min", 15)))),
            "cooldownMin": int(saved_settings.get("cooldownMin", opts.get("cooldown_min", 20))),
            "gasKrwPerHour": int(saved_settings.get("gasKrwPerHour", opts.get("gas_krw_per_hour", 1200))),
            "notifyEvents": bool(saved_settings.get("notifyEvents", True)),
        }
        self.active_preset: str = str(self.db.get_kv("active_preset", "AUTO_ALL"))
        self.room_timers: dict[str, dict[str, Any]] = self.db.get_kv("room_timers", {})
        self.lag_profiles: dict[str, dict[str, Any]] = {}
        self._cycle_counter: int = 0

        gemini_key = str(opts.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
        self.weather_client = FallbackWeatherProvider(
            location=self.settings["weatherLocation"],
            gemini_api_key=gemini_key,
        )
        self.advisor = GeminiThermalAdvisor(gemini_key)
        self.tts_alert = GeminiTTSAlert(gemini_key) if gemini_key else None
        self.notifier = notifier or PushNotifier(
            ha_client=self.ha,
            notify_service=str(opts.get("notify_service", "")),
            telegram_chat_id=str(opts.get("telegram_chat_id", "")),
        )
        self.sentinel = SentinelProtocol()
        self.atmos = AtmosProtocol()

        # Recompute learned thermal lags and restore active residual cooldowns across restarts
        self.refresh_lag_profiles()
        self._restore_cooldowns_from_history()

    def refresh_lag_profiles(self) -> dict[str, dict[str, Any]]:
        """Learn per-room floor/room rise dead-times and post-OFF residual peaks from cycles DB."""
        cycles = self.db.list_cycles(limit=1000)
        base_cd = int(self.settings["cooldownMin"])
        profiles: dict[str, dict[str, Any]] = {}
        for rm in self.db.list_rooms():
            r_name = rm["roomName"]
            prof = learn_room_lag_profile(r_name, cycles, base_cooldown_min=base_cd)
            profiles[r_name] = prof.to_dict()
        self.lag_profiles = profiles
        self.db.set_kv("thermal_lag_profiles", profiles)
        return profiles

    def _restore_cooldowns_from_history(self) -> None:
        """Guarantee residual-heat cooldown survives add-on restarts.

        Scans recent cycles per room: if the room recently transitioned from HEAT to OFF
        (or was already in P000_RESIDUAL_COOLDOWN) within its effective cooldown window,
        preserves/reconstructs cooldownUntil so restart never triggers immediate re-heating.
        """
        now = datetime.now(tz=KST)
        changed = False
        for rm in self.db.list_rooms():
            r_name = rm["roomName"]
            tm = self.room_timers.setdefault(r_name, {"heatingStartedAt": None, "cooldownUntil": None})
            # Check if persisted cooldownUntil is already active in the future
            existing_cd = tm.get("cooldownUntil")
            if existing_cd:
                try:
                    if datetime.fromisoformat(str(existing_cd)) > now:
                        continue
                except ValueError:
                    pass

            recent = self.db.list_cycles(room_name=r_name, limit=60)
            if not recent:
                continue

            prof = self.lag_profiles.get(r_name) or {}
            cd_min = int(prof.get("recommendedCooldownMin", self.settings["cooldownMin"]))
            latest = recent[0]
            # Only protect rooms that are currently OFF / in residual cooldown
            latest_on = (
                latest.get("combustionConfirmed") is True
                or (latest.get("combustionConfirmed") is None and latest.get("commandAction") == "HEAT")
            )
            if latest_on:
                continue

            # Find the most recent HEAT -> OFF boundary in chronological order (recent is DESC)
            off_boundary_ts: datetime | None = None
            for idx in range(len(recent) - 1):
                curr_c = recent[idx]
                prev_c = recent[idx + 1]
                prev_on = (
                    prev_c.get("combustionConfirmed") is True
                    or (prev_c.get("combustionConfirmed") is None and prev_c.get("commandAction") == "HEAT")
                )
                curr_off = curr_c.get("commandAction") == "OFF" or curr_c.get("safetyReason") in (
                    "P000_RESIDUAL_COOLDOWN",
                    "P000_MAX_RUN_CUTOFF",
                    "P000_FLOOR_LIMIT_OFF",
                    "P000_OVERHEAT_CUTOFF",
                    "P200_PREDICTIVE_OFF",
                    "HYBRID_EARLY_CUTOFF",
                )
                if prev_on and curr_off and curr_c.get("executionStartedAt"):
                    try:
                        off_boundary_ts = datetime.fromisoformat(str(curr_c["executionStartedAt"]))
                    except ValueError:
                        off_boundary_ts = None
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
        """Apply one-touch preset (AUTO_ALL / AWAY / SLEEP / PREHEAT / ALL_OFF)."""
        self.active_preset = preset
        self.db.set_kv("active_preset", preset)
        rooms = self.db.list_rooms()
        for rm in rooms:
            if preset == "ALL_OFF":
                self.db.update_room_control(rm["roomName"], control_mode="MANUAL", manual_action="OFF")
            elif preset == "AUTO_ALL":
                self.db.update_room_control(rm["roomName"], control_mode="AUTO", manual_action="HOLD")
            else:
                self.db.update_room_control(rm["roomName"], control_mode="AUTO", manual_action="HOLD")
        return {"activePreset": preset, "offsetC": PRESET_OFFSETS.get(preset, 0.0)}

    def calibrate_rc_and_offsets(self) -> dict[str, Any]:
        """Fit RC parameters and estimate slave floor offsets from stored cycles."""
        cycles = list(reversed(self.db.list_cycles(limit=500)))
        rooms = self.db.list_rooms()
        by_room: dict[str, list[dict[str, Any]]] = {}
        for c in cycles:
            by_room.setdefault(c["roomName"], []).append(c)

        # Build consecutive observations for RC fitting
        observations: list[RCObservation] = []
        dt_sec = float(self.settings["updateIntervalSec"])
        for r_cycles in by_room.values():
            for prev, curr in zip(r_cycles, r_cycles[1:]):
                if None in (prev.get("floorTemp"), prev.get("roomTemp"), curr.get("floorTemp"), curr.get("roomTemp")):
                    continue
                observations.append(
                    RCObservation(
                        dt_sec=dt_sec,
                        floor_temp=float(prev["floorTemp"]),
                        room_temp=float(prev["roomTemp"]),
                        outdoor_temp=5.0,
                        heating_on=(prev.get("commandAction") == "HEAT"),
                        next_floor_temp=float(curr["floorTemp"]),
                        next_room_temp=float(curr["roomTemp"]),
                    )
                )

        rc_result = fit_rc_parameters(observations)
        if rc_result.get("status") == "READY":
            self.db.set_kv("fitted_rc_params", rc_result)

        # Estimate slave room floor offset relative to master room mean indoor delta
        master_room = next((r["roomName"] for r in rooms if r["hasFloorSensor"]), None)
        master_temps = [c["roomTemp"] for c in by_room.get(master_room or "", []) if c.get("roomTemp") is not None]
        master_mean = (sum(master_temps) / len(master_temps)) if master_temps else None

        offset_updates: dict[str, float] = {}
        if master_mean is not None:
            for rm in rooms:
                if rm["hasFloorSensor"]:
                    continue
                r_temps = [c["roomTemp"] for c in by_room.get(rm["roomName"], []) if c.get("roomTemp") is not None]
                if len(r_temps) >= 3:
                    # Estimate floor offset conservatively from indoor difference (clamped +/-2.0C)
                    est_offset = max(-2.0, min(2.0, round((sum(r_temps) / len(r_temps)) - master_mean, 1)))
                    self.db.update_room_control(rm["roomName"], floor_offset_c=est_offset)
                    offset_updates[rm["roomName"]] = est_offset

        lag_profiles = self.refresh_lag_profiles()
        return {
            "rcFit": rc_result,
            "updatedFloorOffsets": offset_updates,
            "thermalLagProfiles": lag_profiles,
        }

    async def _read_outdoor_weather(self) -> tuple[float | None, float | None, str]:
        """Read outdoor temp/wind from HA sensor (e.g., Naver Weather), else fallback to API for configured region."""
        ent = self.settings.get("outdoorTempEntity", "").strip()
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
                    val, _, _ = await self.ha.get_numeric_state(ent)
                    if val is not None:
                        return val, None, "HA센서"

        # Fallback when HA outdoor/Naver Weather sensor fails or is unavailable
        if self.weather_client is not None:
            w = await asyncio.to_thread(self.weather_client.get_weather)
            if w.outdoor_temp_c is not None:
                loc = self.settings.get("weatherLocation", "서울")
                label = "Gemini API" if w.source == "GEMINI_SEARCH_GROUNDING" else "Open-Meteo API"
                return w.outdoor_temp_c, w.wind_speed_ms, f"{label}·{loc}"
        return None, None, "시간대폴백"

    def _timer_metrics(self, room_name: str, now: datetime) -> tuple[float, float]:
        """Return (continuous_run_min, cooldown_remaining_min) for one room."""
        tm = self.room_timers.get(room_name, {})
        run_min = 0.0
        cd_min = 0.0
        started = tm.get("heatingStartedAt")
        if started:
            try:
                run_min = max(0.0, (now - datetime.fromisoformat(started)).total_seconds() / 60.0)
            except ValueError:
                pass
        cd_until = tm.get("cooldownUntil")
        if cd_until:
            try:
                cd_min = max(0.0, (datetime.fromisoformat(cd_until) - now).total_seconds() / 60.0)
            except ValueError:
                pass
        return round(run_min, 1), round(cd_min, 1)

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
    ) -> None:
        """Send Telegram/HA push notification when boiler action or safety reason changes."""
        if not self.settings.get("notifyEvents", True):
            return
        tm = self.room_timers.setdefault(room_name, {})
        prev_action = tm.get("lastNotifiedAction")
        prev_reason = tm.get("lastNotifiedReason")
        is_critical = reason_code in (
            "P000_OVERHEAT_CUTOFF",
            "P000_FLOOR_LIMIT_OFF",
            "P000_MAX_RUN_CUTOFF",
            "P000_VENTILATION_PROTECT",
            "P000_SENSOR_MISSING",
            "P000_SENSOR_STALE",
        )
        # Notify only on ON/OFF transitions or new critical reason (prevents 1-min spam)
        if action == prev_action and (not is_critical or reason_code == prev_reason):
            return

        tm["lastNotifiedAction"] = action
        tm["lastNotifiedReason"] = reason_code
        reason_ko = REASON_KO.get(reason_code, reason_code)
        dry_tag = " [시험운전]" if self.settings.get("dryRun") else ""

        if action == "HEAT":
            title = f"🔥 I.T.S.S. 보일러 켜짐 ({room_name}){dry_tag}"
            lines = [
                f"이유: {reason_ko}",
                f"실내: {room_temp}°C / 바닥: {floor_temp}°C / 목표: {target_temp}°C",
            ]
            if pred_room is not None:
                lines.append(f"30분 예측 실내: {pred_room}°C")
            lines.append(f"조절기 설정: {setpoint}°C · 이번 최대 가동: {max_run_min}분")
        else:
            title = f"❄️ I.T.S.S. 보일러 꺼짐 ({room_name}){dry_tag}"
            lines = [
                f"이유: {reason_ko}",
                f"실내: {room_temp}°C / 바닥: {floor_temp}°C / 목표: {target_temp}°C",
            ]
            if pred_room is not None:
                lines.append(f"30분 예측 실내: {pred_room}°C")
            if run_min > 0:
                lines.append(f"이번 가동 시간: {run_min}분 · 조절기 설정: {setpoint}°C")
            else:
                lines.append(f"조절기 설정: {setpoint}°C")

        await self.notifier.send(title, "\n".join(lines))

    async def run_control_cycle(self) -> list[dict[str, Any]]:
        """Execute one coordinated P000-P400 + P200 predictive multi-zone cycle."""
        now = datetime.now(tz=KST)
        scheduled_at = now.isoformat(timespec="seconds")
        rooms = self.db.list_rooms()
        if not rooms:
            return []

        master_floor, floor_ts, floor_age = await self.ha.get_numeric_state(self.settings["masterFloorEntity"])
        outdoor_temp, wind_speed, _ = await self._read_outdoor_weather()

        atmos_offset = self.atmos.compute_offset(outdoor_temp=outdoor_temp, wind_speed_ms=wind_speed, now_kst=now)
        max_run_min = self.atmos.compute_max_run_minutes(
            outdoor_temp=outdoor_temp,
            now_kst=now,
            hard_cap_min=float(self.settings["maxRunCapMin"]),
        )
        floor_off_limit = float(self.settings["floorOffLimitC"])
        sentinel = SentinelProtocol(SentinelConfig(general_off_floor_c=floor_off_limit))
        predictor = self._build_predictor()
        schedule_offset, _ = self.db.active_schedule_offset(now.hour)
        preset_offset = PRESET_OFFSETS.get(self.active_preset, 0.0)

        # Periodically refresh learned floor/room lag profiles from accumulating cycles
        self._cycle_counter += 1
        if self._cycle_counter % 30 == 0 or not self.lag_profiles:
            self.refresh_lag_profiles()

        telemetry: dict[str, dict[str, Any]] = {}
        auto_zone_readings: list[ZoneReading] = []
        p200_by_room: dict[str, Any] = {}

        for rm in rooms:
            r_name = rm["roomName"]
            r_temp, r_ts, r_age = await self.ha.get_numeric_state(rm["roomTempEntity"])
            c_state = await self.ha.get_state(rm["climateEntity"])
            hvac_mode = c_state.get("state") if c_state else None
            combustion = detect_boiler_running(c_state)
            is_heating = bool(combustion) if combustion is not None else (hvac_mode == "heat")

            has_floor = rm["hasFloorSensor"]
            if master_floor is not None:
                eff_floor = round(master_floor + (0.0 if has_floor else rm["floorOffsetC"]), 1)
                floor_src = "MEASURED" if has_floor else "ESTIMATED"
            elif not self.settings["masterFloorEntity"].strip():
                eff_floor = 24.0
                floor_src = "UNCONFIGURED"
            else:
                eff_floor = None
                floor_src = "MISSING"

            # Compute short-term temperature slopes (degC/min) for P200 prediction & P000 ventilation
            tm = self.room_timers.setdefault(r_name, {"heatingStartedAt": None, "cooldownUntil": None})
            v_room, v_floor = 0.0, 0.0
            prev_ts_str = tm.get("prevSampleAt")
            prev_r = tm.get("prevRoomTemp")
            prev_f = tm.get("prevFloorTemp")
            if prev_ts_str and r_temp is not None and prev_r is not None:
                try:
                    dt_min = (now - datetime.fromisoformat(str(prev_ts_str))).total_seconds() / 60.0
                    if 0.5 <= dt_min <= 60.0:
                        v_room = (r_temp - float(prev_r)) / dt_min
                        if eff_floor is not None and prev_f is not None:
                            v_floor = (eff_floor - float(prev_f)) / dt_min
                except (ValueError, TypeError):
                    pass

            if r_temp is not None:
                tm["prevSampleAt"] = now.isoformat(timespec="seconds")
                tm["prevRoomTemp"] = r_temp
                if eff_floor is not None:
                    tm["prevFloorTemp"] = eff_floor

            # Track continuous heating duration per room based on real boiler state
            if is_heating:
                if not tm.get("heatingStartedAt"):
                    tm["heatingStartedAt"] = now.isoformat(timespec="seconds")
            else:
                tm["heatingStartedAt"] = None
            run_min, cd_min = self._timer_metrics(r_name, now)

            eff_target = round(rm["targetTemp"] + atmos_offset + schedule_offset + preset_offset, 2)

            # Run P200 predictive engine using learned room/floor thermal lag & residual rise
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
                        v_floor_per_min=v_floor,
                        v_room_per_min=v_room,
                        outdoor_temp=outdoor_temp,
                        atmos_loss_offset_c=atmos_offset,
                        circadia_target_offset_c=schedule_offset + preset_offset,
                        horizon_min=learned_horizon,
                    ),
                    residual_stats=learned_stats,
                )

            telemetry[r_name] = {
                "room": rm,
                "roomTemp": r_temp,
                "roomTs": r_ts,
                "roomAge": r_age,
                "effFloor": eff_floor,
                "floorSrc": floor_src,
                "vRoom": v_room,
                "hvacMode": hvac_mode,
                "isHeating": is_heating,
                "combustion": combustion,
                "effTarget": eff_target,
                "runMin": run_min,
                "cdMin": cd_min,
            }

            if rm["controlMode"] == "AUTO" and r_temp is not None:
                auto_zone_readings.append(
                    ZoneReading(
                        room_name=r_name,
                        room_temp=r_temp,
                        target_temp=eff_target,
                        is_heating=is_heating,
                        has_floor_sensor=rm["hasFloorSensor"],
                        floor_offset_from_master_c=rm["floorOffsetC"],
                        weight=rm["weight"],
                    )
                )

        mz_ctrl = MultiZoneController(
            strategy=self.settings["zoneStrategy"],
            general_off_floor_c=floor_off_limit,
        )
        mz_plan = mz_ctrl.evaluate(
            auto_zone_readings,
            master_floor_temp=master_floor if master_floor is not None else 24.0,
        )
        mz_by_room = {c.room_name: c for c in mz_plan.commands}

        # Check if any AUTO room is requesting primary heat via P200 or MultiZone (for co-heating batch)
        any_p200_heat = any(
            (r_name in p200_by_room and p200_by_room[r_name].recommended_action == "HEAT")
            for r_name in telemetry
            if telemetry[r_name]["room"]["controlMode"] == "AUTO"
        )

        results: list[dict[str, Any]] = []
        for rm in rooms:
            r_name = rm["roomName"]
            t = telemetry[r_name]
            exec_started = now_kst_iso()
            cycle_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{r_name}"
            eff_floor = t["effFloor"]
            floor_src = t["floorSrc"]
            pred = p200_by_room.get(r_name)

            is_manual = rm["controlMode"] == "MANUAL"
            snapshot = SensorSnapshot(
                room_name=r_name,
                room_temp=t["roomTemp"],
                floor_temp=eff_floor,
                outdoor_temp=outdoor_temp,
                thermostat_hvac_mode=t["hvacMode"],
                combustion_confirmed=t["combustion"],
                manual_override_active=False,
                sensor_measured_at_room=t["roomTs"],
                sensor_measured_at_floor=floor_ts,
                room_sensor_age_sec=t["roomAge"],
                floor_sensor_age_sec=floor_age,
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
            )

            overheat = (
                (t["roomTemp"] is not None and t["roomTemp"] >= 27.0)
                or (eff_floor is not None and eff_floor >= 29.0)
            )
            lag_prof = self.lag_profiles.get(r_name) or {}
            room_max_run_min = max(
                5.0,
                min(15.0, max_run_min - float(lag_prof.get("recommendedRunTrimMin", 0.0))),
            )
            room_cooldown_min = int(lag_prof.get("recommendedCooldownMin", self.settings["cooldownMin"]))
            tm = self.room_timers.setdefault(r_name, {"heatingStartedAt": None, "cooldownUntil": None})
            decision_reason = "P000_OK"

            if overheat:
                decision_reason = "P000_OVERHEAT_CUTOFF"
                record.safety_reason = decision_reason
                record.command_action = "OFF"
                record.execution_status = "SKIPPED_SAFETY"
                tm["heatingStartedAt"] = None
                tm["cooldownUntil"] = (
                    now + timedelta(minutes=room_cooldown_min)
                ).isoformat(timespec="seconds")
            elif is_manual:
                decision_reason = "P000_MANUAL_OVERRIDE"
                record.safety_reason = decision_reason
                record.command_action = rm["manualAction"]
                record.execution_status = "EXECUTED"
            else:
                verdict = sentinel.evaluate(
                    snapshot,
                    is_heating=t["isHeating"],
                    v_room_per_min=t["vRoom"],
                    continuous_run_min=t["runMin"],
                    max_run_min=room_max_run_min,
                    cooldown_remaining_min=t["cdMin"],
                )
                record.safety_reason = verdict.reason_code
                if verdict.force_off:
                    decision_reason = verdict.reason_code
                    record.command_action = "OFF"
                    record.execution_status = "SKIPPED_SAFETY"
                    if verdict.reason_code in ("P000_MAX_RUN_CUTOFF", "P000_FLOOR_LIMIT_OFF"):
                        tm["heatingStartedAt"] = None
                        tm["cooldownUntil"] = (
                            now + timedelta(minutes=room_cooldown_min)
                        ).isoformat(timespec="seconds")
                elif verdict.hold_current_state or r_name not in mz_by_room:
                    decision_reason = verdict.reason_code
                    record.command_action = "HOLD"
                    record.execution_status = "SKIPPED_SAFETY"
                elif self.settings["zoneStrategy"] == STRATEGY_HYBRID and pred is not None:
                    # P200 Predictive Early-OFF prevents Ondol residual-heat overshoot
                    if pred.recommended_action == "OFF":
                        record.command_action = "OFF"
                        decision_reason = "P200_PREDICTIVE_OFF"
                    elif pred.recommended_action == "HEAT":
                        record.command_action = "HEAT"
                        decision_reason = "P200_PREDICTIVE_ON"
                    elif any_p200_heat and mz_by_room[r_name].action == "HEAT":
                        record.command_action = "HEAT"
                        decision_reason = "HYBRID_COHEAT_BATCH"
                    else:
                        record.command_action = "OFF"
                        decision_reason = mz_by_room[r_name].reason
                    record.safety_reason = decision_reason
                else:
                    record.command_action = mz_by_room[r_name].action
                    decision_reason = mz_by_room[r_name].reason
                    record.safety_reason = decision_reason

            prev_run_min = t["runMin"]
            was_heating = t["isHeating"] or (tm.get("heatingStartedAt") is not None)
            if record.command_action == "HEAT" and not tm.get("heatingStartedAt"):
                tm["heatingStartedAt"] = now.isoformat(timespec="seconds")
            elif record.command_action == "OFF":
                # Enter residual-heat soak cooldown on ANY HEAT -> OFF transition (survives restarts)
                if was_heating and not is_manual and t["cdMin"] <= 0:
                    tm["cooldownUntil"] = (
                        now + timedelta(minutes=room_cooldown_min)
                    ).isoformat(timespec="seconds")
                tm["heatingStartedAt"] = None

            if record.command_action in ("HEAT", "OFF"):
                record.command_setpoint = (
                    float(self.settings["heatOnSetpoint"])
                    if record.command_action == "HEAT"
                    else float(self.settings["heatOffSetpoint"])
                )
                await self._dispatch(rm["climateEntity"], record)
                await self._notify_state_change(
                    room_name=r_name,
                    action=record.command_action,
                    reason_code=decision_reason,
                    room_temp=t["roomTemp"],
                    floor_temp=eff_floor,
                    target_temp=rm["targetTemp"],
                    pred_room=record.predicted_room_30m,
                    setpoint=record.command_setpoint,
                    run_min=prev_run_min,
                    max_run_min=room_max_run_min,
                )

            record.saved_at = now_kst_iso()
            payload = record.to_payload()
            self.db.insert_cycle(payload)
            results.append(payload)

        self.db.set_kv("room_timers", self.room_timers)
        return results

    async def _dispatch(self, climate_entity: str, record: CycleRecord) -> None:
        """Issue climate command to HA Supervisor API (ON=24C, OFF=15C) and re-read.

        Uses RS485/Kocom-safe sequencing: only calls set_hvac_mode when needed,
        pauses briefly before set_temperature, and records exact errors.
        """
        record.control_issued_at = now_kst_iso()
        if self.settings["dryRun"] or not self.ha.is_configured:
            record.control_confirmed_at = now_kst_iso()
            record.observed_at = now_kst_iso()
            return

        try:
            if record.command_action == "HEAT":
                if record.device_mode_event != "heat":
                    try:
                        await self.ha.call_service(
                            "climate", "set_hvac_mode", {"entity_id": climate_entity, "hvac_mode": "heat"}
                        )
                        await asyncio.sleep(0.8)  # Prevent RS485 wallpad command collision
                    except Exception:  # noqa: BLE001
                        # Still attempt set_temperature even if set_hvac_mode is unsupported
                        pass
                await self.ha.call_service(
                    "climate", "set_temperature", {"entity_id": climate_entity, "temperature": record.command_setpoint}
                )
            elif record.command_action == "OFF":
                try:
                    await self.ha.call_service(
                        "climate", "set_temperature", {"entity_id": climate_entity, "temperature": record.command_setpoint}
                    )
                except Exception:  # noqa: BLE001
                    # Some wallpads reject set_temperature while already in 'off' mode; try turning off explicitly
                    if record.device_mode_event != "off":
                        await self.ha.call_service(
                            "climate", "set_hvac_mode", {"entity_id": climate_entity, "hvac_mode": "off"}
                        )
            record.control_confirmed_at = now_kst_iso()

            await asyncio.sleep(DEFAULT_RECHECK_DELAY_SEC)
            recheck = await self.ha.get_state(climate_entity)
            record.observed_at = now_kst_iso()
            if recheck:
                record.device_mode_event = recheck.get("state")
                record.combustion_confirmed = detect_boiler_running(recheck)
        except Exception as err:  # noqa: BLE001
            self.ha.last_error = f"Climate dispatch failed ({climate_entity}): {err}"
            record.execution_status = "FAILED"
            record.observed_at = now_kst_iso()


def create_app(
    db_path: str | None = None,
    ha_client: HomeAssistantClient | None = None,
    notifier: PushNotifier | None = None,
) -> FastAPI:
    engine = ItssEngine(db_path=db_path, ha_client=ha_client, notifier=notifier)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop_event = asyncio.Event()

        async def _loop():
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=engine.settings["updateIntervalSec"])
                except asyncio.TimeoutError:
                    try:
                        await engine.run_control_cycle()
                    except Exception:  # noqa: BLE001
                        pass

        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            stop_event.set()
            task.cancel()

    app = FastAPI(title="I.T.S.S. Thermal SCADA", version="2.0.0", lifespan=lifespan)
    app.state.engine = engine

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "timeKst": now_kst_iso(),
            "haConnected": engine.ha.is_configured,
            "tokenSource": getattr(engine.ha, "token_source", "mock"),
        }

    @app.get("/api/v1/status")
    async def get_status() -> dict[str, Any]:
        rooms = engine.db.list_rooms()
        recent = engine.db.list_cycles(limit=288)
        latest_by_room: dict[str, dict[str, Any]] = {}
        for c in recent:
            if c["roomName"] not in latest_by_room:
                latest_by_room[c["roomName"]] = c

        now_kst = datetime.now(tz=KST)
        master_floor, _, _ = await engine.ha.get_numeric_state(engine.settings["masterFloorEntity"])
        outdoor_temp, _, outdoor_source = await engine._read_outdoor_weather()
        active_max_run = engine.atmos.compute_max_run_minutes(
            outdoor_temp=outdoor_temp,
            now_kst=now_kst,
            hard_cap_min=float(engine.settings["maxRunCapMin"]),
        )
        live_by_room: dict[str, dict[str, Any]] = {}
        for rm in rooms:
            r_name = rm["roomName"]
            r_temp, _, _ = await engine.ha.get_numeric_state(rm["roomTempEntity"])
            c_state = await engine.ha.get_state(rm["climateEntity"])
            eff_floor = (
                round(master_floor + (0.0 if rm["hasFloorSensor"] else rm["floorOffsetC"]), 1)
                if master_floor is not None
                else None
            )
            c_attrs = (c_state.get("attributes") or {}) if c_state else {}
            boiler_running = detect_boiler_running(c_state)
            run_min, cd_min = engine._timer_metrics(r_name, now_kst)
            live_by_room[r_name] = {
                "roomTemp": r_temp,
                "floorTemp": eff_floor,
                "climateState": c_state.get("state") if c_state else None,
                "climateSetpoint": c_attrs.get("temperature"),
                "climateCurrentTemp": c_attrs.get("current_temperature"),
                "boilerRunning": boiler_running,
                "runtimeMin": run_min,
                "cooldownRemainMin": cd_min,
            }

        sched_offset, sched_label = engine.db.active_schedule_offset(now_kst.hour)

        return {
            "timeKst": now_kst_iso(),
            "haConnected": engine.ha.is_configured,
            "tokenSource": getattr(engine.ha, "token_source", "mock"),
            "haLastError": getattr(engine.ha, "last_error", None),
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
            "kpis": compute_daily_kpis(recent),
            "boilerStats": compute_boiler_stats(
                recent,
                interval_sec=engine.settings["updateIntervalSec"],
                gas_krw_per_hour=engine.settings["gasKrwPerHour"],
            ),
            "fittedRcParams": engine.db.get_kv("fitted_rc_params"),
            "thermalLagProfiles": engine.lag_profiles,
        }

    @app.get("/api/v1/ha/entities")
    async def list_ha_entities() -> dict[str, Any]:
        return await engine.ha.list_entities()

    @app.get("/api/v1/ha/diagnose")
    async def diagnose_ha() -> dict[str, Any]:
        """Return live diagnostic dump of rooms, climate attributes, and HA services."""
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
                "climateEntity": rm["climateEntity"],
                "climateState": c_raw.get("state") if c_raw else None,
                "climateAttrs": (c_raw.get("attributes") or {}) if c_raw else None,
            })
        mf_raw = await engine.ha.get_state(engine.settings["masterFloorEntity"])
        services = await engine.ha.list_services() if hasattr(engine.ha, "list_services") else {}
        return {
            "haConnected": engine.ha.is_configured,
            "tokenSource": getattr(engine.ha, "token_source", "mock"),
            "dryRun": engine.settings["dryRun"],
            "heatOnSetpoint": engine.settings["heatOnSetpoint"],
            "heatOffSetpoint": engine.settings["heatOffSetpoint"],
            "masterFloorEntity": engine.settings["masterFloorEntity"],
            "masterFloorState": mf_raw.get("state") if mf_raw else None,
            "lastError": getattr(engine.ha, "last_error", None),
            "rooms": room_diags,
            "availableServices": services,
            "notifyConfig": {
                "notifyService": engine.notifier.notify_service,
                "telegramChatId": engine.notifier.telegram_chat_id,
            },
        }

    @app.post("/api/v1/rooms/{room_name}/test-climate")
    async def test_room_climate(room_name: str, req: TestClimateRequest) -> dict[str, Any]:
        """Directly dispatch a live HEAT(24C)/OFF(15C) test to one room's climate entity."""
        rm = engine.db.get_room(room_name)
        if not rm:
            raise HTTPException(status_code=404, detail="Room not found")
        cid = rm["climateEntity"]
        setpoint = (
            float(engine.settings["heatOnSetpoint"])
            if req.action == "HEAT"
            else float(engine.settings["heatOffSetpoint"])
        )
        before = await engine.ha.get_state(cid)
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
        return engine.db.upsert_room(req.model_dump())

    @app.patch("/api/v1/rooms/{room_name}")
    async def patch_room(room_name: str, req: RoomPatchRequest) -> dict[str, Any]:
        updated = engine.db.update_room_control(
            room_name,
            target_temp=req.targetTemp,
            control_mode=req.controlMode,
            manual_action=req.manualAction,
            floor_offset_c=req.floorOffsetC,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Room not found")
        # Trigger immediate control cycle when user switches manual mode or target temp
        if req.controlMode is not None or req.manualAction is not None:
            await engine.run_control_cycle()
        return updated

    @app.delete("/api/v1/rooms/{room_name}")
    def delete_room(room_name: str) -> dict[str, Any]:
        if not engine.db.delete_room(room_name):
            raise HTTPException(status_code=404, detail="Room not found")
        return {"deleted": room_name}

    @app.patch("/api/v1/settings")
    def patch_settings(req: SettingsPatchRequest) -> dict[str, Any]:
        for field_name, val in req.model_dump(exclude_none=True).items():
            engine.settings[field_name] = val
        if req.weatherLocation is not None and engine.weather_client is not None:
            engine.weather_client.set_location(req.weatherLocation)
        engine.db.set_kv("settings", engine.settings)
        return engine.settings

    @app.post("/api/v1/preset")
    async def set_preset(req: PresetRequest) -> dict[str, Any]:
        res = engine.apply_preset(req.preset)
        if engine.settings.get("notifyEvents", True):
            await engine.notifier.send(
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
    async def test_notify() -> dict[str, str]:
        return await engine.notifier.send("I.T.S.S. 테스트 알림", "모바일 푸시 알림 연동이 정상 작동합니다.")

    @app.post("/api/v1/control/run-now")
    async def run_now() -> dict[str, Any]:
        records = await engine.run_control_cycle()
        return {"executed": len(records), "records": records}

    @app.get("/api/v1/cycles")
    def get_cycles(room_name: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500)):
        items = engine.db.list_cycles(room_name=room_name, limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/v1/report/daily")
    async def daily_report() -> dict[str, Any]:
        cycles = engine.db.list_cycles(limit=288)
        return await asyncio.to_thread(engine.advisor.generate_report, cycles)

    @app.post("/api/v1/rc/fit")
    def fit_rc(req: RCFitRequest) -> dict[str, Any]:
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
        return fit_rc_parameters(obs)

    return app


app = create_app()
