"""Standardized state, prediction, and KST telemetry schemas for I.T.S.S."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ..const import KST


def now_kst_iso() -> str:
    """Return the current timestamp in ISO-8601 KST (+09:00) format."""
    return datetime.now(tz=KST).isoformat(timespec="seconds")


@dataclass(slots=True)
class SensorSnapshot:
    """Point-in-time sensor and device telemetry for a single room."""

    room_name: str
    room_temp: float | None
    floor_temp: float | None
    humidity: float | None = None
    outdoor_temp: float | None = None
    thermostat_setpoint: float | None = None
    thermostat_hvac_mode: str | None = None
    combustion_confirmed: bool | None = None
    manual_override_active: bool = False
    sensor_measured_at_room: str | None = None
    sensor_measured_at_floor: str | None = None
    room_sensor_age_sec: float | None = None
    floor_sensor_age_sec: float | None = None


@dataclass(slots=True)
class SafetyVerdict:
    """Output of ITSS-P000 SENTINEL safety evaluation."""

    allow_heating: bool
    force_off: bool
    hold_current_state: bool
    reason_code: str
    message: str


@dataclass(slots=True)
class ThermalInput:
    """Standard input shared by both P200 v1.2 linear and adaptive RC models."""

    room_temp: float
    floor_temp: float
    target_temp: float
    is_heating: bool
    v_floor_per_min: float = 0.0
    v_room_per_min: float = 0.0
    outdoor_temp: float | None = None
    atmos_loss_offset_c: float = 0.0
    circadia_target_offset_c: float = 0.0
    horizon_min: int = 30


@dataclass(slots=True)
class ThermalPrediction:
    """Standard prediction output returned by any P200 predictor implementation."""

    model_name: str
    recommended_action: str  # "HEAT", "OFF", or "HOLD"
    predicted_room_temp: float
    predicted_floor_temp: float
    effective_target_temp: float
    residual_learning_status: str  # "READY", "INSUFFICIENT_SAMPLES", "TRANSIENT_SKIP"
    estimated_residual_rise_c: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CycleRecord:
    """Full audit record for one control cycle with strict KST timestamp separation."""

    cycle_id: str
    room_name: str
    protocol_version: str = "ITSS-v1.0"

    # Distinct KST timestamps to prevent null/ambiguous audit logs
    scheduled_at: str | None = None
    execution_started_at: str | None = None
    sensor_measured_at_room: str | None = None
    sensor_measured_at_floor: str | None = None
    control_issued_at: str | None = None
    control_confirmed_at: str | None = None
    observed_at: str | None = None
    saved_at: str | None = None

    # Execution & chattering tracking
    execution_status: str = "EXECUTED"  # EXECUTED, DELAYED, SKIPPED_SAFETY, FAILED
    delay_sec: float | None = None
    opposing_command_interval_sec: float | None = None

    # Measured & predicted values
    room_temp: float | None = None
    floor_temp: float | None = None
    target_temp: float | None = None
    predicted_room_30m: float | None = None
    predicted_floor_30m: float | None = None
    predictor_model: str | None = None
    residual_learning_status: str | None = None

    # Command vs actual device/combustion state
    safety_reason: str | None = None
    command_action: str | None = None          # HEAT, OFF, HOLD
    command_setpoint: float | None = None      # Rounded to 0.5 degC
    device_mode_event: str | None = None       # Observed thermostat mode
    combustion_confirmed: bool | None = None   # Actual boiler/valve confirmation

    def to_payload(self) -> dict[str, Any]:
        """Serialize to camelCase JSON payload compatible with I.T.S.S. SCADA API."""
        raw = asdict(self)
        return {
            "cycleId": raw["cycle_id"],
            "roomName": raw["room_name"],
            "protocolVersion": raw["protocol_version"],
            "scheduledAt": raw["scheduled_at"],
            "executionStartedAt": raw["execution_started_at"],
            "sensorMeasuredAtRoom": raw["sensor_measured_at_room"],
            "sensorMeasuredAtFloor": raw["sensor_measured_at_floor"],
            "controlIssuedAt": raw["control_issued_at"],
            "controlConfirmedAt": raw["control_confirmed_at"],
            "observedAt": raw["observed_at"],
            "savedAt": raw["saved_at"],
            "executionStatus": raw["execution_status"],
            "delaySec": raw["delay_sec"],
            "opposingCommandIntervalSec": raw["opposing_command_interval_sec"],
            "roomTemp": raw["room_temp"],
            "floorTemp": raw["floor_temp"],
            "targetTemp": raw["target_temp"],
            "predictedRoom30m": raw["predicted_room_30m"],
            "predictedFloor30m": raw["predicted_floor_30m"],
            "predictorModel": raw["predictor_model"],
            "residualLearningStatus": raw["residual_learning_status"],
            "safetyReason": raw["safety_reason"],
            "commandAction": raw["command_action"],
            "commandSetpoint": raw["command_setpoint"],
            "deviceModeEvent": raw["device_mode_event"],
            "combustionConfirmed": raw["combustion_confirmed"],
        }

