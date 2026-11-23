"""FastAPI application for the I.T.S.S. SCADA Home Assistant Add-on."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel

from .db import ScadaDatabase
from .rc_optimizer import RCObservation, fit_rc_parameters

KST = timezone(timedelta(hours=9))
DB_PATH = os.environ.get("ITSS_DB_PATH", "./itss_scada.db")

app = FastAPI(title="I.T.S.S. SCADA Server", version="1.0.0")
db = ScadaDatabase(DB_PATH)


class CyclePayload(BaseModel):
    """Minimal validated envelope for incoming KST cycle telemetry."""

    cycleId: str
    roomName: str
    scheduledAt: str | None = None
    executionStartedAt: str | None = None
    sensorMeasuredAtRoom: str | None = None
    sensorMeasuredAtFloor: str | None = None
    controlIssuedAt: str | None = None
    controlConfirmedAt: str | None = None
    observedAt: str | None = None
    savedAt: str | None = None
    executionStatus: str = "EXECUTED"
    delaySec: float | None = None
    opposingCommandIntervalSec: float | None = None
    roomTemp: float | None = None
    floorTemp: float | None = None
    targetTemp: float | None = None
    predictedRoom30m: float | None = None
    predictedFloor30m: float | None = None
    predictorModel: str | None = None
    residualLearningStatus: str | None = None
    safetyReason: str | None = None
    commandAction: str | None = None
    commandSetpoint: float | None = None
    deviceModeEvent: str | None = None
    combustionConfirmed: bool | None = None


class RCFitRequest(BaseModel):
    """Request payload for RC parameter estimation."""

    observations: list[dict[str, Any]]


@app.get("/health")
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "timeKst": datetime.now(tz=KST).isoformat(timespec="seconds")}


@app.post("/api/v1/cycles")
def create_cycle(payload: CyclePayload) -> dict[str, str]:
    """Store one cycle audit record with server-side KST savedAt fallback."""
    data = payload.model_dump()
    if not data.get("savedAt"):
        data["savedAt"] = datetime.now(tz=KST).isoformat(timespec="seconds")
    db.insert_cycle(data)
    return {"status": "saved", "cycleId": payload.cycleId}


@app.get("/api/v1/cycles")
def get_cycles(
    room_name: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Return recent cycle audit records."""
    items = db.list_cycles(room_name=room_name, limit=limit)
    return {"count": len(items), "items": items}


@app.post("/api/v1/rc/fit")
def fit_rc(req: RCFitRequest) -> dict[str, Any]:
    """Run 2-node RC parameter optimization on submitted observations."""
    obs_list = [
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
    return fit_rc_parameters(obs_list)

