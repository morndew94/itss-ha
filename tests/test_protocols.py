"""Unit tests for ITSS P000-P400 protocols, KST schema, and SCADA DB/RC optimizer."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from custom_components.itss.const import KST
from custom_components.itss.models.schema import CycleRecord, SensorSnapshot, ThermalInput
from custom_components.itss.protocols.p000_sentinel import SentinelProtocol
from custom_components.itss.protocols.p100_core import (
    compute_opposing_interval,
    round_thermostat_setpoint,
)
from custom_components.itss.protocols.p200_therm import (
    AdaptiveRCModel,
    LinearExtrapolationV12,
    ResidualHeatStats,
)
from custom_components.itss.protocols.p300_atmos import AtmosProtocol
from custom_components.itss.protocols.p400_circadia import CircadiaProtocol
from custom_components.itss.protocols.rc_optimizer import RCObservation, fit_rc_parameters
from custom_components.itss.storage.db import ScadaDatabase


def test_p000_overheat_cutoff():
    sentinel = SentinelProtocol()
    snap = SensorSnapshot(room_name="living", room_temp=27.2, floor_temp=26.5)
    verdict = sentinel.evaluate(snap, is_heating=True)
    assert verdict.force_off is True
    assert verdict.reason_code == "P000_OVERHEAT_CUTOFF"


def test_p000_general_floor_limit():
    sentinel = SentinelProtocol()
    snap = SensorSnapshot(room_name="living", room_temp=23.0, floor_temp=28.1)
    verdict = sentinel.evaluate(snap, is_heating=True)
    assert verdict.force_off is True
    assert verdict.reason_code == "P000_FLOOR_LIMIT_OFF"


def test_p000_anti_chattering_hold():
    sentinel = SentinelProtocol()
    snap = SensorSnapshot(room_name="living", room_temp=22.5, floor_temp=24.0)
    verdict = sentinel.evaluate(snap, is_heating=True, seconds_since_last_switch=120.0)
    assert verdict.hold_current_state is True
    assert verdict.reason_code == "P000_ANTI_CHATTER"


def test_p100_half_degree_rounding():
    assert round_thermostat_setpoint(23.1, action="HEAT") == 23.5
    assert round_thermostat_setpoint(23.4, action="OFF") == 23.0
    assert compute_opposing_interval("HEAT", "OFF", 650.4) == 650.4
    assert compute_opposing_interval("HEAT", "HEAT", 650.4) is None


def test_p200_linear_v12_and_rc_shared_interface():
    thermal_input = ThermalInput(
        room_temp=22.9,
        floor_temp=26.0,
        target_temp=23.0,
        is_heating=True,
        v_floor_per_min=0.05,
        v_room_per_min=0.01,
        outdoor_temp=2.0,
    )
    linear_pred = LinearExtrapolationV12().predict(thermal_input, ResidualHeatStats(sample_count=2))
    assert linear_pred.residual_learning_status == "INSUFFICIENT_SAMPLES"
    assert linear_pred.recommended_action == "OFF"

    rc_pred = AdaptiveRCModel().predict(thermal_input)
    assert rc_pred.model_name == "adaptive_rc"
    assert rc_pred.recommended_action in ("HEAT", "OFF")


def test_p300_and_p400_offsets():
    dawn_kst = datetime(2026, 11, 20, 3, 0, tzinfo=KST)
    assert AtmosProtocol().compute_offset(outdoor_temp=None, now_kst=dawn_kst) == 0.05
    assert CircadiaProtocol().compute_offset(now_kst=dawn_kst) == -0.3


def test_scada_db_and_rc_optimizer(tmp_path: Path):
    db = ScadaDatabase(str(tmp_path / "test_scada.db"))
    rec = CycleRecord(
        cycle_id="20261120-1800-living",
        room_name="living",
        scheduled_at="2026-11-20T18:00:00+09:00",
        execution_started_at="2026-11-20T18:00:01+09:00",
        command_action="HEAT",
        room_temp=22.8,
        floor_temp=25.1,
        target_temp=23.0,
    )
    db.insert_cycle(rec.to_payload())
    rows = db.list_cycles(room_name="living")
    assert len(rows) == 1
    assert rows[0]["cycleId"] == "20261120-1800-living"

    # Verify insufficient sample guard in RC optimizer
    short_result = fit_rc_parameters(
        [RCObservation(300.0, 25.0, 22.8, 3.0, True, 25.3, 22.9)]
    )
    assert short_result["status"] == "INSUFFICIENT_SAMPLES"

