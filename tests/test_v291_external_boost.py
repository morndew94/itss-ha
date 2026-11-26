"""v2.9.1 회귀 테스트: 외부 조작(월패드/다른 HA 자동화)으로 켜진 난방 -> 설정 시간(기본 15분) 유지 후 자동 중지.

재현 근거 (2026-11-25 트레이스): 18:41 거실, 19:53 방3·방2 를 조절기에서 24°C 로 올렸으나
ITSS 가 1분 안에 15°C 로 되돌리고 0분 가동에 30분 잔열 휴지까지 걸었던 문제.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import itss_scada.app.main as main_mod
from itss_scada.app.const import KST
from itss_scada.app.main import ItssEngine, create_app
from test_v27_hardening import FakeHA, FakeNotifier, NoWeather

LEADER, FOLLOWER, INDEP = "방3", "방2", "거실"
ENT = {LEADER: "climate.y", FOLLOWER: "climate.s", INDEP: "climate.g"}


@pytest.fixture(autouse=True)
def _fast_dispatch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_mod, "DEFAULT_RECHECK_DELAY_SEC", 0.0)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _house(tmp_path: Path, *, floor: float = 20.0, room: float = 22.0, indep_mode: str = "AUTO"):
    """세 방 모두 목표(21.0) 이상 -> AUTO 는 난방하지 않는 상태. 조절기는 OFF(15°C) 관측."""
    ha = FakeHA()
    notifier = FakeNotifier()
    eng = ItssEngine(db_path=str(tmp_path / "itss.db"), ha_client=ha, notifier=notifier,
                     weather_client=NoWeather(), options={})
    eng.settings.update({"masterFloorEntity": "sensor.floor", "dryRun": False})
    eng.db.upsert_room({"roomName": LEADER, "roomTempEntity": "sensor.y_temp", "climateEntity": ENT[LEADER],
                        "hasFloorSensor": True, "targetTemp": 21.0})
    eng.db.upsert_room({"roomName": FOLLOWER, "roomTempEntity": "sensor.s_temp", "climateEntity": ENT[FOLLOWER],
                        "hasFloorSensor": False, "floorEstimateEnabled": True, "floorOffsetC": -0.4,
                        "targetTemp": 21.0})
    eng.db.upsert_room({"roomName": INDEP, "roomTempEntity": "sensor.g_temp", "climateEntity": ENT[INDEP],
                        "hasFloorSensor": False, "floorEstimateEnabled": False, "targetTemp": 21.0,
                        "controlMode": indep_mode, "manualAction": "OFF"})
    ha.set_sensor("sensor.floor", floor)
    for ent in ("sensor.y_temp", "sensor.s_temp", "sensor.g_temp"):
        ha.set_sensor(ent, room)
    for ent in ENT.values():
        ha.climate[ent] = {"state": "heat", "attributes": {"temperature": 15.0, "current_temperature": 21.0}}
    return eng, ha, notifier


def _cycle(eng: ItssEngine) -> dict[str, dict]:
    return {r["roomName"]: r for r in asyncio.run(eng.run_control_cycle())}


def _wallpad_set(ha: FakeHA, room: str, sp: float) -> None:
    """월패드/다른 자동화가 조절기 설정온도를 직접 바꾼 상황 (ITSS 호출 기록 없음)."""
    ha.climate[ENT[room]]["attributes"]["temperature"] = sp


def _sent(ha: FakeHA, room: str) -> list[float]:
    return [d["temperature"] for _, s, d in ha.calls if s == "set_temperature" and d.get("entity_id") == ENT[room]]


def _age_boost(eng: ItssEngine, room: str, ran_min: float, remain_min: float) -> None:
    """외부 조작 가동이 ran_min 분 진행된 상태로 시각을 당김."""
    now = datetime.now(tz=KST)
    tm = eng.room_timers[room]
    tm["externalBoostStartedAt"] = _iso(now - timedelta(minutes=ran_min))
    tm["externalBoostUntil"] = _iso(now + timedelta(minutes=remain_min))
    tm["heatingStartedAt"] = _iso(now - timedelta(minutes=ran_min))


def test_wallpad_on_runs_15min_then_stops(tmp_path: Path):
    eng, ha, notifier = _house(tmp_path)
    r0 = _cycle(eng)  # 기준 관측: 모두 비가동
    assert r0[INDEP]["commandAction"] == "OFF"
    assert eng.room_timers[INDEP]["lastObservedHeating"] is False

    _wallpad_set(ha, INDEP, 24.0)
    r1 = _cycle(eng)
    assert r1[INDEP]["commandAction"] == "HEAT" and r1[INDEP]["safetyReason"] == "EXTERNAL_BOOST"
    assert r1[INDEP]["dispatchResult"] == "NO_CHANGE" and _sent(ha, INDEP) == []  # 외부 설정값 유지
    assert r1[INDEP]["diagnostics"]["externalBoost"] == "START"
    until = datetime.fromisoformat(eng.room_timers[INDEP]["externalBoostUntil"])
    assert timedelta(minutes=14) < until - datetime.now(tz=KST) <= timedelta(minutes=15)
    msg = next(m for t, m in notifier.sent if INDEP in t and "켜짐" in t)
    assert "외부 조작 감지" in msg and "15분 유지 후 자동 중지" in msg
    # 다른 방은 영향 없음 (합류/그룹 가열 없음)
    assert r1[LEADER]["commandAction"] == "OFF" and r1[FOLLOWER]["commandAction"] == "OFF"

    r2 = _cycle(eng)
    assert r2[INDEP]["safetyReason"] == "EXTERNAL_BOOST" and r2[INDEP]["diagnostics"]["externalBoost"] == "ACTIVE"

    # 15분 경과 -> OFF 전송 + 잔열 휴지
    _age_boost(eng, INDEP, ran_min=15.0, remain_min=-0.1)
    r3 = _cycle(eng)
    assert r3[INDEP]["commandAction"] == "OFF" and r3[INDEP]["safetyReason"] == "EXTERNAL_BOOST_END"
    assert r3[INDEP]["dispatchResult"] == "SENT" and _sent(ha, INDEP)[-1] <= 15.0
    tm = eng.room_timers[INDEP]
    assert "externalBoostUntil" not in tm
    assert datetime.fromisoformat(tm["cooldownUntil"]) > datetime.now(tz=KST)
    assert any(INDEP in t and "꺼짐" in t and "외부 조작 가동 시간 종료" in m for t, m in notifier.sent)

    # 종료 후 다시 외부 조작으로 오인하지 않음
    r4 = _cycle(eng)
    assert r4[INDEP]["commandAction"] == "OFF" and "externalBoost" not in r4[INDEP]["diagnostics"]


def test_wallpad_off_during_boost_is_respected(tmp_path: Path):
    eng, ha, _ = _house(tmp_path)
    _cycle(eng)
    _wallpad_set(ha, INDEP, 24.0)
    assert _cycle(eng)[INDEP]["safetyReason"] == "EXTERNAL_BOOST"

    _wallpad_set(ha, INDEP, 15.0)  # 사용자가 월패드에서 다시 끔 -> ITSS 가 24°C 로 되살리지 않음
    r = _cycle(eng)
    assert r[INDEP]["commandAction"] == "OFF" and r[INDEP]["safetyReason"] == "EXTERNAL_BOOST_CANCELLED"
    assert all(sp < 20.0 for sp in _sent(ha, INDEP))
    tm = eng.room_timers[INDEP]
    assert "externalBoostUntil" not in tm
    assert not tm.get("cooldownUntil")  # 최소 펄스(5분) 미만 가동 -> 잔열 휴지 없음


def test_itss_own_heat_and_unapplied_off_are_not_external(tmp_path: Path):
    # ITSS 가 직접 HEAT 를 보낸 경우 다음 회차에 외부 조작으로 오인하지 않음
    eng, ha, _ = _house(tmp_path / "a", room=18.0)
    r = _cycle(eng)
    assert r[INDEP]["commandAction"] == "HEAT" and r[INDEP]["dispatchResult"] == "SENT"
    r2 = _cycle(eng)
    assert "externalBoost" not in r2[INDEP]["diagnostics"] and r2[INDEP]["safetyReason"] != "EXTERNAL_BOOST"

    # ITSS OFF 명령이 조절기에 반영되지 않은 경우(직전 관측도 가동) -> 외부 조작 아님, OFF 재전송
    eng2, ha2, _ = _house(tmp_path / "b")
    _cycle(eng2)
    _wallpad_set(ha2, INDEP, 24.0)
    eng2.room_timers[INDEP].update({"lastCommandAction": "OFF", "lastObservedHeating": True})
    r3 = _cycle(eng2)
    assert r3[INDEP]["commandAction"] == "OFF" and r3[INDEP]["safetyReason"] != "EXTERNAL_BOOST"
    assert r3[INDEP]["dispatchResult"] == "SENT"


def test_floor_rooms_boost_together_linked(tmp_path: Path):
    # 월패드로 방3 만 켬 -> 같은 바닥의 방2 도 같은 종료 시각으로 함께 가동 (그룹 경로)
    eng, ha, notifier = _house(tmp_path / "a", floor=20.0)
    _cycle(eng)
    _wallpad_set(ha, LEADER, 24.0)
    r = _cycle(eng)
    for n in (LEADER, FOLLOWER):
        assert r[n]["commandAction"] == "HEAT" and r[n]["safetyReason"] == "EXTERNAL_BOOST", n
    assert r[LEADER]["diagnostics"]["groupRole"] == "leader" and r[FOLLOWER]["diagnostics"]["groupRole"] == "follower"
    assert _sent(ha, LEADER) == []  # 방3 은 월패드 설정값 유지 (이미 HEAT 상태)
    assert _sent(ha, FOLLOWER)[-1] >= 20.0  # 방2 는 ITSS 가 함께 켬
    assert eng.room_timers[FOLLOWER]["externalBoostUntil"] == eng.room_timers[LEADER]["externalBoostUntil"]
    assert r[INDEP]["commandAction"] == "OFF"  # 독립 방은 무관
    msg = next(m for t, m in notifier.sent if FOLLOWER in t and "켜짐" in t)
    assert "연동" in msg and "외부 조작 감지" in msg

    r2 = _cycle(eng)
    assert all(r2[n]["safetyReason"] == "EXTERNAL_BOOST" for n in (LEADER, FOLLOWER))

    # 15분 종료 -> 두 방 동시 OFF, 같은 잔열 휴지
    for n in (LEADER, FOLLOWER):
        _age_boost(eng, n, ran_min=15.0, remain_min=-0.1)
    eng.room_timers[LEADER]["heatingStartFloor"] = 18.0  # 상승률 예측 차단과 분리해 종료 경로 확인
    r3 = _cycle(eng)
    for n in (LEADER, FOLLOWER):
        assert r3[n]["commandAction"] == "OFF" and r3[n]["safetyReason"] == "EXTERNAL_BOOST_END", n
        assert "externalBoostUntil" not in eng.room_timers[n]
    assert eng.room_timers[LEADER]["cooldownUntil"] == eng.room_timers[FOLLOWER]["cooldownUntil"]
    assert eng.room_timers[LEADER]["cooldownUntil"]


def test_linked_boost_cancel_and_floor_safety(tmp_path: Path):
    # 월패드로 방2 를 켰다가 다시 끄면 두 방 모두 종료 (방3 을 되살리지 않음)
    eng, ha, _ = _house(tmp_path / "a", floor=20.0)
    _cycle(eng)
    _wallpad_set(ha, FOLLOWER, 24.0)
    r = _cycle(eng)
    assert r[LEADER]["safetyReason"] == "EXTERNAL_BOOST" and r[FOLLOWER]["safetyReason"] == "EXTERNAL_BOOST"
    _cycle(eng)
    _wallpad_set(ha, FOLLOWER, 15.0)
    r2 = _cycle(eng)
    for n in (LEADER, FOLLOWER):
        assert r2[n]["commandAction"] == "OFF" and r2[n]["safetyReason"] == "EXTERNAL_BOOST_CANCELLED", n
    assert all(sp < 20.0 for sp in _sent(ha, FOLLOWER))

    # 9분 가동 중 바닥 상승률 예측 피크(22.0 + 0.30 x 10 = 25.0 >= 상한 - 0.2) -> 15분 전이라도 두 방 동시 차단
    eng2, ha2, _ = _house(tmp_path / "b", floor=22.0)
    _cycle(eng2)
    _wallpad_set(ha2, LEADER, 24.0)
    _cycle(eng2)
    for n in (LEADER, FOLLOWER):
        _age_boost(eng2, n, ran_min=9.0, remain_min=6.0)
    eng2.room_timers[LEADER]["heatingStartFloor"] = 22.0
    r3 = _cycle(eng2)
    for n in (LEADER, FOLLOWER):
        assert r3[n]["commandAction"] == "OFF" and r3[n]["safetyReason"] == "P000_FLOOR_PREDICTED_LIMIT", n
        assert "externalBoostUntil" not in eng2.room_timers[n]
    tm = eng2.room_timers[LEADER]
    assert datetime.fromisoformat(tm["cooldownUntil"]) > datetime.now(tz=KST)
    assert eng2.room_timers[FOLLOWER]["cooldownUntil"] == tm["cooldownUntil"]


def test_boost_disabled_or_manual_room_keeps_old_behavior(tmp_path: Path):
    # externalBoostMin = 0 -> v2.9.0 이전처럼 즉시 ITSS 판정(OFF)으로 되돌림
    eng, ha, _ = _house(tmp_path / "a")
    eng.settings["externalBoostMin"] = 0
    _cycle(eng)
    _wallpad_set(ha, INDEP, 24.0)
    r = _cycle(eng)
    assert r[INDEP]["commandAction"] == "OFF" and r[INDEP]["dispatchResult"] == "SENT"
    assert r[INDEP]["safetyReason"] != "EXTERNAL_BOOST"

    # ITSS 수동 모드 방은 외부 조작 감지 대상 아님
    eng2, ha2, _ = _house(tmp_path / "b", indep_mode="MANUAL")
    _cycle(eng2)
    _wallpad_set(ha2, INDEP, 24.0)
    r2 = _cycle(eng2)
    assert r2[INDEP]["safetyReason"] != "EXTERNAL_BOOST" and "externalBoostUntil" not in eng2.room_timers[INDEP]


def test_settings_api_and_status_expose_boost(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    c = TestClient(app)
    assert app.state.engine.settings["externalBoostMin"] == 15
    assert c.patch("/api/v1/settings", json={"externalBoostMin": 30}).json()["externalBoostMin"] == 30
    assert c.patch("/api/v1/settings", json={"externalBoostMin": 0}).status_code == 200
    assert c.patch("/api/v1/settings", json={"externalBoostMin": 61}).status_code == 422

    eng = app.state.engine
    eng.settings["masterFloorEntity"] = "sensor.floor"
    eng.db.upsert_room({"roomName": INDEP, "roomTempEntity": "sensor.g_temp", "climateEntity": ENT[INDEP],
                        "hasFloorSensor": False, "floorEstimateEnabled": False, "targetTemp": 21.0})
    until = _iso(datetime.now(tz=KST) + timedelta(minutes=10))
    eng.room_timers[INDEP] = {"externalBoostUntil": until}
    live = c.get("/api/v1/status").json()["liveByRoom"]
    assert live[INDEP]["externalBoostUntil"] == until
