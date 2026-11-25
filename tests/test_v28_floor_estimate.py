"""v2.8.0 바닥 센서 없는 방의 추정 바닥값 ON/OFF 및 offset 조절 테스트."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import itss_scada.app.main as main_mod
from itss_scada.app.const import DEFAULT_OVERHEAT_FLOOR_C, KST
from itss_scada.app.main import ItssEngine, create_app
from itss_scada.app.models.schema import CycleRecord
from itss_scada.app.protocols.multi_zone import MultiZoneController, ZoneReading
from itss_scada.app.storage.db import ScadaDatabase
from test_v27_hardening import FakeHA, FakeNotifier, NoWeather

REPO = Path(__file__).resolve().parents[1]
MASTER, SLAVE = "거실", "작은방"


@pytest.fixture(autouse=True)
def _fast_dispatch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_mod, "DEFAULT_RECHECK_DELAY_SEC", 0.0)


def _setup(tmp_path: Path, *, estimate: bool, slave_mode: str = "AUTO", slave_manual: str = "HOLD"):
    ha = FakeHA()
    eng = ItssEngine(db_path=str(tmp_path / "itss.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    eng.settings["masterFloorEntity"] = "sensor.floor"
    eng.db.upsert_room({"roomName": MASTER, "roomTempEntity": "sensor.master_temp",
                        "climateEntity": "climate.master", "hasFloorSensor": True, "targetTemp": 22.0})
    eng.db.upsert_room({"roomName": SLAVE, "roomTempEntity": "sensor.slave_temp",
                        "climateEntity": "climate.slave", "hasFloorSensor": False, "floorOffsetC": 0.4,
                        "floorEstimateEnabled": estimate, "targetTemp": 21.0,
                        "controlMode": slave_mode, "manualAction": slave_manual})
    ha.set_sensor("sensor.floor", 21.0)
    ha.set_sensor("sensor.master_temp", 22.0)
    ha.set_sensor("sensor.slave_temp", 19.0)
    return eng, ha


def _slave(eng: ItssEngine) -> dict:
    recs = asyncio.run(eng.run_control_cycle())
    return next(r for r in recs if r["roomName"] == SLAVE)


def test_estimate_on_uses_master_plus_offset(tmp_path: Path):
    eng, _ = _setup(tmp_path, estimate=True)
    rec = _slave(eng)
    assert rec["floorSensorSource"] == "ESTIMATED" and rec["floorTemp"] == 21.4
    assert rec["predictedRoom30m"] is not None  # P200 사용
    assert rec["diagnostics"]["guardFloor"] == 21.4


def test_estimate_off_is_independent_room_without_floor(tmp_path: Path):
    # v2.9.0: 추정 OFF = 독립 제어 방. 바닥값·안전 판정용 바닥 모두 없음 (v2.8 마스터 대리 판정 폐기)
    eng, _ = _setup(tmp_path, estimate=False)
    rec = _slave(eng)
    assert rec["floorSensorSource"] == "ESTIMATE_OFF" and rec["floorTemp"] is None
    assert rec["predictedRoom30m"] is None  # P200 생략, 실내 기준 다중방 band 제어
    assert rec["safetyReason"] == "HYBRID_PRIMARY_HEAT" and rec["commandAction"] == "HEAT"
    assert rec["diagnostics"]["guardFloor"] is None and rec["diagnostics"]["floorEstimateEnabled"] is False
    assert rec["diagnostics"]["groupRole"] is None


def test_independent_room_not_blocked_by_master_floor(tmp_path: Path):
    # 2026-11-25 07:40 재현: 마스터 바닥 과열이 바닥 기준 없는 방까지 차단하던 문제
    eng, ha = _setup(tmp_path, estimate=False)
    ha.set_sensor("sensor.floor", DEFAULT_OVERHEAT_FLOOR_C + 0.6)
    recs = {r["roomName"]: r for r in asyncio.run(eng.run_control_cycle())}
    assert recs[MASTER]["safetyReason"] == "P000_OVERHEAT_CUTOFF"
    assert recs[SLAVE]["commandAction"] == "HEAT" and recs[SLAVE]["safetyReason"] == "HYBRID_PRIMARY_HEAT"

    # 설정 바닥 상한 초과·마스터 바닥 센서 누락에도 독립 방은 실내 기준으로 계속 제어
    eng2, ha2 = _setup(tmp_path / "b", estimate=False)
    eng2.settings["floorOffLimitC"] = 22.5
    ha2.set_sensor("sensor.floor", 22.8)
    assert _slave(eng2)["commandAction"] == "HEAT"
    eng3, ha3 = _setup(tmp_path / "c", estimate=False)
    ha3.set_sensor("sensor.floor", None)
    assert _slave(eng3)["commandAction"] == "HEAT"


def test_manual_heat_on_independent_room_needs_only_room_sensor(tmp_path: Path):
    eng, ha = _setup(tmp_path, estimate=False, slave_mode="MANUAL", slave_manual="HEAT")
    assert _slave(eng)["commandAction"] == "HEAT"
    ha.set_sensor("sensor.floor", None)  # 마스터 바닥 누락과 무관
    assert _slave(eng)["commandAction"] == "HEAT"
    ha.set_sensor("sensor.slave_temp", None)
    rec = _slave(eng)
    assert rec["commandAction"] == "OFF" and rec["safetyReason"] == "P000_SENSOR_MISSING"


def test_multizone_independent_zone_has_no_floor():
    # main.py 와 동일하게 설정 바닥 상한(floorOffLimitC 기본 26.5°C)을 명시 전달
    ctrl = MultiZoneController(strategy="hybrid", general_off_floor_c=26.5)
    indep = ZoneReading("s", room_temp=19.0, target_temp=21.0, is_heating=False,
                        floor_offset_from_master_c=0.5, floor_estimate_enabled=False)
    cmd = ctrl.evaluate([indep], master_floor_temp=20.0).commands[0]
    assert cmd.floor_sensor_source == "NONE" and cmd.effective_floor_temp is None and cmd.action == "HEAT"
    # 마스터 바닥 과열이어도 독립 방은 차단하지 않고, 바닥 기준 방만 차단
    floor_zone = ZoneReading("m", room_temp=19.0, target_temp=21.0, is_heating=False, has_floor_sensor=True)
    by = {c.room_name: c for c in ctrl.evaluate([indep, floor_zone], master_floor_temp=30.0).commands}
    assert by["s"].action == "HEAT" and by["m"].reason == "P000_SAFETY_CUTOFF"


def test_api_toggle_offset_validation_and_suggestions(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    eng = app.state.engine
    c = TestClient(app)
    assert c.post("/api/v1/rooms", json={"roomName": MASTER, "roomTempEntity": "sensor.master_temp",
                                         "climateEntity": "climate.master", "hasFloorSensor": True}).status_code == 200
    r = c.post("/api/v1/rooms", json={"roomName": SLAVE, "roomTempEntity": "sensor.slave_temp",
                                      "climateEntity": "climate.slave", "floorEstimateEnabled": False})
    assert r.status_code == 200 and r.json()["floorEstimateEnabled"] is False

    r = c.patch(f"/api/v1/rooms/{SLAVE}", json={"floorEstimateEnabled": True, "floorOffsetC": 0.7})
    assert r.status_code == 200 and r.json()["floorEstimateEnabled"] is True and r.json()["floorOffsetC"] == 0.7
    assert c.patch(f"/api/v1/rooms/{SLAVE}", json={"floorOffsetC": 6.0}).status_code == 422
    assert c.patch(f"/api/v1/rooms/{MASTER}", json={"floorOffsetC": 0.3}).status_code == 409
    assert c.patch(f"/api/v1/rooms/{MASTER}", json={"floorEstimateEnabled": False}).status_code == 409

    # RC 피팅/보정 실행 시 슬레이브 offset 제안값이 저장되어 /status 로 노출 (자동 적용 안 함)
    now = datetime.now(tz=KST)
    for k in range(4):
        for name, temp in ((MASTER, 22.0), (SLAVE, 21.4)):
            rec = CycleRecord(cycle_id=f"{name}-{k}", room_name=name, room_temp=temp, target_temp=22.0,
                              execution_started_at=(now - timedelta(minutes=10 * k)).isoformat(timespec="seconds"))
            eng.db.insert_cycle(rec.to_payload())
    cal = c.post("/api/v1/calibrate/auto").json()
    assert cal["suggestedFloorOffsets"] == {SLAVE: -0.6} and cal["floorOffsetsApplied"] is False
    st = c.get("/api/v1/status").json()
    assert st["suggestedFloorOffsets"]["offsets"] == {SLAVE: -0.6}
    assert eng.db.get_room(SLAVE)["floorOffsetC"] == 0.7  # 자동 적용 안 됨
    assert {"floorSrc", "guardFloorTemp"} <= set(st["liveByRoom"][SLAVE])

    html = (REPO / "itss_scada" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-act="estimate"' in html and 'data-act="offset"' in html and "newFloorEstimate" in html


def test_db_migration_adds_estimate_column_default_on(tmp_path: Path):
    db_file = tmp_path / "old.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """CREATE TABLE rooms (room_name TEXT PRIMARY KEY, room_temp_entity TEXT NOT NULL,
               climate_entity TEXT NOT NULL, window_sensor_entity TEXT NOT NULL DEFAULT '',
               has_floor_sensor INTEGER NOT NULL DEFAULT 0, floor_offset_c REAL NOT NULL DEFAULT 0.0,
               target_temp REAL NOT NULL DEFAULT 23.0, control_mode TEXT NOT NULL DEFAULT 'AUTO',
               manual_action TEXT NOT NULL DEFAULT 'HOLD', weight REAL NOT NULL DEFAULT 1.0,
               updated_at TEXT NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO rooms (room_name, room_temp_entity, climate_entity, floor_offset_c, updated_at) "
            "VALUES ('작은방', 'sensor.s', 'climate.s', 0.3, '2026-11-24T00:00:00+09:00')"
        )
    room = ScadaDatabase(str(db_file)).get_room("작은방")
    assert room["floorEstimateEnabled"] is True and room["floorOffsetC"] == 0.3
