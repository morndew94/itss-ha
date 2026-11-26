"""v2.9.1 회귀 테스트: ITSS 수동 HEAT 1회 최대 가동/바닥 예측/외부 OFF -> 자동 복귀, 바닥 최저 온도(floorMinC).

재현 근거 (2026-11-25 트레이스): 수동 HEAT 로 거실 약 154분 연속 가동(바닥 기준 없음), 방3 36.9분 가동 후
바닥 27.1°C, 방2 추정 바닥(offset -0.4)이 실측보다 낮아 22:27 단독 재가열, 22:28 외부 OFF 를 24°C 로 되살림.
그리고 실내는 목표 근처인데 바닥 19.7°C 로 식어도 자동 난방이 없었던 문제.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import itss_scada.app.main as main_mod
from itss_scada.app.const import KST
from itss_scada.app.main import create_app
from test_v27_hardening import FakeHA, FakeNotifier, NoWeather
from test_v291_external_boost import ENT, FOLLOWER, INDEP, LEADER, _cycle, _house, _sent, _wallpad_set


@pytest.fixture(autouse=True)
def _fast_dispatch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_mod, "DEFAULT_RECHECK_DELAY_SEC", 0.0)


def _ago(minutes: float) -> str:
    return (datetime.now(tz=KST) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _manual_heat(eng, room: str) -> None:
    eng.db.update_room_control(room, control_mode="MANUAL", manual_action="HEAT")


# ---------------------------------------------------------------- 수동 HEAT
def test_manual_heat_times_out_and_returns_to_auto(tmp_path: Path):
    eng, ha, notifier = _house(tmp_path)
    _manual_heat(eng, INDEP)
    r = _cycle(eng)
    assert r[INDEP]["commandAction"] == "HEAT" and r[INDEP]["safetyReason"] == "P000_MANUAL_OVERRIDE"
    assert eng.room_timers[INDEP]["manualHeatSince"]
    msg = next(m for t, m in notifier.sent if INDEP in t and "켜짐" in t)
    assert "수동 난방:" in msg and "15분 후 자동 제어 복귀" in msg

    # 14분: 유지
    eng.room_timers[INDEP]["manualHeatSince"] = _ago(14.0)
    assert _cycle(eng)[INDEP]["safetyReason"] == "P000_MANUAL_OVERRIDE"
    # 15분: OFF 전송 + 자동 복귀 + 잔열 휴지
    eng.room_timers[INDEP]["manualHeatSince"] = _ago(15.0)
    r2 = _cycle(eng)
    assert r2[INDEP]["commandAction"] == "OFF" and r2[INDEP]["safetyReason"] == "MANUAL_HEAT_TIMEOUT"
    assert r2[INDEP]["dispatchResult"] == "SENT" and _sent(ha, INDEP)[-1] <= 15.0
    room = eng.db.get_room(INDEP)
    assert room["controlMode"] == "AUTO" and room["manualAction"] == "HOLD"
    tm = eng.room_timers[INDEP]
    assert "manualHeatSince" not in tm and datetime.fromisoformat(tm["cooldownUntil"]) > datetime.now(tz=KST)
    assert any(INDEP in t and "꺼짐" in t and "자동 제어(AUTO) 복귀" in m for t, m in notifier.sent)

    # 다음 회차는 자동 제어 (외부 조작으로 오인하지 않음)
    r3 = _cycle(eng)
    assert r3[INDEP]["commandAction"] == "OFF" and r3[INDEP]["safetyReason"] != "EXTERNAL_BOOST"


def test_manual_heat_external_off_is_respected(tmp_path: Path):
    eng, ha, _ = _house(tmp_path)
    _manual_heat(eng, INDEP)
    _cycle(eng)
    n_heat = len([sp for sp in _sent(ha, INDEP) if sp >= 20.0])
    _wallpad_set(ha, INDEP, 15.0)  # 월패드에서 끔 -> 24°C 로 되살리지 않음
    r = _cycle(eng)
    assert r[INDEP]["commandAction"] == "OFF" and r[INDEP]["safetyReason"] == "MANUAL_HEAT_CANCELLED"
    assert len([sp for sp in _sent(ha, INDEP) if sp >= 20.0]) == n_heat
    assert eng.db.get_room(INDEP)["controlMode"] == "AUTO"


def test_manual_heat_floor_prediction_and_shared_floor(tmp_path: Path):
    # 방3 수동 9분 가동: 22.0 + 0.30 x (9 + 1) = 25.0 >= 상한 24.5 - 0.2 -> 차단 + 자동 복귀 + 잔열 휴지
    eng, ha, _ = _house(tmp_path / "a", floor=22.0)
    _manual_heat(eng, LEADER)
    ha.climate[ENT[LEADER]]["attributes"]["temperature"] = 24.0
    eng.room_timers[LEADER] = {"heatingStartedAt": _ago(9.0), "heatingStartFloor": 22.0, "manualHeatSince": _ago(9.0),
                               "lastCommandAction": "HEAT", "lastSwitchAt": _ago(9.0), "lastObservedHeating": True}
    r = _cycle(eng)
    assert r[LEADER]["commandAction"] == "OFF" and r[LEADER]["safetyReason"] == "P000_FLOOR_PREDICTED_LIMIT"
    assert eng.db.get_room(LEADER)["controlMode"] == "AUTO"
    assert datetime.fromisoformat(eng.room_timers[LEADER]["cooldownUntil"]) > datetime.now(tz=KST)

    # 방2(offset -0.4) 수동 ON: 실측 23.0 -> 추정 22.6 이지만 같은 바닥이므로 23.0 으로 판정
    # 시작 전 예측 23.0 + 0.30 x 5 = 24.5 >= 24.3 -> 차단 (추정값 22.6 기준이면 24.1 로 통과했을 상황)
    eng2, ha2, _ = _house(tmp_path / "b", floor=23.0)
    _manual_heat(eng2, FOLLOWER)
    r2 = _cycle(eng2)
    assert r2[FOLLOWER]["commandAction"] == "OFF" and r2[FOLLOWER]["safetyReason"] == "P000_FLOOR_PREDICTED_LIMIT"
    assert all(sp < 20.0 for sp in _sent(ha2, FOLLOWER))
    assert eng2.db.get_room(FOLLOWER)["controlMode"] == "AUTO"


# ---------------------------------------------------------------- 바닥 최저 온도
def test_floor_min_heats_group_when_floor_cold_and_air_near_target(tmp_path: Path):
    # 2026-11-25 19:50 재현: 실내 ≈ 목표, 바닥 19.7°C -> 기존에는 GROUP_IDLE
    eng, ha, _ = _house(tmp_path / "a", floor=19.7, room=21.1)
    r0 = _cycle(eng)
    assert r0[LEADER]["safetyReason"] == "GROUP_IDLE"  # 기본값 0 = 사용 안 함

    eng2, ha2, _ = _house(tmp_path / "b", floor=19.7, room=21.1)
    eng2.settings["floorMinC"] = 21.0
    r = _cycle(eng2)
    for n in (LEADER, FOLLOWER):
        assert r[n]["commandAction"] == "HEAT" and r[n]["safetyReason"] == "GROUP_FLOOR_MIN_HEAT", n
    assert r[INDEP]["commandAction"] == "OFF"  # 독립 방은 바닥 기준 없음 -> 무관

    # 실내가 이미 목표 + 0.3°C 이상이면 바닥 보충 안 함
    eng3, _, _ = _house(tmp_path / "c", floor=19.7, room=22.0)
    eng3.settings["floorMinC"] = 21.0
    assert _cycle(eng3)[LEADER]["safetyReason"] == "GROUP_IDLE"


def _set_group_running(eng, ha, run_min: float, start_floor: float) -> None:
    for n in (LEADER, FOLLOWER):
        ha.climate[ENT[n]]["attributes"]["temperature"] = 24.0
        eng.room_timers[n] = {"heatingStartedAt": _ago(run_min), "heatingStartFloor": start_floor,
                              "lastCommandAction": "HEAT", "lastSwitchAt": _ago(run_min), "lastObservedHeating": True}


def test_floor_min_runs_until_predicted_peak(tmp_path: Path):
    # 시작 19.7°C, 3분: 19.7 + 0.30 x 3 = 20.6 < 21.0 + 1.0 -> 계속
    eng, ha, _ = _house(tmp_path / "a", floor=19.9, room=21.1)
    eng.settings["floorMinC"] = 21.0
    _set_group_running(eng, ha, run_min=3.0, start_floor=19.7)
    r = _cycle(eng)
    assert r[LEADER]["commandAction"] == "HEAT" and r[LEADER]["safetyReason"] == "GROUP_FLOOR_MIN_CONTINUE"
    assert r[FOLLOWER]["commandAction"] == "HEAT"

    # 8분: 19.7 + 2.4 = 22.1 >= 22.0 -> 두 방 동시 정지 + 잔열 휴지
    eng2, ha2, _ = _house(tmp_path / "b", floor=20.3, room=21.1)
    eng2.settings["floorMinC"] = 21.0
    _set_group_running(eng2, ha2, run_min=8.0, start_floor=19.7)
    r2 = _cycle(eng2)
    for n in (LEADER, FOLLOWER):
        assert r2[n]["commandAction"] == "OFF" and r2[n]["safetyReason"] == "GROUP_FLOOR_MIN_REACHED", n
    assert eng2.room_timers[LEADER]["cooldownUntil"]


def test_settings_validation_and_status(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    c = TestClient(app)
    eng = app.state.engine
    assert eng.settings["manualHeatMaxMin"] == 15 and eng.settings["floorMinC"] == 0.0
    assert c.patch("/api/v1/settings", json={"manualHeatMaxMin": 30}).json()["manualHeatMaxMin"] == 30
    assert c.patch("/api/v1/settings", json={"manualHeatMaxMin": 4}).status_code == 422
    assert c.patch("/api/v1/settings", json={"manualHeatMaxMin": 61}).status_code == 422
    assert c.patch("/api/v1/settings", json={"floorMinC": 21.0}).json()["floorMinC"] == 21.0
    assert c.patch("/api/v1/settings", json={"floorMinC": 23.5}).status_code == 422

    eng.settings["masterFloorEntity"] = "sensor.floor"
    eng.db.upsert_room({"roomName": INDEP, "roomTempEntity": "sensor.g_temp", "climateEntity": ENT[INDEP],
                        "hasFloorSensor": False, "floorEstimateEnabled": False, "targetTemp": 21.0,
                        "controlMode": "MANUAL", "manualAction": "HEAT"})
    eng.room_timers[INDEP] = {"manualHeatSince": _ago(5.0)}
    live = c.get("/api/v1/status").json()["liveByRoom"]
    until = datetime.fromisoformat(live[INDEP]["manualHeatUntil"])
    assert timedelta(minutes=24) < until - datetime.now(tz=KST) <= timedelta(minutes=25)  # 30분 설정 - 5분 경과

    html = (Path(__file__).resolve().parents[1] / "itss_scada" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="manualHeatMax"' in html and 'id="floorMinC"' in html
