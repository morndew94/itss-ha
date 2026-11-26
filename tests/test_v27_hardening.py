"""v2.7.0 리뷰 반영 회귀 테스트 (보안, 안전 가드, 센서 보고 주기, 명령 전송, 통계, 운영, 로그/트레이스)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import itss_scada.app.main as main_mod
from itss_scada.app.const import DEFAULT_OVERHEAT_FLOOR_C, KST, VERSION
from itss_scada.app.ha_client import HomeAssistantClient
from itss_scada.app.integrations.google_weather import (
    FallbackWeatherProvider,
    GoogleWeatherClient,
    OpenMeteoWeatherClient,
    WeatherSnapshot,
)
from itss_scada.app.integrations.notifications import PushNotifier
from itss_scada.app.main import ItssEngine, compute_boiler_stats, create_app
from itss_scada.app.models.schema import CycleRecord, SensorSnapshot
from itss_scada.app.protocols import sensor_tracker as st
from itss_scada.app.protocols.p000_sentinel import SentinelConfig, SentinelProtocol
from itss_scada.app.protocols.rc_optimizer import RCObservation, fit_rc_parameters

REPO = Path(__file__).resolve().parents[1]


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class FakeHA:
    """last_reported 를 제공하는 HA 가짜 클라이언트 (서비스 호출 시 climate 상태 갱신)."""

    def __init__(self) -> None:
        self.is_configured = True
        self.token_source = "mock"
        self.last_error = None
        self.numeric: dict[str, tuple[float | None, str | None]] = {}
        self.climate: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_services: set[tuple[str, str]] = set()

    def set_sensor(self, entity_id: str, value: float | None, reported: datetime | None = None) -> None:
        self.numeric[entity_id] = (value, _iso(reported or datetime.now(tz=KST)))

    async def get_numeric_reading(self, entity_id: str):
        if entity_id not in self.numeric:
            return None, None, None, None
        val, ts = self.numeric[entity_id]
        if val is None:
            return None, None, None, None
        age = (datetime.now(tz=KST) - datetime.fromisoformat(ts)).total_seconds()
        return val, ts, round(age, 1), "last_reported"

    async def get_numeric_state(self, entity_id: str):
        val, ts, age, _ = await self.get_numeric_reading(entity_id)
        return val, ts, age

    async def get_state(self, entity_id: str):
        return self.climate.get(entity_id, {"state": "off", "attributes": {}})

    async def call_service(self, domain: str, service: str, data: dict):
        self.calls.append((domain, service, data))
        if (domain, service) in self.fail_services:
            raise RuntimeError(f"{domain}.{service} boom")
        if domain == "climate":
            state = self.climate.setdefault(data["entity_id"], {"state": "off", "attributes": {}})
            if service == "set_temperature":
                state["attributes"]["temperature"] = data["temperature"]
            if service == "set_hvac_mode":
                state["state"] = data["hvac_mode"]

    async def list_entities(self):
        return {"tempSensors": [], "sensors": [], "climates": [], "windowSensors": [], "notifies": []}


class FakeNotifier:
    notify_service = ""
    telegram_chat_id = ""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, title: str, message: str):
        self.sent.append((title, message))
        return {"status": "ok"}


class NoWeather:
    def get_weather(self, now_epoch=None):
        return WeatherSnapshot(None, None, "test", "FALLBACK", 0.0)

    def set_location(self, _loc):
        pass


@pytest.fixture(autouse=True)
def _fast_dispatch(monkeypatch: pytest.MonkeyPatch):
    # 재조회 대기 2초 제거 (테스트 속도)
    monkeypatch.setattr(main_mod, "DEFAULT_RECHECK_DELAY_SEC", 0.0)


def _engine(tmp_path: Path, ha: FakeHA, notifier: FakeNotifier | None = None, options: dict | None = None) -> ItssEngine:
    return ItssEngine(
        db_path=str(tmp_path / "itss.db"),
        ha_client=ha,
        notifier=notifier or FakeNotifier(),
        weather_client=NoWeather(),
        options=options or {},
    )


def _add_room(eng: ItssEngine, name: str = "거실", *, target: float = 21.0, mode: str = "AUTO", manual: str = "HOLD"):
    eng.db.upsert_room({
        "roomName": name,
        "roomTempEntity": f"sensor.{'r' + str(abs(hash(name)) % 1000)}_temp",
        "climateEntity": f"climate.{'c' + str(abs(hash(name)) % 1000)}",
        "hasFloorSensor": True,
        "targetTemp": target,
        "controlMode": mode,
        "manualAction": manual,
    })
    return eng.db.get_room(name)


def _run(eng: ItssEngine):
    return asyncio.run(eng.run_control_cycle())


# ---------------------------------------------------------------- P0 보안
def test_ingress_ip_filter_blocks_lan_clients(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=FakeHA(), notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    assert TestClient(app, client=("192.168.0.50", 5000)).get("/api/v1/rooms").status_code == 403
    assert TestClient(app, client=("172.30.32.2", 5000)).get("/api/v1/rooms").status_code == 200
    assert TestClient(app, client=("::ffff:172.30.32.2", 5000)).get("/health").status_code == 200
    cfg = yaml.safe_load((REPO / "itss_scada" / "config.yaml").read_text(encoding="utf-8"))
    assert "ports" not in cfg and cfg["ingress"] is True


def test_input_validation_rejects_markup(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=FakeHA(), notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    c = TestClient(app)
    base = {"roomTempEntity": "sensor.room_temp", "climateEntity": "climate.room"}
    assert c.post("/api/v1/rooms", json={**base, "roomName": "<img src=x onerror=alert(1)>"}).status_code == 422
    assert c.post("/api/v1/rooms", json={**base, "roomName": "Kid's room"}).status_code == 422
    assert c.post("/api/v1/rooms", json={**base, "roomName": "방3", "roomTempEntity": "sensor.x<script>"}).status_code == 422
    assert c.post("/api/v1/rooms", json={**base, "roomName": "방3"}).status_code == 200
    bad_slot = {"slotId": "a", "label": "<b>x</b>", "startHour": 1, "endHour": 2, "offsetC": 0.0}
    assert c.put("/api/v1/schedules", json={"slots": [bad_slot]}).status_code == 422
    html = (REPO / "itss_scada" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "function esc(" in html and "onclick=\"changeTarget('${" not in html


# ---------------------------------------------------------------- P0 동시성
def test_control_cycles_are_serialized(tmp_path: Path):
    eng = _engine(tmp_path, FakeHA())
    state = {"active": 0, "max": 0}

    async def fake_locked():
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return []

    eng._run_cycle_locked = fake_locked  # type: ignore[method-assign]

    async def burst():
        await asyncio.gather(*(eng.run_control_cycle() for _ in range(5)))

    asyncio.run(burst())
    assert state["max"] == 1


def test_cycle_ids_unique_within_same_second(tmp_path: Path):
    ha = FakeHA()
    eng = _engine(tmp_path, ha)
    eng.settings["masterFloorEntity"] = "sensor.floor"
    rm = _add_room(eng, mode="MANUAL", manual="OFF")
    ha.set_sensor("sensor.floor", 21.0)
    ha.set_sensor(rm["roomTempEntity"], 21.0)
    _run(eng)
    _run(eng)
    assert eng.db.count_cycles() == 2


# ---------------------------------------------------------------- P0 안전 가드
def test_manual_heat_blocked_without_sensor_and_floor_latch(tmp_path: Path):
    ha = FakeHA()
    eng = _engine(tmp_path, ha)
    eng.settings.update({"masterFloorEntity": "sensor.floor", "dryRun": False, "floorOffLimitC": 22.5})
    rm = _add_room(eng, mode="MANUAL", manual="HEAT")
    ha.set_sensor("sensor.floor", 21.0)
    ha.set_sensor(rm["roomTempEntity"], None)  # 실내 센서 누락

    rec = _run(eng)[0]
    assert rec["commandAction"] == "OFF" and rec["safetyReason"] == "P000_SENSOR_MISSING"
    assert not any(d.get("temperature", 0) >= 24.0 for _, s, d in ha.calls if s == "set_temperature")

    # 바닥 상한 도달 -> 수동 HEAT 종료 + 자동 제어 복귀 (v2.9.1: 수동은 1회성 요청, 래치 대기 후 재가열 없음)
    ha.set_sensor(rm["roomTempEntity"], 21.0)
    ha.set_sensor("sensor.floor", 22.6)
    rec = _run(eng)[0]
    assert rec["commandAction"] == "OFF" and rec["safetyReason"] == "P000_FLOOR_LIMIT_OFF"
    assert eng.db.get_room(rm["roomName"])["controlMode"] == "AUTO"
    assert DEFAULT_OVERHEAT_FLOOR_C > 22.6

    # 차가운 바닥에서 다시 수동 ON -> 가동 (시작 전 예측: 20.0 + 0.30 x 5 = 21.5 < 상한 22.5 - 0.2)
    eng.db.update_room_control(rm["roomName"], control_mode="MANUAL", manual_action="HEAT")
    ha.set_sensor("sensor.floor", 20.0)
    assert _run(eng)[0]["commandAction"] == "HEAT"
    # 시작 전 예측 피크가 상한 근처(21.0 + 1.5 = 22.5 >= 22.3)면 수동 ON 도 차단 후 자동 복귀
    eng.db.update_room_control(rm["roomName"], control_mode="MANUAL", manual_action="HEAT")
    eng.room_timers[rm["roomName"]].update({"heatingStartedAt": None, "manualHeatSince": None})
    ha.climate[rm["climateEntity"]] = {"state": "heat", "attributes": {"temperature": 15.0, "current_temperature": 21.0}}
    ha.set_sensor("sensor.floor", 21.0)
    rec = _run(eng)[0]
    assert rec["commandAction"] == "OFF" and rec["safetyReason"] == "P000_FLOOR_PREDICTED_LIMIT"
    assert eng.db.get_room(rm["roomName"])["controlMode"] == "AUTO"


def test_floor_unconfigured_blocks_auto_and_live(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    eng = app.state.engine
    rm = _add_room(eng, target=22.0)
    ha.set_sensor(rm["roomTempEntity"], 19.0)
    rec = _run(eng)[0]
    assert rec["safetyReason"] == "P000_FLOOR_UNCONFIGURED" and rec["commandAction"] == "OFF"
    assert rec["floorTemp"] is None
    c = TestClient(app)
    r = c.patch("/api/v1/settings", json={"dryRun": False})
    assert r.status_code == 409
    assert c.get("/health").json()["warnings"]


def test_settings_cross_validation(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=FakeHA(), notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    c = TestClient(app)
    assert c.patch("/api/v1/settings", json={"heatOffSetpoint": 22.0}).status_code == 422
    assert c.patch("/api/v1/settings", json={"maxRunCapMin": 3}).json()["maxRunCapMin"] == 5


# ---------------------------------------------------------------- P1 센서 보고 주기
def test_sensor_tracker_interval_detection_and_adaptive_stale():
    now = datetime(2026, 11, 24, 12, 0, tzinfo=KST)
    tr = st.new_track()
    for k in range(8):
        t = now - timedelta(seconds=300 * (7 - k))
        st.observe(tr, 25.0 + 0.1 * k, _iso(t), t, "last_reported")
    assert st.typical_interval_sec(tr) == 300.0
    # 관측 6시간 미만이면 임계 축소 금지 (이벤트형 센서 heartbeat 공백 미학습)
    assert st.stale_threshold_sec(tr) == 7200.0
    for k in range(8, 80):
        t = now + timedelta(seconds=300 * (k - 7))
        st.observe(tr, 25.0 + 0.01 * k, _iso(t), t, "last_reported")
    assert st.stale_threshold_sec(tr) == 600.0  # max(600, min(7200, 2 x 300))

    # 60초 주기 센서라도 24시간 내 50분 heartbeat 공백이 있으면 임계가 그만큼 넓어짐
    trh = st.new_track()
    t = now
    for k in range(400):
        t = t + timedelta(seconds=3000 if k == 200 else 60)
        st.observe(trh, 22.0 + (k % 3) * 0.1, _iso(t), t, "last_reported")
    assert st.typical_interval_sec(trh) == 60.0
    assert st.stale_threshold_sec(trh) == 6000.0

    # last_updated 만 있으면 값 변화 간격이라 적응형 임계 미사용
    tr2 = st.new_track()
    for k in range(8):
        t = now - timedelta(seconds=300 * (7 - k))
        st.observe(tr2, 25.0 + 0.1 * k, _iso(t), t, "last_updated")
    assert st.stale_threshold_sec(tr2) == 7200.0

    # 보고 지연 보상: 기울기 0.05°C/min, 마지막 보고 후 240초 -> +0.2°C
    tr3 = st.new_track()
    for k in range(4):
        t = now - timedelta(seconds=300 * (3 - k))
        st.observe(tr3, 24.0, _iso(t), t, "last_reported")
    later = now + timedelta(seconds=240)
    assert st.latency_rise_c(tr3, later, 0.05) == pytest.approx(0.2, abs=0.01)


def test_event_driven_step_is_not_ventilation_but_real_drop_is():
    now = datetime(2026, 11, 24, 12, 0, tzinfo=KST)
    sentinel = SentinelProtocol(SentinelConfig(general_off_floor_c=26.0))
    snap = SensorSnapshot("r", room_temp=21.0, floor_temp=21.5)

    # 이벤트형 센서: 20분 조용하다가 0.3°C 하락을 한 번에 보고 (실제 변화율 ~-0.016°C/min)
    tr = st.new_track()
    st.observe(tr, 21.3, _iso(now - timedelta(minutes=20)), now - timedelta(minutes=20), "last_reported")
    st.observe(tr, 21.0, _iso(now - timedelta(minutes=1)), now, "last_reported")
    rate = st.recent_rate_per_min(tr, now)
    drop5 = st.delta_over(tr, now, 5.0)
    assert rate > -0.05 and drop5 > -0.2
    v = sentinel.evaluate(snap, is_heating=False, v_room_per_min=rate, drop_5m_room_c=drop5, drop_5m_floor_c=0.0)
    assert v.reason_code != "P000_VENTILATION_PROTECT"

    # 실제 환기: 1분 간격으로 0.3°C씩 하락
    tr2 = st.new_track()
    for k in range(6):
        t = now - timedelta(minutes=5 - k)
        st.observe(tr2, 22.5 - 0.3 * k, _iso(t), t, "last_reported")
    v2 = sentinel.evaluate(
        snap, is_heating=False, v_room_per_min=st.recent_rate_per_min(tr2, now),
        drop_5m_room_c=st.delta_over(tr2, now, 5.0), drop_5m_floor_c=0.0,
    )
    assert v2.reason_code == "P000_VENTILATION_PROTECT"


def test_floor_latency_compensation_in_sentinel():
    sentinel = SentinelProtocol(SentinelConfig(general_off_floor_c=22.5))
    snap = SensorSnapshot("r", room_temp=20.0, floor_temp=22.3)
    assert sentinel.evaluate(snap, is_heating=True).reason_code == "P000_OK"
    assert sentinel.evaluate(snap, is_heating=True, floor_latency_c=0.3).reason_code == "P000_FLOOR_LIMIT_OFF"


def test_ha_client_prefers_last_reported():
    now = datetime(2026, 11, 24, 12, 0, tzinfo=KST)
    raw = {
        "state": "23.46",
        "last_updated": "2026-11-24T01:00:00+00:00",   # 10:00 KST (값 변화 시각)
        "last_reported": "2026-11-24T02:59:00+00:00",  # 11:59 KST (마지막 보고)
    }
    val, ts, age, src = HomeAssistantClient.parse_numeric_reading(raw, now)
    assert val == 23.5 and src == "last_reported" and age == 60.0 and ts.startswith("2026-11-24T11:59")
    _, _, age2, src2 = HomeAssistantClient.parse_numeric_reading({"state": "1", "last_updated": raw["last_updated"]}, now)
    assert src2 == "last_updated" and age2 == 7200.0


# ---------------------------------------------------------------- P1 명령 전송
def test_dispatch_skips_when_in_target_and_resyncs_after_30min(tmp_path: Path):
    ha = FakeHA()
    eng = _engine(tmp_path, ha)
    eng.settings.update({"masterFloorEntity": "sensor.floor", "dryRun": False})
    rm = _add_room(eng, mode="MANUAL", manual="OFF")
    ha.set_sensor("sensor.floor", 21.0)
    ha.set_sensor(rm["roomTempEntity"], 21.0)
    ha.climate[rm["climateEntity"]] = {"state": "heat", "attributes": {"temperature": 15.0, "current_temperature": 21.0}}

    assert _run(eng)[0]["dispatchResult"] == "NO_CHANGE"
    assert _run(eng)[0]["dispatchResult"] == "NO_CHANGE"
    assert ha.calls == []
    eng.room_timers[rm["roomName"]]["lastDispatchAt"] = _iso(datetime.now(tz=KST) - timedelta(minutes=31))
    assert _run(eng)[0]["dispatchResult"] == "SENT"
    assert [s for _, s, _ in ha.calls] == ["set_temperature"]


def test_dispatch_failure_alerts_instead_of_on_message(tmp_path: Path):
    ha = FakeHA()
    notifier = FakeNotifier()
    eng = _engine(tmp_path, ha, notifier)
    eng.settings.update({"masterFloorEntity": "sensor.floor", "dryRun": False})
    rm = _add_room(eng, mode="MANUAL", manual="HEAT")
    ha.set_sensor("sensor.floor", 21.0)
    ha.set_sensor(rm["roomTempEntity"], 20.0)
    ha.climate[rm["climateEntity"]] = {"state": "heat", "attributes": {"temperature": 15.0, "current_temperature": 20.0}}
    ha.fail_services.add(("climate", "set_temperature"))

    rec = _run(eng)[0]
    assert rec["dispatchResult"] == "FAILED" and rec["executionStatus"] == "FAILED"
    titles = [t for t, _ in notifier.sent]
    assert any("명령 실패" in t for t in titles)
    assert not any("보일러 켜짐" in t for t in titles)


def test_dynamic_setpoints_follow_thermostat_reading(tmp_path: Path):
    eng = _engine(tmp_path, FakeHA())
    hot = {"state": "heat", "attributes": {"current_temperature": 24.3}}
    assert eng.compute_command_setpoint("HEAT", hot) == 25.5
    cold = {"state": "heat", "attributes": {"current_temperature": 15.6}}
    assert eng.compute_command_setpoint("OFF", cold) == 14.5
    cold_min = {"state": "heat", "attributes": {"current_temperature": 15.6, "min_temp": 15.0}}
    assert eng.compute_command_setpoint("OFF", cold_min) == 15.0
    assert eng.compute_command_setpoint("HEAT", {"state": "off", "attributes": {}}) == 24.0


def test_anti_chatter_blocks_quick_reversal_only(tmp_path: Path):
    ha = FakeHA()
    eng = _engine(tmp_path, ha)
    eng.settings.update({"masterFloorEntity": "sensor.floor"})
    rm = _add_room(eng, target=21.0)
    ha.set_sensor("sensor.floor", 20.0)
    ha.set_sensor(rm["roomTempEntity"], 21.6)
    ha.climate[rm["climateEntity"]] = {"state": "heat", "attributes": {"temperature": 24.0, "current_temperature": 21.0}}
    tm = eng.room_timers.setdefault(rm["roomName"], {})
    tm.update({"lastCommandAction": "HEAT", "lastSwitchAt": _iso(datetime.now(tz=KST) - timedelta(seconds=60))})

    rec = _run(eng)[0]
    assert rec["commandAction"] == "HOLD" and rec["safetyReason"] == "P000_ANTI_CHATTER"
    tm["lastSwitchAt"] = _iso(datetime.now(tz=KST) - timedelta(minutes=6))
    assert _run(eng)[0]["commandAction"] == "OFF"


def test_underheat_alert_once(tmp_path: Path):
    eng = _engine(tmp_path, FakeHA())
    tm: dict = {}
    now = datetime.now(tz=KST)
    assert eng._update_underheat(tm, is_auto=True, room_temp=20.0, eff_target=21.0, reason="P000_RESIDUAL_COOLDOWN", now=now) is None
    tm["underheatSince"] = _iso(now - timedelta(minutes=95))
    top = eng._update_underheat(tm, is_auto=True, room_temp=20.0, eff_target=21.0, reason="P000_RESIDUAL_COOLDOWN", now=now)
    assert top and top[0][0] == "P000_RESIDUAL_COOLDOWN"
    assert eng._update_underheat(tm, is_auto=True, room_temp=20.0, eff_target=21.0, reason="X", now=now) is None
    eng._update_underheat(tm, is_auto=True, room_temp=20.95, eff_target=21.0, reason="X", now=now)
    assert "underheatSince" not in tm


# ---------------------------------------------------------------- P1 통계 / RC
def test_boiler_stats_time_based_and_dry_run_excluded():
    rows = [
        {"executionStartedAt": "2026-11-24T10:00:05+09:00", "commandAction": "HEAT", "dryRun": False},
        {"executionStartedAt": "2026-11-24T10:01:05+09:00", "commandAction": "HEAT", "dryRun": False},
        {"executionStartedAt": "2026-11-24T10:02:05+09:00", "commandAction": "HEAT", "dryRun": False},
        {"executionStartedAt": "2026-11-24T10:03:05+09:00", "commandAction": "HEAT", "dryRun": True,
         "combustionConfirmed": False},
        {"executionStartedAt": "2026-11-24T10:04:05+09:00", "commandAction": "OFF", "dryRun": False},
    ]
    s = compute_boiler_stats(rows, interval_sec=60, gas_krw_per_hour=1200)
    assert s["heatingMinutes"] == 3.0 and s["totalMinutes"] == 5.0
    assert s["shortCycles"] == 1 and s["estimatedGasKrw"] == 60


def test_status_kpis_use_24h_window(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    eng = app.state.engine
    _add_room(eng)
    old = CycleRecord(cycle_id="old-1", room_name="거실",
                      execution_started_at=_iso(datetime.now(tz=KST) - timedelta(hours=30)),
                      command_action="HEAT", room_temp=20.0, target_temp=21.0)
    eng.db.insert_cycle(old.to_payload())
    status = TestClient(app).get("/api/v1/status").json()
    assert status["kpis"]["totalCycles"] == 0 and status["statsWindowHours"] == 24
    assert {s["role"] for s in status["sensorHealth"]} == {"ROOM"}


def test_rc_fit_reduces_error_on_synthetic_data():
    truth = {"c_floor": 2500.0, "c_room": 600.0, "r_floor_room": 1.5, "r_room_ambient": 15.0, "q_heat_kw": 2.5}
    tf, tr, obs = 22.0, 20.0, []
    for k in range(240):
        heating = (k // 15) % 2 == 0
        q = truth["q_heat_kw"] if heating else 0.0
        q_fr = (tf - tr) / truth["r_floor_room"]
        q_ra = (tr - 2.0) / truth["r_room_ambient"]
        nf = tf + (q - q_fr) * 60.0 / truth["c_floor"]
        nr = tr + (q_fr - q_ra) * 60.0 / truth["c_room"]
        obs.append(RCObservation(60.0, tf, tr, 2.0, heating, nf, nr))
        tf, tr = nf, nr
    res = fit_rc_parameters(obs)
    assert res["status"] == "READY"
    assert res["mse"] < res["baselineMse"] * 0.2
    assert fit_rc_parameters(obs[:20])["status"] == "INSUFFICIENT_SAMPLES"


# ---------------------------------------------------------------- P2 운영
def test_options_precedence_ui_vs_addon_changes(tmp_path: Path):
    ha = FakeHA()
    opts = {"heat_on_setpoint": 24.0, "dry_run": True}
    e1 = _engine(tmp_path, ha, options=dict(opts))
    e1.settings["heatOnSetpoint"] = 25.0  # UI 에서 변경 저장
    e1.db.set_kv("settings", e1.settings)
    e2 = _engine(tmp_path, ha, options=dict(opts))  # 옵션 그대로 재부팅 -> UI 값 유지
    assert e2.settings["heatOnSetpoint"] == 25.0
    e3 = _engine(tmp_path, ha, options={**opts, "heat_on_setpoint": 26.0})  # HA 설정 화면에서 변경
    assert e3.settings["heatOnSetpoint"] == 26.0


def test_history_retention_prunes_old_cycles(tmp_path: Path):
    ha = FakeHA()
    e1 = _engine(tmp_path, ha)
    old = CycleRecord(cycle_id="old", room_name="거실",
                      execution_started_at=_iso(datetime.now(tz=KST) - timedelta(days=40)))
    new = CycleRecord(cycle_id="new", room_name="거실", execution_started_at=_iso(datetime.now(tz=KST)))
    e1.db.insert_cycle(old.to_payload())
    e1.db.insert_cycle(new.to_payload())
    _engine(tmp_path, ha, options={"history_retention_days": 30})
    assert [c["cycleId"] for c in e1.db.list_cycles()] == ["new"]


def test_telegram_chat_id_with_legacy_fallback():
    class TgHA(FakeHA):
        async def call_service(self, domain, service, data):
            self.calls.append((domain, service, data))
            if "chat_id" in data and self.reject_chat_id:
                raise RuntimeError("extra keys not allowed")

    ha = TgHA()
    ha.reject_chat_id = False
    n = PushNotifier(ha_client=ha, telegram_chat_id="-100123")
    assert asyncio.run(n.send("t", "m"))["telegram"] == "sent_to:-100123"
    assert ha.calls[-1][2]["chat_id"] == -100123
    ha.reject_chat_id = True
    assert asyncio.run(n.send("t", "m"))["telegram"].endswith("(legacy_target)")
    assert ha.calls[-1][2]["target"] == -100123


def test_weather_prefers_open_meteo_and_backs_off():
    gem_calls = {"n": 0}

    def gem_post(_u, _p):
        gem_calls["n"] += 1
        return {"candidates": [{"content": {"parts": [{"text": '{"outdoor_temp_c": 1.0}'}]}}]}

    om = OpenMeteoWeatherClient("서울", http_get=lambda _u: {"current": {"temperature_2m": 3.0, "wind_speed_10m": 1.0}})
    prov = FallbackWeatherProvider("서울", gemini_client=GoogleWeatherClient("k", "서울", http_post=gem_post),
                                   open_meteo_client=om)
    assert prov.get_weather(now_epoch=1000.0).source == "OPEN_METEO_API" and gem_calls["n"] == 0

    om_calls = {"n": 0}

    def failing_get(_u):
        om_calls["n"] += 1
        raise RuntimeError("down")

    om2 = OpenMeteoWeatherClient("서울", http_get=failing_get)
    om2.get_weather(now_epoch=1000.0)
    om2.get_weather(now_epoch=1100.0)  # 600초 backoff 중 재시도 안 함
    assert om_calls["n"] == 1


def test_config_dockerfile_version_consistency():
    cfg = yaml.safe_load((REPO / "itss_scada" / "config.yaml").read_text(encoding="utf-8"))
    assert set(cfg["options"]) == set(cfg["schema"])
    assert cfg["version"] == VERSION and cfg["arch"] == ["amd64"]
    docker = (REPO / "itss_scada" / "Dockerfile").read_text(encoding="utf-8")
    assert f'ITSS_BUILD_VERSION="{VERSION}"' in docker and "FROM ghcr.io/home-assistant/amd64-base-python:" in docker
    assert "--factory" in (REPO / "itss_scada" / "run.sh").read_text(encoding="utf-8")
    reqs = (REPO / "itss_scada" / "requirements.txt").read_text(encoding="utf-8").split()
    assert reqs and all("==" in r for r in reqs)


# ---------------------------------------------------------------- 실운전 로그/트레이스
def test_debug_trace_and_log_endpoints(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={"log_level": "debug"})
    eng = app.state.engine
    eng.settings["masterFloorEntity"] = "sensor.floor"
    rm = _add_room(eng)
    ha.set_sensor("sensor.floor", 20.5)
    ha.set_sensor(rm["roomTempEntity"], 20.0)
    _run(eng)

    files = list((tmp_path / "trace").glob("trace-*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
    for key in ("cycle", "inputs", "dynamics", "limits", "p200", "sentinel", "decision", "command", "timersAfter"):
        assert key in rec, key
    assert rec["room"] == "거실" and rec["cycle"]["floor"]["entity"] == "sensor.floor"

    c = TestClient(app)
    r = c.get("/api/v1/debug/trace", params={"date": "today", "room_name": "거실"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    assert json.loads(r.text.splitlines()[0])["room"] == "거실"
    assert c.get("/api/v1/debug/trace", params={"date": "../../etc"}).status_code == 422
    assert c.get("/api/v1/debug/trace", params={"date": "19990101"}).status_code == 404
    logs = c.get("/api/v1/debug/logs", params={"lines": 50}).json()
    assert logs["logPath"] and any("회차 #" in ln for ln in logs["lines"])
    assert c.get("/api/v1/debug/trace/files").json()["files"][0]["sizeBytes"] > 0

    # 다음 날 prune: 지난 일자 파일 gzip 압축 후에도 다운로드 가능, 보존 기간 지나면 삭제
    day = files[0].name[6:14]
    raw_size = files[0].stat().st_size
    eng.trace.prune(datetime.now(tz=KST) + timedelta(days=1))
    gz = tmp_path / "trace" / f"trace-{day}.jsonl.gz"
    assert gz.exists() and not files[0].exists() and gz.stat().st_size < raw_size
    r2 = c.get("/api/v1/debug/trace", params={"date": day})
    assert r2.status_code == 200 and json.loads(r2.text.splitlines()[0])["room"] == "거실"
    eng.trace.prune(datetime.now(tz=KST) + timedelta(days=30))
    assert not gz.exists()
