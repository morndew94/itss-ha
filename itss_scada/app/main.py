"""FastAPI SCADA Server & Ingress Web UI for the I.T.S.S. Home Assistant Add-on (v2.0)."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .const import (
    DEFAULT_RECHECK_DELAY_SEC,
    KST,
    MODEL_LINEAR_V12,
    STRATEGY_HYBRID,
)
from .ha_client import HomeAssistantClient
from .integrations.google_alerts import GeminiTTSAlert
from .integrations.google_gemini_advisor import GeminiThermalAdvisor, compute_daily_kpis
from .integrations.google_weather import GoogleWeatherClient
from .integrations.notifications import PushNotifier
from .models.schema import CycleRecord, SensorSnapshot, now_kst_iso
from .protocols.multi_zone import MultiZoneController, ZoneReading
from .protocols.p000_sentinel import SentinelProtocol
from .protocols.p300_atmos import AtmosProtocol
from .protocols.rc_optimizer import RCObservation, fit_rc_parameters
from .storage.db import ScadaDatabase

STATIC_DIR = Path(__file__).parent / "static"

PRESET_OFFSETS = {
    "AUTO_ALL": 0.0,
    "AWAY": -2.0,
    "SLEEP": -0.3,
    "PREHEAT": 0.2,
    "ALL_OFF": 0.0,
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
    heatOnSetpoint: float | None = Field(default=None, ge=15.0, le=30.0)
    heatOffSetpoint: float | None = Field(default=None, ge=5.0, le=25.0)
    gasKrwPerHour: int | None = Field(default=None, ge=100, le=10000)


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
            "updateIntervalSec": int(opts.get("update_interval_sec", 300)),
            "masterFloorEntity": str(saved_settings.get("masterFloorEntity", opts.get("master_floor_entity", ""))),
            "weatherLocation": str(opts.get("weather_location", "서울")),
            "heatOnSetpoint": float(saved_settings.get("heatOnSetpoint", opts.get("heat_on_setpoint", 24.0))),
            "heatOffSetpoint": float(saved_settings.get("heatOffSetpoint", opts.get("heat_off_setpoint", 15.0))),
            "gasKrwPerHour": int(saved_settings.get("gasKrwPerHour", opts.get("gas_krw_per_hour", 1200))),
        }
        self.active_preset: str = str(self.db.get_kv("active_preset", "AUTO_ALL"))

        gemini_key = str(opts.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
        self.weather_client = GoogleWeatherClient(gemini_key, self.settings["weatherLocation"]) if gemini_key else None
        self.advisor = GeminiThermalAdvisor(gemini_key)
        self.tts_alert = GeminiTTSAlert(gemini_key) if gemini_key else None
        self.notifier = notifier or PushNotifier(
            ha_client=self.ha,
            notify_service=str(opts.get("notify_service", "")),
            telegram_chat_id=str(opts.get("telegram_chat_id", "")),
        )
        self.sentinel = SentinelProtocol()
        self.atmos = AtmosProtocol()

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

        return {"rcFit": rc_result, "updatedFloorOffsets": offset_updates}

    async def run_control_cycle(self) -> list[dict[str, Any]]:
        """Execute one coordinated multi-zone cycle across all registered rooms."""
        now = datetime.now(tz=KST)
        scheduled_at = now.isoformat(timespec="seconds")
        rooms = self.db.list_rooms()
        if not rooms:
            return []

        master_floor, floor_ts, floor_age = await self.ha.get_numeric_state(self.settings["masterFloorEntity"])
        outdoor_temp, wind_speed = None, None
        if self.weather_client is not None:
            w = await asyncio.to_thread(self.weather_client.get_weather)
            outdoor_temp, wind_speed = w.outdoor_temp_c, w.wind_speed_ms

        atmos_offset = self.atmos.compute_offset(outdoor_temp=outdoor_temp, wind_speed_ms=wind_speed, now_kst=now)
        schedule_offset, _ = self.db.active_schedule_offset(now.hour)
        preset_offset = PRESET_OFFSETS.get(self.active_preset, 0.0)

        telemetry: dict[str, dict[str, Any]] = {}
        auto_zone_readings: list[ZoneReading] = []

        for rm in rooms:
            r_name = rm["roomName"]
            r_temp, r_ts, r_age = await self.ha.get_numeric_state(rm["roomTempEntity"])
            c_state = await self.ha.get_state(rm["climateEntity"])
            hvac_mode = c_state.get("state") if c_state else None
            hvac_action = (c_state.get("attributes") or {}).get("hvac_action") if c_state else None
            is_heating = hvac_mode == "heat"
            combustion = (hvac_action == "heating") if hvac_action is not None else None

            eff_target = round(rm["targetTemp"] + atmos_offset + schedule_offset + preset_offset, 2)
            telemetry[r_name] = {
                "room": rm,
                "roomTemp": r_temp,
                "roomTs": r_ts,
                "roomAge": r_age,
                "hvacMode": hvac_mode,
                "isHeating": is_heating,
                "combustion": combustion,
                "effTarget": eff_target,
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

        mz_ctrl = MultiZoneController(strategy=self.settings["zoneStrategy"])
        mz_plan = mz_ctrl.evaluate(
            auto_zone_readings,
            master_floor_temp=master_floor if master_floor is not None else 24.0,
        )
        mz_by_room = {c.room_name: c for c in mz_plan.commands}

        results: list[dict[str, Any]] = []
        for rm in rooms:
            r_name = rm["roomName"]
            t = telemetry[r_name]
            exec_started = now_kst_iso()
            cycle_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{r_name}"

            has_floor = rm["hasFloorSensor"]
            if master_floor is not None:
                eff_floor = round(master_floor + (0.0 if has_floor else rm["floorOffsetC"]), 1)
                floor_src = "MEASURED" if has_floor else "ESTIMATED"
            elif not self.settings["masterFloorEntity"].strip():
                # Allow room-only operation when master floor sensor is not yet assigned
                eff_floor = 24.0
                floor_src = "UNCONFIGURED"
            else:
                eff_floor = None
                floor_src = "MISSING"

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
                predictor_model="manual" if is_manual else self.settings["predictorModel"],
                device_mode_event=t["hvacMode"],
                combustion_confirmed=t["combustion"],
            )

            overheat = (
                (t["roomTemp"] is not None and t["roomTemp"] >= 27.0)
                or (eff_floor is not None and eff_floor >= 29.0)
            )
            if overheat:
                record.safety_reason = "P000_OVERHEAT_CUTOFF"
                record.command_action = "OFF"
                record.execution_status = "SKIPPED_SAFETY"
                await self.notifier.send(
                    "I.T.S.S. 과열 차단 경고",
                    f"{r_name} 과열 감지(실내 {t['roomTemp']}°C / 바닥 {eff_floor}°C)로 난방을 차단했습니다.",
                )
            elif is_manual:
                record.safety_reason = "P000_MANUAL_OVERRIDE"
                record.command_action = rm["manualAction"]
                record.execution_status = "EXECUTED"
            else:
                verdict = self.sentinel.evaluate(snapshot, is_heating=t["isHeating"])
                record.safety_reason = verdict.reason_code
                if verdict.force_off:
                    record.command_action = "OFF"
                    record.execution_status = "SKIPPED_SAFETY"
                elif verdict.hold_current_state or r_name not in mz_by_room:
                    record.command_action = "HOLD"
                    record.execution_status = "SKIPPED_SAFETY"
                else:
                    record.command_action = mz_by_room[r_name].action

            if record.command_action in ("HEAT", "OFF"):
                record.command_setpoint = (
                    float(self.settings["heatOnSetpoint"])
                    if record.command_action == "HEAT"
                    else float(self.settings["heatOffSetpoint"])
                )
                await self._dispatch(rm["climateEntity"], record)

            record.saved_at = now_kst_iso()
            payload = record.to_payload()
            self.db.insert_cycle(payload)
            results.append(payload)

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
                act = (recheck.get("attributes") or {}).get("hvac_action")
                record.combustion_confirmed = (act == "heating") if act is not None else None
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

        master_floor, _, _ = await engine.ha.get_numeric_state(engine.settings["masterFloorEntity"])
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
            live_by_room[r_name] = {
                "roomTemp": r_temp,
                "floorTemp": eff_floor,
                "climateState": c_state.get("state") if c_state else None,
                "climateSetpoint": (c_state.get("attributes") or {}).get("temperature") if c_state else None,
            }

        now_hour = datetime.now(tz=KST).hour
        sched_offset, sched_label = engine.db.active_schedule_offset(now_hour)

        return {
            "timeKst": now_kst_iso(),
            "haConnected": engine.ha.is_configured,
            "tokenSource": getattr(engine.ha, "token_source", "mock"),
            "haLastError": getattr(engine.ha, "last_error", None),
            "masterFloorTemp": master_floor,
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
                "hvac_action": (after.get("attributes") or {}).get("hvac_action") if after else None,
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
        engine.db.set_kv("settings", engine.settings)
        return engine.settings

    @app.post("/api/v1/preset")
    async def set_preset(req: PresetRequest) -> dict[str, Any]:
        res = engine.apply_preset(req.preset)
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
