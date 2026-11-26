"""v2.9.1 회귀 테스트: 실내 센서 보정값, 쾌적 학습(월패드/수동 ON = 추움 신호, 제안 전용),
동시 비교 측정 보정값 계산, Gemini 일일 리포트(자문 전용).

배경 (2026-11-25 트레이스): 방별 실내 센서가 모두 다른 제품이고 교정되지 않음
(실내 센서 - 조절기 내장 온도: 방3 +3.6, 거실 +1.5, 방2 +0.3, 안방 +2.2°C). 월패드 조작이 반복됨.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import itss_scada.app.main as main_mod
from itss_scada.app import comfort
from itss_scada.app.const import KST
from itss_scada.app.integrations.google_gemini_advisor import GeminiThermalAdvisor
from itss_scada.app.main import create_app
from test_v27_hardening import FakeHA, FakeNotifier, NoWeather
from test_v291_external_boost import FOLLOWER, INDEP, LEADER, _cycle, _house, _wallpad_set


@pytest.fixture(autouse=True)
def _fast_dispatch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_mod, "DEFAULT_RECHECK_DELAY_SEC", 0.0)


NOW = datetime(2026, 11, 26, 8, 0, tzinfo=KST)


# ---------------------------------------------------------------- 실내 센서 보정값
def test_room_offset_applies_to_control_and_safety(tmp_path: Path):
    eng, ha, _ = _house(tmp_path / "a", room=22.0)
    assert _cycle(eng)[INDEP]["commandAction"] == "OFF"  # 22.0 > 목표 21.0

    # 원시 22.0 + 보정 -1.5 = 20.5 -> 목표 미달로 난방, 기록/트레이스는 보정값 기준 + 원시값 보존
    eng.db.update_room_control(INDEP, room_temp_offset_c=-1.5)
    r = _cycle(eng)[INDEP]
    assert r["roomTemp"] == 20.5 and r["commandAction"] == "HEAT"
    assert r["diagnostics"]["roomTempOffsetC"] == -1.5
    # 센서 트랙은 원시값 유지 (보정값 변경으로 가짜 급변/환기 감지 없음)
    assert eng.sensor_tracks["sensor.g_temp"]["lastValue"] == 22.0

    # 과열 보호도 보정값 기준: 원시 23.5 + 0.6 = 24.1 >= 24.0
    eng2, ha2, _ = _house(tmp_path / "b", room=23.5)
    eng2.db.update_room_control(INDEP, room_temp_offset_c=0.6)
    r2 = _cycle(eng2)[INDEP]
    assert r2["roomTemp"] == 24.1 and r2["safetyReason"] == "P000_OVERHEAT_CUTOFF"


def test_room_offset_api_validation_and_reregister_keeps_value(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    c = TestClient(app)
    body = {"roomName": INDEP, "roomTempEntity": "sensor.g_temp", "climateEntity": "climate.g", "targetTemp": 21.0}
    assert c.post("/api/v1/rooms", json=body).json()["roomTempOffsetC"] == 0.0
    assert c.patch(f"/api/v1/rooms/{INDEP}", json={"roomTempOffsetC": -1.2}).json()["roomTempOffsetC"] == -1.2
    assert c.patch(f"/api/v1/rooms/{INDEP}", json={"roomTempOffsetC": 3.5}).status_code == 422
    # 같은 이름 재등록(보정값 미지정) -> 기존 보정값 유지
    assert c.post("/api/v1/rooms", json={**body, "targetTemp": 21.5}).json()["roomTempOffsetC"] == -1.2
    assert c.post("/api/v1/rooms", json={**body, "roomTempOffsetC": 0.4}).json()["roomTempOffsetC"] == 0.4

    ha.set_sensor("sensor.g_temp", 22.0)
    live = c.get("/api/v1/status").json()["liveByRoom"][INDEP]
    assert live["roomTemp"] == 22.4 and live["roomTempRaw"] == 22.0 and live["roomTempOffsetC"] == 0.4


# ---------------------------------------------------------------- 쾌적 학습 이벤트 수집
def test_wallpad_and_manual_on_are_recorded_as_comfort_signals(tmp_path: Path):
    # 원시 21.5 + 보정 -0.3 = 21.2 (목표 21.0 이상 -> ITSS 는 대기)
    eng, ha, _ = _house(tmp_path, floor=19.8, room=21.5)
    eng.db.update_room_control(INDEP, room_temp_offset_c=-0.3)
    assert _cycle(eng)[INDEP]["commandAction"] == "OFF"
    assert eng.comfort_events == []

    _wallpad_set(ha, INDEP, 24.0)
    _cycle(eng)
    ev = eng.comfort_events[-1]
    assert ev["room"] == INDEP and ev["source"] == "WALLPAD"
    assert ev["roomTempRaw"] == 21.5 and ev["roomOffsetC"] == -0.3 and ev["roomTemp"] == 21.2
    assert ev["floorSrc"] == "ESTIMATE_OFF"
    _cycle(eng)  # ACTIVE 는 추가 기록 없음
    assert len(eng.comfort_events) == 1

    # 방2 월패드 -> 방3 연동 가동이지만 신호는 조작한 방2 만
    _wallpad_set(ha, FOLLOWER, 24.0)
    _cycle(eng)
    srcs = [(e["room"], e["source"]) for e in eng.comfort_events]
    assert (FOLLOWER, "WALLPAD") in srcs and not any(r == LEADER for r, _ in srcs)
    fol = next(e for e in eng.comfort_events if e["room"] == FOLLOWER)
    assert fol["floorSrc"] == "ESTIMATED" and fol["floorTemp"] is not None

    # ITSS 수동 ON 은 세션당 1회
    eng2, _, _ = _house(tmp_path / "m")
    eng2.db.update_room_control(INDEP, control_mode="MANUAL", manual_action="HEAT")
    _cycle(eng2)
    _cycle(eng2)
    assert [e["source"] for e in eng2.comfort_events] == ["ITSS_MANUAL"]
    # 재시작 후에도 유지 (kv 저장)
    assert eng2.db.get_kv("comfort_events")[0]["room"] == INDEP


# ---------------------------------------------------------------- 쾌적 학습 분석 (순수 함수)
def _ev(day: int, hour: int, room: str, raw: float, eff: float, *, base: float | None = None,
        floor: float | None = None, src: str = "ESTIMATE_OFF", off: float = 0.0) -> dict:
    return comfort.make_event(
        now=NOW - timedelta(days=day, hours=NOW.hour - hour), room=room, source="WALLPAD",
        room_temp_raw=raw, room_offset_c=off, target_temp=base if base is not None else eff, eff_target=eff,
        floor_temp=floor, floor_src=src, outdoor_temp=0.0,
    )


def test_comfort_target_up_suggestion_is_absolute_and_not_compounding():
    rooms = {INDEP: {"roomName": INDEP, "targetTemp": 21.0, "roomTempOffsetC": 0.0}}
    evs = [_ev(1, 20, INDEP, 21.0, 21.0), _ev(2, 21, INDEP, 20.9, 21.0), _ev(3, 20, INDEP, 21.1, 21.0)]
    res = comfort.analyze(evs, rooms, now=NOW)
    sg = next(s for s in res["suggestions"] if s["type"] == "TARGET_UP")
    assert sg["room"] == INDEP and sg["current"] == 21.0 and sg["suggested"] == 21.4  # 중앙 21.0 + 0.4
    assert res["byRoom"][INDEP]["peakHours"] == "19~22시"

    # 2건이면 근거 부족 -> 제안 없음
    assert comfort.analyze(evs[:2], rooms, now=NOW)["suggestions"] == []
    # 적용 후(목표 21.4) 같은 이벤트로 다시 올리지 않음
    rooms_applied = {INDEP: {**rooms[INDEP], "targetTemp": 21.4}}
    assert comfort.analyze(evs, rooms_applied, now=NOW)["suggestions"] == []
    # 스케줄 오프셋(-1.0) 구간의 조작은 기본 목표 기준으로 환산: 20.1 + 0.4 - (-1.0) = 21.5
    evs_night = [_ev(d, 23, INDEP, 20.1, 20.0, base=21.0) for d in (1, 2, 3)]
    assert next(s for s in comfort.analyze(evs_night, rooms, now=NOW)["suggestions"])["suggested"] == 21.5
    # 14일 이전 이벤트는 제외
    old = [_ev(d, 20, INDEP, 21.0, 21.0) for d in (20, 21, 22)]
    assert comfort.analyze(old, rooms, now=NOW)["eventCount"] == 0


def test_comfort_offset_change_reinterprets_past_signals():
    evs = [_ev(d, 20, INDEP, 21.6, 21.0) for d in (1, 2, 3)]  # 원시 21.6, 당시 보정 0
    rooms = {INDEP: {"roomName": INDEP, "targetTemp": 21.0, "roomTempOffsetC": 0.0}}
    # 보정 전: 목표보다 +0.6 따뜻한데 켬 -> 21.6 + 0.4 = 22.0 제안
    assert comfort.analyze(evs, rooms, now=NOW)["suggestions"][0]["suggested"] == 22.0
    # 동시 비교 측정으로 보정 -0.6 확인 -> 실제 21.0 에서 켠 것 -> 21.4 제안
    rooms[INDEP]["roomTempOffsetC"] = -0.6
    assert comfort.analyze(evs, rooms, now=NOW)["suggestions"][0]["suggested"] == 21.4


def test_comfort_floor_cold_and_underheat_classification():
    rooms = {
        LEADER: {"roomName": LEADER, "targetTemp": 21.0, "roomTempOffsetC": 0.0},
        FOLLOWER: {"roomName": FOLLOWER, "targetTemp": 21.0, "roomTempOffsetC": 0.0},
        INDEP: {"roomName": INDEP, "targetTemp": 21.0, "roomTempOffsetC": 0.0},
    }
    # 방3/방2: 실내는 목표 근처, 바닥 19.6~19.9 -> 바닥 최저 온도 제안 (중앙 19.75 + 1.0 -> 0.5 올림 21.0)
    evs = [
        _ev(1, 19, LEADER, 21.0, 21.0, floor=19.7, src="MEASURED"),
        _ev(2, 19, LEADER, 21.1, 21.0, floor=19.6, src="MEASURED"),
        _ev(1, 20, FOLLOWER, 21.0, 21.0, floor=19.8, src="ESTIMATED"),
        _ev(3, 20, FOLLOWER, 20.9, 21.0, floor=19.9, src="ESTIMATED"),
    ]
    # 거실: 목표보다 1°C 이상 낮을 때 켬 -> 설정 문제가 아니라 난방 부족 점검
    evs += [_ev(d, 18, INDEP, 19.8, 21.0) for d in (1, 2, 3)]
    res = comfort.analyze(evs, rooms, now=NOW, floor_min_c=0.0)
    by_type = {s["type"]: s for s in res["suggestions"]}
    assert by_type["FLOOR_MIN"]["suggested"] == 21.0 and by_type["FLOOR_MIN"]["events"] == 4
    assert by_type["CONTROL_CHECK"]["room"] == INDEP
    assert "TARGET_UP" not in by_type  # 바닥 문제/난방 부족은 공기 목표 상향으로 오인하지 않음
    # 이미 21.0 으로 설정 -> 바닥 제안 없음
    assert all(s["type"] != "FLOOR_MIN" for s in comfort.analyze(evs, rooms, now=NOW, floor_min_c=21.0)["suggestions"])


def test_comfort_api_and_reset(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    c = TestClient(app)
    eng = app.state.engine
    eng.db.upsert_room({"roomName": INDEP, "roomTempEntity": "sensor.g_temp", "climateEntity": "climate.g",
                        "targetTemp": 21.0})
    now = datetime.now(tz=KST)
    eng.comfort_events = [
        comfort.make_event(now=now - timedelta(days=d), room=INDEP, source="WALLPAD", room_temp_raw=21.0,
                           room_offset_c=0.0, target_temp=21.0, eff_target=21.0, floor_temp=None,
                           floor_src="ESTIMATE_OFF", outdoor_temp=1.0)
        for d in (1, 2, 3)
    ]
    res = c.get("/api/v1/comfort").json()
    assert res["suggestions"][0]["suggested"] == 21.4 and len(res["events"]) == 3
    assert c.get("/api/v1/status").json()["comfort"]["eventCount"] == 3
    assert c.post("/api/v1/comfort/reset").json()["deleted"] == 3
    assert eng.comfort_events == [] and eng.db.get_kv("comfort_events") == []


# ---------------------------------------------------------------- 동시 비교 측정 보정값 계산
def _seed_colocation(eng, start: datetime, hours: float, raws: dict[str, float], offsets: dict[str, float]) -> None:
    n = int(hours * 60)
    for i in range(n):
        ts = start + timedelta(minutes=i)
        for room, raw in raws.items():
            off = offsets.get(room, 0.0)
            # 30분마다 새 보고 (보고 주기 긴 센서 모사), 값은 +-0.1 흔들림
            rep_at = start + timedelta(minutes=(i // 30) * 30)
            eng.db.insert_cycle({
                "cycleId": f"{room}-{i}", "roomName": room,
                "executionStartedAt": ts.isoformat(timespec="seconds"),
                "sensorMeasuredAtRoom": rep_at.isoformat(timespec="seconds"),
                "roomTemp": round(raw + off + (0.1 if (i // 30) % 2 else -0.1), 2),
                "diagnostics": {"roomTempOffsetC": off} if off else {},
            })


def test_sensor_calibration_endpoint(tmp_path: Path):
    ha = FakeHA()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=FakeNotifier(),
                     weather_client=NoWeather(), options={})
    c = TestClient(app)
    eng = app.state.engine
    for name, ent in ((LEADER, "y"), (FOLLOWER, "s"), (INDEP, "g")):
        eng.db.upsert_room({"roomName": name, "roomTempEntity": f"sensor.{ent}_temp", "climateEntity": f"climate.{ent}",
                            "targetTemp": 21.0, "roomTempOffsetC": 0.5 if name == FOLLOWER else 0.0})
    start = datetime(2026, 11, 28, 23, 0, tzinfo=KST)
    # 원시값: 방3 21.0, 방2 22.0 (기록은 보정 +0.5 적용된 22.5), 거실 20.5
    _seed_colocation(eng, start, 3.0, {LEADER: 21.0, FOLLOWER: 22.0, INDEP: 20.5}, {FOLLOWER: 0.5})
    body = {"start": start.isoformat(), "end": (start + timedelta(hours=3)).isoformat()}

    res = c.post("/api/v1/calibration/room-sensors", json=body).json()
    sug = {r["room"]: r["suggestedOffsetC"] for r in res["rooms"]}
    assert res["reference"]["valueC"] == 21.0  # 원시 중앙값들의 중앙값
    assert sug == {LEADER: 0.0, FOLLOWER: -1.0, INDEP: 0.5}
    assert all(r["status"] == "OK" and r["reports"] >= 6 for r in res["rooms"])

    # 기준 온도계 21.3°C
    sug_t = {r["room"]: r["suggestedOffsetC"]
             for r in c.post("/api/v1/calibration/room-sensors", json={**body, "referenceTempC": 21.3}).json()["rooms"]}
    assert sug_t == {LEADER: 0.3, FOLLOWER: -0.7, INDEP: 0.8}
    # 기준 방 = 방2 (원시 22.0 + 현재 보정 0.5 = 22.5)
    sug_r = {r["room"]: r["suggestedOffsetC"]
             for r in c.post("/api/v1/calibration/room-sensors", json={**body, "reference": FOLLOWER}).json()["rooms"]}
    assert sug_r == {LEADER: 1.5, FOLLOWER: 0.5, INDEP: 2.0}

    short = {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()}
    assert c.post("/api/v1/calibration/room-sensors", json=short).status_code == 422
    assert c.post("/api/v1/calibration/room-sensors", json={**body, "reference": "없는방"}).status_code == 404


# ---------------------------------------------------------------- Gemini 일일 리포트
def _fake_gemini(captured: list):
    def _post(url: str, payload: dict) -> dict:
        captured.append((url, payload))
        return {"candidates": [{"content": {"parts": [
            {"text": "THOUGHT_SECRET", "thought": True},
            {"text": "1) 총평: ROOM_A 가 저녁에 추웠습니다."},  # 거실(가나다순 첫 방) 별칭
        ]}}]}
    return _post


def test_daily_report_compose_and_send(tmp_path: Path):
    eng, ha, notifier = _house(tmp_path, floor=19.8, room=21.1)
    _cycle(eng)
    _wallpad_set(ha, INDEP, 24.0)
    _cycle(eng)
    captured: list = []
    eng.advisor = GeminiThermalAdvisor("test-key", http_post=_fake_gemini(captured))

    res = asyncio.run(eng.create_daily_report(send=True))
    assert res["status"] == "OK" and res["sent"] is True
    title, text = notifier.sent[-1]
    assert "일일 리포트" in title
    assert "보일러 가동" in text and f"{INDEP}(월패드·외부 자동화" in text
    # 응답의 별칭은 원래 방 이름으로 복원, 사고(thought) 파트 제외
    assert "Gemini 분석" in text and f"총평: {INDEP} 가" in text and "THOUGHT_SECRET" not in text
    url, payload = captured[0]
    assert url.endswith("/models/gemini-3.8-flash:generateContent")
    prompt = payload["contents"][0]["parts"][0]["text"]
    assert "userHeatRequests" in prompt and "마크다운" in prompt
    # 방 이름은 익명화해 전송 (개인정보 보호)
    assert all(n not in prompt for n in (INDEP, LEADER, FOLLOWER)) and '"room": "ROOM_A"' in prompt

    # API 키 없음 -> 결정론적 요약만
    eng.advisor = GeminiThermalAdvisor("")
    res2 = asyncio.run(eng.create_daily_report())
    assert res2["status"] == "NO_API_KEY" and "Gemini 분석 없음" in res2["text"] and "sent" not in res2


def test_daily_report_schedule_once_per_day(tmp_path: Path):
    eng, _, notifier = _house(tmp_path)
    _cycle(eng)
    eng.advisor = GeminiThermalAdvisor("")
    day = datetime.now(tz=KST).replace(hour=8, minute=0, second=0, microsecond=0)
    assert eng.daily_report_due(day - timedelta(minutes=1)) is False
    assert eng.daily_report_due(day) is True
    assert eng.daily_report_due(day + timedelta(hours=4)) is False  # 발송 창(4시간) 밖

    async def _go(now: datetime) -> bool:
        started = eng.maybe_start_daily_report(now)
        if eng._report_task is not None:
            await eng._report_task
        return started

    assert asyncio.run(_go(day + timedelta(minutes=5))) is True
    assert any("일일 리포트" in t for t, _ in notifier.sent)
    n = len(notifier.sent)
    assert asyncio.run(_go(day + timedelta(minutes=10))) is False  # 같은 날 재발송 없음
    assert len(notifier.sent) == n

    eng.settings["dailyReportEnabled"] = False
    assert eng.daily_report_due(day + timedelta(days=1)) is False


def test_daily_report_settings_and_endpoint(tmp_path: Path):
    ha = FakeHA()
    notifier = FakeNotifier()
    app = create_app(db_path=str(tmp_path / "a.db"), ha_client=ha, notifier=notifier,
                     weather_client=NoWeather(), options={})
    c = TestClient(app)
    eng = app.state.engine
    assert eng.settings["dailyReportEnabled"] is True and eng.settings["dailyReportHour"] == 8
    assert c.patch("/api/v1/settings", json={"dailyReportHour": 21}).json()["dailyReportHour"] == 21
    assert c.patch("/api/v1/settings", json={"dailyReportHour": 24}).status_code == 422
    assert c.patch("/api/v1/settings", json={"dailyReportEnabled": False}).json()["dailyReportEnabled"] is False

    res = c.post("/api/v1/report/daily").json()
    assert {"kpis", "report", "status", "text"} <= set(res) and notifier.sent == []
    assert c.post("/api/v1/report/daily?send=true").json()["sent"] is True
    assert any("일일 리포트" in t for t, _ in notifier.sent)

    html = (Path(__file__).resolve().parents[1] / "itss_scada" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    for el in ('id="dailyReportHour"', 'id="comfortSuggest"', 'id="calStart"', 'data-act="roomoffset"'):
        assert el in html
