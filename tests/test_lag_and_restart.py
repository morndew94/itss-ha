"""Unit tests for residual cooldown survival across restarts and thermal lag learning."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from itss_scada.app.const import KST
from itss_scada.app.main import create_app
from itss_scada.app.protocols.thermal_lag_learner import learn_room_lag_profile


class StaticFakeHA:
    """Mock HA client with controllable sensor/climate states."""

    def __init__(self, room_temp: float = 21.0, floor_temp: float = 25.5, heating: bool = False) -> None:
        self.is_configured = True
        self.token_source = "mock"
        self.last_error = None
        self.room_temp = room_temp
        self.floor_temp = floor_temp
        self.heating = heating
        self.calls: list[tuple[str, str, dict]] = []

    async def get_numeric_state(self, entity_id: str):
        if "floor" in entity_id:
            return self.floor_temp, datetime.now(tz=KST).isoformat(timespec="seconds"), 5.0
        return self.room_temp, datetime.now(tz=KST).isoformat(timespec="seconds"), 5.0

    async def get_state(self, entity_id: str):
        sp = 24.0 if self.heating else 15.0
        return {"state": "heat", "attributes": {"temperature": sp, "current_temperature": 21.0}}

    async def call_service(self, domain: str, service: str, data: dict):
        self.calls.append((domain, service, data))


def test_residual_cooldown_survives_addon_restart(tmp_path: Path):
    db_file = str(tmp_path / "restart.db")
    ha1 = StaticFakeHA(room_temp=21.2, floor_temp=26.7, heating=True)

    # Instance 1: floor hits 26.7C (>= 26.5C limit) -> forces OFF and enters 20m cooldown
    app1 = create_app(db_path=db_file, ha_client=ha1)
    with TestClient(app1) as c1:
        c1.patch("/api/v1/settings", json={"masterFloorEntity": "sensor.floor_temp", "dryRun": False})
        c1.post("/api/v1/rooms", json={
            "roomName": "방3",
            "roomTempEntity": "sensor.room_temp",
            "climateEntity": "climate.room_heater",
            "hasFloorSensor": True,
            "targetTemp": 22.5,
        })
        r1 = c1.post("/api/v1/control/run-now").json()
        assert r1["records"][0]["safetyReason"] == "P000_FLOOR_LIMIT_OFF"
        assert r1["records"][0]["commandAction"] == "OFF"

    # Simulate add-on RESTART: create brand new app instance on same SQLite DB
    # Floor has cooled slightly to 25.5C and room is still below target (21.2C < 22.5C),
    # yet cooldown MUST prevent turning the boiler back ON!
    ha2 = StaticFakeHA(room_temp=21.2, floor_temp=25.5, heating=False)
    app2 = create_app(db_path=db_file, ha_client=ha2)
    with TestClient(app2) as c2:
        r2 = c2.post("/api/v1/control/run-now").json()
        assert r2["records"][0]["safetyReason"] == "P000_RESIDUAL_COOLDOWN"
        assert r2["records"][0]["commandAction"] == "OFF"
        # Verify boiler was NEVER commanded to 24.0C after restart
        heat_calls = [d for _, svc, d in ha2.calls if svc == "set_temperature" and d.get("temperature") == 24.0]
        assert heat_calls == []


def test_thermal_lag_learner_extracts_floor_and_room_delays():
    base = datetime(2026, 11, 23, 10, 0, tzinfo=KST)
    cycles = []
    # Generate 3 realistic Ondol heating/cooling episodes (1-minute resolution)
    for ep in range(3):
        t0 = base + timedelta(minutes=ep * 60)
        # 10 min heating: floor starts rising at +4m, room starts rising at +12m (after OFF)
        for m in range(10):
            ft = 24.0 + (0.0 if m < 4 else (m - 3) * 0.2)
            rt = 22.0
            cycles.append({
                "roomName": "방3",
                "executionStartedAt": (t0 + timedelta(minutes=m)).isoformat(timespec="seconds"),
                "commandAction": "HEAT",
                "combustionConfirmed": True,
                "floorTemp": round(ft, 1),
                "roomTemp": round(rt, 1),
            })
        # 30 min post-OFF soak: floor peaks +8m after OFF (+0.6C), room peaks +22m after OFF (+0.4C)
        for m in range(30):
            ts = t0 + timedelta(minutes=10 + m)
            ft = 25.2 + (0.6 * (m / 8.0) if m <= 8 else max(0.0, 0.6 - (m - 8) * 0.03))
            rt = 22.0 + (0.0 if m < 2 else (0.4 * ((m - 2) / 20.0) if m <= 22 else max(0.0, 0.4 - (m - 22) * 0.02)))
            cycles.append({
                "roomName": "방3",
                "executionStartedAt": ts.isoformat(timespec="seconds"),
                "commandAction": "OFF",
                "combustionConfirmed": False,
                "floorTemp": round(ft, 2),
                "roomTemp": round(rt, 2),
            })

    prof = learn_room_lag_profile("방3", cycles, base_cooldown_min=20)
    assert prof.status == "LEARNING"
    assert prof.sample_count == 3
    assert prof.floor_on_deadtime_min == 4.0
    assert prof.floor_post_off_peak_min == 8.0
    assert prof.floor_residual_rise_c >= 0.5
    assert prof.room_post_off_peak_min == 22.0
    assert prof.room_residual_rise_c >= 0.35
    assert prof.recommended_cooldown_min == 22
    assert prof.recommended_run_trim_min >= 1.0

    # Verify ventilation-contaminated episodes are excluded from lag learning
    vent_cycles = []
    for c in cycles:
        vc = dict(c)
        if vc["commandAction"] == "OFF":
            vc["safetyReason"] = "P000_VENTILATION_PROTECT"
        vent_cycles.append(vc)
    vent_prof = learn_room_lag_profile("방3", vent_cycles)
    assert vent_prof.sample_count == 0
    assert vent_prof.status == "INSUFFICIENT_SAMPLES"


def test_ventilation_5m_window_latch_and_contact_sensor(tmp_path: Path):
    from itss_scada.app.protocols.p000_sentinel import SentinelProtocol
    from itss_scada.app.models.schema import SensorSnapshot

    sentinel = SentinelProtocol()
    snap = SensorSnapshot("방3", room_temp=20.8, floor_temp=25.0)

    # 5-min air-only drop (-0.6C room, 0.0C floor) triggers ventilation even if 1m slope is gentle
    v5 = sentinel.evaluate(snap, is_heating=False, v_room_per_min=-0.10, drop_5m_room_c=-0.6, drop_5m_floor_c=0.0)
    assert v5.force_off is True
    assert v5.reason_code == "P000_VENTILATION_PROTECT"

    # Normal cooling where both air and floor cool together does NOT false-trigger ventilation
    v_norm = sentinel.evaluate(snap, is_heating=False, v_room_per_min=-0.05, drop_5m_room_c=-0.5, drop_5m_floor_c=-0.4)
    assert v_norm.reason_code == "P000_OK"

    # Physical window contact sensor open -> 0-sec immediate cutoff
    v_win = sentinel.evaluate(snap, is_heating=True, window_open=True)
    assert v_win.force_off is True
    assert v_win.reason_code == "P000_WINDOW_OPEN"

    # One-touch VENTILATION preset latches hold and survives restart
    db_file = str(tmp_path / "vent.db")
    ha = StaticFakeHA(room_temp=20.5, floor_temp=25.0, heating=False)
    app1 = create_app(db_path=db_file, ha_client=ha)
    with TestClient(app1) as c1:
        c1.patch("/api/v1/settings", json={"masterFloorEntity": "sensor.floor_temp", "ventilationHoldMin": 20})
        c1.post("/api/v1/rooms", json={
            "roomName": "방3",
            "roomTempEntity": "sensor.room_temp",
            "climateEntity": "climate.room_heater",
            "windowSensorEntity": "binary_sensor.room_window",
            "hasFloorSensor": True,
            "targetTemp": 22.5,
        })
        p_res = c1.post("/api/v1/preset", json={"preset": "VENTILATION"}).json()
        assert p_res["ventilationHoldMin"] == 20

    # After simulated restart, ventilation hold MUST remain active
    app2 = create_app(db_path=db_file, ha_client=ha)
    with TestClient(app2) as c2:
        r2 = c2.post("/api/v1/control/run-now").json()
        assert r2["records"][0]["safetyReason"] == "P000_VENTILATION_HOLD"
        assert r2["records"][0]["commandAction"] == "OFF"

