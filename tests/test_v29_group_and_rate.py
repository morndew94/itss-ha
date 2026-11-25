"""v2.9.0 회귀 테스트: 시작 전 가드(A안), 바닥 상승률 학습(C안), 바닥 기준 동시 제어 그룹/독립 방(B안)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import itss_scada.app.main as main_mod
from itss_scada.app.const import DEFAULT_FLOOR_RISE_RATE_C_PER_MIN, KST
from itss_scada.app.main import ItssEngine, create_app
from itss_scada.app.models.schema import SensorSnapshot
from itss_scada.app.protocols.multi_zone import GroupMember, evaluate_floor_group
from itss_scada.app.protocols.p000_sentinel import SentinelConfig, SentinelProtocol
from itss_scada.app.protocols.thermal_lag_learner import learn_room_lag_profile
from test_v27_hardening import FakeHA, FakeNotifier, NoWeather

LEADER, FOLLOWER, INDEP = "방3", "방2", "거실"


@pytest.fixture(autouse=True)
def _fast_dispatch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_mod, "DEFAULT_RECHECK_DELAY_SEC", 0.0)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------- A안: 시작 전 가드
def test_prestart_guard_matches_running_guard_legacy_surge():
    # 2026-11-25 05:56 재현: 바닥 23.5, 상한 25.0, surge 2.3 -> v2.8 은 시작 허용 후 1분 만에 차단
    s = SentinelProtocol(SentinelConfig(general_off_floor_c=25.0))
    snap = SensorSnapshot("방3", room_temp=21.1, floor_temp=23.5)
    kw = {"effective_floor_limit_c": 25.0, "expected_floor_surge_c": 2.3}
    assert s.evaluate(snap, is_heating=False, **kw).reason_code == "P000_FLOOR_PREDICTED_LIMIT"
    assert s.evaluate(snap, is_heating=True, **kw).reason_code == "P000_FLOOR_PREDICTED_LIMIT"
    # 여유가 충분하면 시작 허용
    assert s.evaluate(SensorSnapshot("방3", 21.1, 22.0), is_heating=False, **kw).reason_code == "P000_OK"


def test_rate_model_prestart_and_running_cut_are_consistent():
    s = SentinelProtocol(SentinelConfig(general_off_floor_c=25.0))
    kw = {"effective_floor_limit_c": 25.0, "floor_rise_rate_c_per_min": 0.30, "min_pulse_min": 5.0}
    # 시작: 바닥 + 0.30 x 5분 >= 25.0 - 0.2 이면 차단
    assert s.evaluate(SensorSnapshot("r", 21.0, 23.5), is_heating=False, **kw).reason_code == "P000_FLOOR_PREDICTED_LIMIT"
    assert s.evaluate(SensorSnapshot("r", 21.0, 23.2), is_heating=False, **kw).reason_code == "P000_OK"
    # 가동 중: 시작 바닥 23.0 + 0.30 x 가동시간 >= 24.8 에서 차단 (5분 허용, 6분 차단)
    snap = SensorSnapshot("r", 21.0, 23.0)
    v5 = s.evaluate(snap, is_heating=True, continuous_run_min=5.0, heating_start_floor_c=23.0, **kw)
    v6 = s.evaluate(snap, is_heating=True, continuous_run_min=6.0, heating_start_floor_c=23.0, **kw)
    assert v5.reason_code == "P000_OK" and v6.reason_code == "P000_FLOOR_PREDICTED_LIMIT"
    # 시작 허용된 바닥(23.2)은 최소 펄스(5분) 동안 끊기지 않음 -> 1분 펄스 불가
    v_min = s.evaluate(SensorSnapshot("r", 21.0, 23.2), is_heating=True, continuous_run_min=4.9,
                       heating_start_floor_c=23.2, **kw)
    assert v_min.reason_code == "P000_OK"


def test_floor_optional_room_checks_only():
    s = SentinelProtocol()
    no_floor = SensorSnapshot("거실", room_temp=21.0, floor_temp=None)
    assert s.evaluate(no_floor, is_heating=False).reason_code == "P000_SENSOR_MISSING"
    assert s.evaluate(no_floor, is_heating=False, floor_optional=True).reason_code == "P000_OK"
    hot = SensorSnapshot("거실", room_temp=24.1, floor_temp=None)
    assert s.evaluate(hot, is_heating=True, floor_optional=True).reason_code == "P000_OVERHEAT_CUTOFF"
    # 시간 기반 보호(최대 가동)는 유지
    v = s.evaluate(no_floor, is_heating=True, floor_optional=True, continuous_run_min=13.0, max_run_min=13.0)
    assert v.reason_code == "P000_MAX_RUN_CUTOFF"


# ---------------------------------------------------------------- C안: 상승률 학습
def _episode(t0: datetime, run_min: int, start_floor: float, rise: float, room: str = "방3") -> list[dict]:
    """가동 run_min 분(데드타임으로 바닥 평탄) -> OFF 후 20분에 걸쳐 +rise 상승 후 완만 하강."""
    rows = []
    for m in range(run_min):
        rows.append({"roomName": room, "executionStartedAt": _iso(t0 + timedelta(minutes=m)),
                     "commandAction": "HEAT", "combustionConfirmed": True,
                     "floorTemp": start_floor, "roomTemp": 21.0})
    for m in range(40):
        f = start_floor + (rise * m / 20.0 if m <= 20 else rise - 0.02 * (m - 20))
        rows.append({"roomName": room, "executionStartedAt": _iso(t0 + timedelta(minutes=run_min + m)),
                     "commandAction": "OFF", "combustionConfirmed": False,
                     "floorTemp": round(f, 2), "roomTemp": 21.0})
    return rows


def test_floor_rise_rate_excludes_short_pulses_and_is_conservative():
    base = datetime(2026, 11, 25, 5, 0, tzinfo=KST)
    # 트레이스와 같은 구성: 1분 펄스 2회(+0.5) + 8분 펄스(+2.1)
    cycles = (_episode(base, 1, 23.4, 0.5) + _episode(base + timedelta(minutes=60), 1, 23.3, 0.5)
              + _episode(base + timedelta(minutes=120), 8, 23.6, 2.1))
    prof = learn_room_lag_profile("방3", cycles).to_dict()
    assert prof["floorRateSamples"] == 1  # 1분 펄스 제외
    assert prof["floorRiseRateCPerMin"] == DEFAULT_FLOOR_RISE_RATE_C_PER_MIN  # 표본 부족 -> max(기본, 0.26)
    # v2.8 방식(펄스 길이 무시 중앙값)은 1분 펄스에 끌려 내려감 -> 이것이 07:19 오버슈트 원인
    assert prof["floorResidualRiseC"] <= 0.6

    # 표본 3개 이상이면 75 백분위 (중앙값보다 보수적)
    cycles3 = (_episode(base, 10, 21.0, 2.0) + _episode(base + timedelta(minutes=60), 10, 21.0, 2.2)
               + _episode(base + timedelta(minutes=120), 10, 21.0, 4.0))
    prof3 = learn_room_lag_profile("방3", cycles3).to_dict()
    assert prof3["floorRateSamples"] == 3
    assert prof3["floorRiseRateCPerMin"] == pytest.approx(0.40, abs=0.01)


# ---------------------------------------------------------------- B안: 그룹 판정
def test_evaluate_floor_group_weighted_deviation():
    m = [GroupMember("a", 21.0, 21.5), GroupMember("b", 21.3, 21.5)]
    assert evaluate_floor_group(m, is_heating=False).action == "HEAT"  # 평균 -0.35 <= -0.15
    m2 = [GroupMember("a", 21.0, 21.5), GroupMember("b", 22.0, 21.5)]
    d = evaluate_floor_group(m2, is_heating=False)
    assert d.action == "OFF" and d.reason == "GROUP_IDLE" and d.weighted_dev_c == 0.0
    assert evaluate_floor_group(m2, is_heating=True).reason == "GROUP_TARGET_REACHED"
    # 가중치: 찬 방 가중이 크면 기동
    m3 = [GroupMember("a", 21.0, 21.5, weight=3.0), GroupMember("b", 22.0, 21.5)]
    assert evaluate_floor_group(m3, is_heating=False).action == "HEAT"
    assert evaluate_floor_group([GroupMember("a", None, 21.5)], is_heating=False).reason == "GROUP_NO_ROOM_DATA"


class EntityFailHA(FakeHA):
    """특정 climate 엔티티 명령만 실패시키는 가짜 HA."""

    def __init__(self, fail_entity: str) -> None:
        super().__init__()
        self.fail_entity = fail_entity

    async def call_service(self, domain: str, service: str, data: dict):
        if domain == "climate" and data.get("entity_id") == self.fail_entity:
            self.calls.append((domain, service, data))
            raise RuntimeError("wallpad timeout")
        await super().call_service(domain, service, data)


def _house(tmp_path: Path, *, floor: float = 20.0, rooms: tuple[float | None, float | None, float | None] = (18.0, 18.0, 18.0),
           ha: FakeHA | None = None):
    ha = ha or FakeHA()
    notifier = FakeNotifier()
    eng = ItssEngine(db_path=str(tmp_path / "itss.db"), ha_client=ha, notifier=notifier,
                     weather_client=NoWeather(), options={})
    eng.settings.update({"masterFloorEntity": "sensor.floor", "dryRun": False})
    eng.db.upsert_room({"roomName": LEADER, "roomTempEntity": "sensor.y_temp", "climateEntity": "climate.y",
                        "hasFloorSensor": True, "targetTemp": 21.0})
    eng.db.upsert_room({"roomName": FOLLOWER, "roomTempEntity": "sensor.s_temp", "climateEntity": "climate.s",
                        "hasFloorSensor": False, "floorEstimateEnabled": True, "floorOffsetC": -0.4,
                        "targetTemp": 21.0})
    eng.db.upsert_room({"roomName": INDEP, "roomTempEntity": "sensor.g_temp", "climateEntity": "climate.g",
                        "hasFloorSensor": False, "floorEstimateEnabled": False, "targetTemp": 21.0})
    ha.set_sensor("sensor.floor", floor)
    for ent, val in zip(("sensor.y_temp", "sensor.s_temp", "sensor.g_temp"), rooms):
        ha.set_sensor(ent, val)
    return eng, ha, notifier


def _cycle(eng: ItssEngine) -> dict[str, dict]:
    return {r["roomName"]: r for r in asyncio.run(eng.run_control_cycle())}


def _heat_calls(ha: FakeHA, entity: str) -> list[dict]:
    return [d for _, s, d in ha.calls if s == "set_temperature" and d.get("entity_id") == entity
            and d.get("temperature", 0) >= 20.0]


def test_group_starts_together_and_independent_room_separate(tmp_path: Path):
    eng, ha, notifier = _house(tmp_path)
    r = _cycle(eng)
    assert r[LEADER]["commandAction"] == "HEAT" and r[LEADER]["safetyReason"] == "GROUP_DEMAND_HEAT"
    assert r[FOLLOWER]["commandAction"] == "HEAT" and r[FOLLOWER]["safetyReason"] == "GROUP_DEMAND_HEAT"
    assert r[FOLLOWER]["diagnostics"]["groupRole"] == "follower"
    assert r[FOLLOWER]["diagnostics"]["groupLeader"] == LEADER
    assert r[INDEP]["commandAction"] == "HEAT" and r[INDEP]["safetyReason"] == "HYBRID_PRIMARY_HEAT"
    assert r[INDEP]["diagnostics"]["groupRole"] is None
    # 결과는 등록 순서 유지
    assert list(r) == [rm["roomName"] for rm in eng.db.list_rooms()]

    msgs = {t: m for t, m in notifier.sent}
    follower_msg = next(m for t, m in msgs.items() if FOLLOWER in t)
    indep_msg = next(m for t, m in msgs.items() if INDEP in t)
    assert f"{LEADER} 기준 동시 제어" in follower_msg and "동시 제어:" in follower_msg
    assert "미사용(독립 제어)" in indep_msg
    assert all("None°C" not in m for _, m in notifier.sent)


def test_group_prestart_guard_blocks_both_rooms_not_independent(tmp_path: Path):
    # 바닥 23.5: 최소 펄스 예측 피크 25.0 >= 상한(목표 연동 약 24.5) - 0.2 -> 그룹 시작 차단 (1분 펄스 방지)
    eng, ha, _ = _house(tmp_path, floor=23.5)
    r = _cycle(eng)
    assert r[LEADER]["safetyReason"] == "P000_FLOOR_PREDICTED_LIMIT" and r[LEADER]["commandAction"] == "OFF"
    assert r[FOLLOWER]["safetyReason"] == "P000_FLOOR_PREDICTED_LIMIT" and r[FOLLOWER]["commandAction"] == "OFF"
    assert r[INDEP]["commandAction"] == "HEAT"
    assert _heat_calls(ha, "climate.y") == [] and _heat_calls(ha, "climate.s") == []


def _set_running(eng: ItssEngine, ha: FakeHA, run_min: float, start_floor: float) -> None:
    started = _iso(datetime.now(tz=KST) - timedelta(minutes=run_min))
    for name, ent in ((LEADER, "climate.y"), (FOLLOWER, "climate.s")):
        ha.climate[ent] = {"state": "heat", "attributes": {"temperature": 24.0, "current_temperature": 21.0}}
        eng.room_timers[name] = {"heatingStartedAt": started, "heatingStartFloor": start_floor,
                                 "lastCommandAction": "HEAT", "lastSwitchAt": started}


def test_group_running_cut_by_rate_model_syncs_cooldown(tmp_path: Path):
    eng, ha, _ = _house(tmp_path / "a", floor=22.0)
    _set_running(eng, ha, run_min=2.0, start_floor=22.0)
    r = _cycle(eng)
    assert r[LEADER]["safetyReason"] == "GROUP_CONTINUE" and r[FOLLOWER]["commandAction"] == "HEAT"

    # 9분 가동: 22.0 + 0.30 x 9 = 24.7 >= 상한 - 0.2 -> 두 방 같은 사유로 동시 차단, 휴지 시각 동일
    eng2, ha2, _ = _house(tmp_path / "b", floor=22.0)
    _set_running(eng2, ha2, run_min=9.0, start_floor=22.0)
    r2 = _cycle(eng2)
    assert r2[LEADER]["safetyReason"] == "P000_FLOOR_PREDICTED_LIMIT"
    assert r2[FOLLOWER]["safetyReason"] == "P000_FLOOR_PREDICTED_LIMIT" and r2[FOLLOWER]["commandAction"] == "OFF"
    cd_leader = eng2.room_timers[LEADER]["cooldownUntil"]
    assert cd_leader and eng2.room_timers[FOLLOWER]["cooldownUntil"] == cd_leader


def test_follower_warm_skip_but_never_heats_alone(tmp_path: Path):
    # 추종 방만 목표 상한 이상 -> 그룹은 가열해도 이 방만 개별 정지
    eng, ha, _ = _house(tmp_path / "a", rooms=(18.0, 22.0, 18.0))
    r = _cycle(eng)
    assert r[LEADER]["commandAction"] == "HEAT"
    assert r[FOLLOWER]["commandAction"] == "OFF" and r[FOLLOWER]["safetyReason"] == "GROUP_MEMBER_WARM_SKIP"

    # 기준 방 실내 센서 누락 -> 기준 방 OFF, 추종 방이 추워도 단독 가열 금지
    eng2, ha2, _ = _house(tmp_path / "b", rooms=(None, 18.0, 18.0))
    r2 = _cycle(eng2)
    assert r2[LEADER]["safetyReason"] == "P000_SENSOR_MISSING"
    assert r2[FOLLOWER]["commandAction"] == "OFF" and _heat_calls(ha2, "climate.s") == []

    # 기준 방 명령 실패 -> 추종 방 단독 가열 금지
    eng3, ha3, _ = _house(tmp_path / "c", ha=EntityFailHA("climate.y"))
    r3 = _cycle(eng3)
    assert r3[LEADER]["dispatchResult"] == "FAILED"
    assert r3[FOLLOWER]["commandAction"] == "OFF" and r3[FOLLOWER]["safetyReason"] == "GROUP_LEADER_UNAVAILABLE"


def test_status_exposes_group_roles_and_rate(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    eng = app.state.engine
    eng.settings["masterFloorEntity"] = "sensor.floor"
    for name, has_floor, est in ((LEADER, True, True), (FOLLOWER, False, True), (INDEP, False, False)):
        eng.db.upsert_room({"roomName": name, "roomTempEntity": f"sensor.t{len(name)}{int(has_floor)}{int(est)}",
                            "climateEntity": f"climate.c{int(has_floor)}{int(est)}", "hasFloorSensor": has_floor,
                            "floorEstimateEnabled": est, "targetTemp": 21.0})
    ha.set_sensor("sensor.floor", 22.4)
    live = TestClient(app).get("/api/v1/status").json()["liveByRoom"]
    assert live[LEADER]["groupRole"] == "LEADER" and live[FOLLOWER]["groupRole"] == "FOLLOWER"
    assert live[FOLLOWER]["groupLeader"] == LEADER and live[INDEP]["groupRole"] == "INDEPENDENT"
    assert live[LEADER]["floorRiseRateCPerMin"] == DEFAULT_FLOOR_RISE_RATE_C_PER_MIN
    assert live[INDEP]["floorRiseRateCPerMin"] is None and live[INDEP]["guardFloorTemp"] is None
