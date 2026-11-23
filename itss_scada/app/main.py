"""FastAPI SCADA Server & Ingress Web UI for the I.T.S.S. Home Assistant Add-on."""

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
    MODEL_ADAPTIVE_RC,
    MODEL_LINEAR_V12,
    STRATEGY_HYBRID,
)
from .ha_client import HomeAssistantClient
from .integrations.google_alerts import GeminiTTSAlert
from .integrations.google_gemini_advisor import GeminiThermalAdvisor, compute_daily_kpis
from .integrations.google_weather import GoogleWeatherClient
from .models.schema import CycleRecord, SensorSnapshot, now_kst_iso
from .protocols.multi_zone import MultiZoneController, ZoneReading
from .protocols.p000_sentinel import SentinelProtocol
from .protocols.p100_core import round_thermostat_setpoint
from .protocols.p300_atmos import AtmosProtocol
from .protocols.p400_circadia import CircadiaProtocol
from .protocols.rc_optimizer import RCObservation, fit_rc_parameters
from .storage.db import ScadaDatabase

STATIC_DIR = Path(__file__).parent / "static"


def _load_options() -> dict[str, Any]:
    """Load Home Assistant Add-on options.json when present."""
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


class RCFitRequest(BaseModel):
    observations: list[dict[str, Any]]


class ItssEngine:
    """Core runtime state and multi-zone P000-P400 control engine for the Add-on."""

    def __init__(self, db_path: str | None = None, ha_client: HomeAssistantClient | None = None) -> None:
        opts = _load_options()
        resolved_db = db_path or os.environ.get("ITSS_DB_PATH") or opts.get("db_path") or "./itss_scada.db"
        self.db = ScadaDatabase(resolved_db)
        self.ha = ha_client or HomeAssistantClient()

        self.settings: dict[str, Any] = {
            "dryRun": bool(opts.get("dry_run", True)),
            "zoneStrategy": str(opts.get("zone_strategy", STRATEGY_HYBRID)),
            "predictorModel": str(opts.get("predictor_model", MODEL_LINEAR_V12)),
            "updateIntervalSec": int(opts.get("update_interval_sec", 300)),
            "masterFloorEntity": str(opts.get("master_floor_entity", "sensor.living_floor_temperature")),
            "weatherLocation": str(opts.get("weather_location", "서울")),
            "heatOnSetpoint": float(opts.get("heat_on_setpoint", 24.0)),
            "heatOffSetpoint": float(opts.get("heat_off_setpoint", 15.0)),
        }

        gemini_key = str(opts.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
        self.weather_client = GoogleWeatherClient(gemini_key, self.settings["weatherLocation"]) if gemini_key else None
        self.advisor = GeminiThermalAdvisor(gemini_key)
        self.tts_alert = GeminiTTSAlert(gemini_key) if gemini_key else None
        self.sentinel = SentinelProtocol()
        self.atmos = AtmosProtocol()
        self.circadia = CircadiaProtocol()

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
        circadia_offset = self.circadia.compute_offset(now_kst=now)

        # Gather per-room telemetry
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

            eff_target = round(rm["targetTemp"] + atmos_offset + circadia_offset, 2)
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
            eff_floor = (
                round(master_floor + (0.0 if has_floor else rm["floorOffsetC"]), 1)
                if master_floor is not None
                else None
            )
            floor_src = "MEASURED" if has_floor else "ESTIMATED"

            is_manual = rm["controlMode"] == "MANUAL"
            snapshot = SensorSnapshot(
                room_name=r_name,
                room_temp=t["roomTemp"],
                floor_temp=eff_floor,
                outdoor_temp=outdoor_temp,
                thermostat_hvac_mode=t["hvacMode"],
                combustion_confirmed=t["combustion"],
                manual_override_active=False,  # Evaluated explicitly below with overheat protection
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

            # Hard overheat protection still applies even in MANUAL mode
            overheat = (
                (t["roomTemp"] is not None and t["roomTemp"] >= 27.0)
                or (eff_floor is not None and eff_floor >= 29.0)
            )
            if overheat:
                record.safety_reason = "P000_OVERHEAT_CUTOFF"
                record.command_action = "OFF"
                record.execution_status = "SKIPPED_SAFETY"
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
                # Fixed boiler swing setpoints: ON -> 24.0C (boiler ON), OFF -> 15.0C (boiler OFF)
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
        """Issue climate command to HA Supervisor API (unless dryRun is True) and re-read.

        Uses setpoint swing control for Korean wallpad thermostats:
          - HEAT: set hvac_mode='heat' and temperature=heatOnSetpoint (default 24.0C)
          - OFF : set temperature=heatOffSetpoint (default 15.0C) to turn boiler off
        """
        record.control_issued_at = now_kst_iso()
        if self.settings["dryRun"] or not self.ha.is_configured:
            record.control_confirmed_at = now_kst_iso()
            record.observed_at = now_kst_iso()
            return

        try:
            if record.command_action == "HEAT":
                await self.ha.call_service(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": climate_entity, "hvac_mode": "heat"},
                )
                await self.ha.call_service(
                    "climate",
                    "set_temperature",
                    {"entity_id": climate_entity, "temperature": record.command_setpoint},
                )
            elif record.command_action == "OFF":
                await self.ha.call_service(
                    "climate",
                    "set_temperature",
                    {"entity_id": climate_entity, "temperature": record.command_setpoint},
                )
            record.control_confirmed_at = now_kst_iso()

            await asyncio.sleep(DEFAULT_RECHECK_DELAY_SEC)
            recheck = await self.ha.get_state(climate_entity)
            record.observed_at = now_kst_iso()
            if recheck:
                record.device_mode_event = recheck.get("state")
                act = (recheck.get("attributes") or {}).get("hvac_action")
                record.combustion_confirmed = (act == "heating") if act is not None else None
        except Exception:  # noqa: BLE001
            record.execution_status = "FAILED"
            record.observed_at = now_kst_iso()


def create_app(db_path: str | None = None, ha_client: HomeAssistantClient | None = None) -> FastAPI:
    """Create configured FastAPI application for production or testing."""
    engine = ItssEngine(db_path=db_path, ha_client=ha_client)

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

    app = FastAPI(title="I.T.S.S. Thermal SCADA", version="1.1.0", lifespan=lifespan)
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
        }

    @app.get("/api/v1/status")
    async def get_status() -> dict[str, Any]:
        rooms = engine.db.list_rooms()
        recent = engine.db.list_cycles(limit=100)
        latest_by_room: dict[str, dict[str, Any]] = {}
        for c in recent:
            if c["roomName"] not in latest_by_room:
                latest_by_room[c["roomName"]] = c

        # Fetch live real-time temperatures directly from HA so UI never stays '--'
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

        return {
            "timeKst": now_kst_iso(),
            "haConnected": engine.ha.is_configured,
            "haLastError": getattr(engine.ha, "last_error", None),
            "masterFloorTemp": master_floor,
            "settings": engine.settings,
            "rooms": rooms,
            "liveByRoom": live_by_room,
            "latestByRoom": latest_by_room,
            "kpis": compute_daily_kpis(recent),
        }

    @app.get("/api/v1/ha/entities")
    async def list_ha_entities() -> dict[str, Any]:
        return await engine.ha.list_entities()

    @app.get("/api/v1/rooms")
    def list_rooms() -> list[dict[str, Any]]:
        return engine.db.list_rooms()

    @app.post("/api/v1/rooms")
    def create_or_update_room(req: RoomCreateRequest) -> dict[str, Any]:
        return engine.db.upsert_room(req.model_dump())

    @app.patch("/api/v1/rooms/{room_name}")
    def patch_room(room_name: str, req: RoomPatchRequest) -> dict[str, Any]:
        updated = engine.db.update_room_control(
            room_name,
            target_temp=req.targetTemp,
            control_mode=req.controlMode,
            manual_action=req.manualAction,
            floor_offset_c=req.floorOffsetC,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Room not found")
        return updated

    @app.delete("/api/v1/rooms/{room_name}")
    def delete_room(room_name: str) -> dict[str, Any]:
        if not engine.db.delete_room(room_name):
            raise HTTPException(status_code=404, detail="Room not found")
        return {"deleted": room_name}

    @app.patch("/api/v1/settings")
    def patch_settings(req: SettingsPatchRequest) -> dict[str, Any]:
        if req.dryRun is not None:
            engine.settings["dryRun"] = req.dryRun
        if req.zoneStrategy is not None:
            engine.settings["zoneStrategy"] = req.zoneStrategy
        if req.predictorModel is not None:
            engine.settings["predictorModel"] = req.predictorModel
        if req.masterFloorEntity is not None:
            engine.settings["masterFloorEntity"] = req.masterFloorEntity
        if req.heatOnSetpoint is not None:
            engine.settings["heatOnSetpoint"] = float(req.heatOnSetpoint)
        if req.heatOffSetpoint is not None:
            engine.settings["heatOffSetpoint"] = float(req.heatOffSetpoint)
        return engine.settings

    @app.post("/api/v1/control/run-now")
    async def run_now() -> dict[str, Any]:
        records = await engine.run_control_cycle()
        return {"executed": len(records), "records": records}

    @app.get("/api/v1/cycles")
    def get_cycles(room_name: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=500)):
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

