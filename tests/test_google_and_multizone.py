"""Unit tests for Google API integrations (non-Nest) and single-floor-sensor multi-zone control."""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path

from itss_scada.app.const import KST
from itss_scada.app.integrations.google_alerts import (
    GeminiTTSAlert,
    format_cycle_for_sheets,
)
from itss_scada.app.integrations.google_calendar import GoogleCalendarAdvisor
from itss_scada.app.integrations.google_gemini_advisor import (
    GeminiThermalAdvisor,
    compute_daily_kpis,
)
from itss_scada.app.integrations.google_weather import (
    FallbackWeatherProvider,
    GoogleWeatherClient,
    OpenMeteoWeatherClient,
)
from itss_scada.app.protocols.multi_zone import MultiZoneController, ZoneReading


def test_multizone_hybrid_synchronized_start_and_early_cutoff():
    ctrl = MultiZoneController(strategy="hybrid")
    zones = [
        ZoneReading("living", room_temp=22.8, target_temp=23.0, is_heating=False, has_floor_sensor=True),
        ZoneReading("bed1", room_temp=22.92, target_temp=23.0, is_heating=False, floor_offset_from_master_c=0.4),
        ZoneReading("bed2", room_temp=23.2, target_temp=23.0, is_heating=True, floor_offset_from_master_c=0.2),
    ]
    plan = ctrl.evaluate(zones, master_floor_temp=25.5)
    by_room = {c.room_name: c for c in plan.commands}

    assert plan.boiler_demand is True
    assert by_room["living"].action == "HEAT"
    assert by_room["living"].floor_sensor_source == "MEASURED"
    assert by_room["bed1"].action == "HEAT"
    assert by_room["bed1"].floor_sensor_source == "ESTIMATED"
    assert by_room["bed1"].effective_floor_temp == 25.9
    assert by_room["bed2"].action == "OFF"
    assert by_room["bed2"].reason == "HYBRID_EARLY_CUTOFF"


def test_multizone_simultaneous_global_overheat_guard():
    ctrl = MultiZoneController(strategy="simultaneous")
    zones = [
        ZoneReading("living", room_temp=22.5, target_temp=23.0, is_heating=True, has_floor_sensor=True),
        ZoneReading("small_room", room_temp=27.1, target_temp=23.0, is_heating=True),
    ]
    plan = ctrl.evaluate(zones, master_floor_temp=26.0)
    assert plan.boiler_demand is False
    assert all(c.action == "OFF" for c in plan.commands)


def test_google_weather_client_caching_and_fallback():
    calls = 0

    def fake_post(_url: str, _payload: dict) -> dict:
        nonlocal calls
        calls += 1
        return {
            "candidates": [
                {"content": {"parts": [{"text": '{"outdoor_temp_c": -2.5, "wind_speed_ms": 6.5, "condition": "맑음"}'}]}}
            ]
        }

    client = GoogleWeatherClient("dummy-key", "서울", cache_ttl_sec=1800, http_post=fake_post)
    w1 = client.get_weather(now_epoch=1000.0)
    w2 = client.get_weather(now_epoch=1500.0)
    assert w1.outdoor_temp_c == -2.5
    assert w1.wind_speed_ms == 6.5
    assert w2.source == "GEMINI_SEARCH_GROUNDING"
    assert calls == 1

    def failing_post(_url: str, _payload: dict) -> dict:
        raise RuntimeError("network down")

    fallback_client = GoogleWeatherClient("dummy-key", "서울", http_post=failing_post)
    wf = fallback_client.get_weather(now_epoch=2000.0)
    assert wf.outdoor_temp_c is None
    assert wf.source == "FALLBACK"

    # Open-Meteo fallback works without API key when Gemini/Naver weather fail
    om = OpenMeteoWeatherClient(
        "수원",
        http_get=lambda _url: {"current": {"temperature_2m": 3.4, "wind_speed_10m": 1.8}},
    )
    provider = FallbackWeatherProvider("수원", gemini_client=fallback_client, open_meteo_client=om)
    res = provider.get_weather(now_epoch=2000.0)
    assert res.outdoor_temp_c == 3.4
    assert res.source == "OPEN_METEO_API"


def test_google_calendar_advisor_away_and_preheat():
    advisor = GoogleCalendarAdvisor()
    events = [
        {
            "summary": "지방 출장",
            "start": {"dateTime": "2026-11-23T09:00:00+09:00"},
            "end": {"dateTime": "2026-11-23T17:00:00+09:00"},
        },
        {
            "summary": "저녁 귀가",
            "start": {"dateTime": "2026-11-23T18:30:00+09:00"},
            "end": {"dateTime": "2026-11-23T20:00:00+09:00"},
        },
    ]

    away = advisor.evaluate_events(events, now_kst=datetime(2026, 11, 23, 12, 0, tzinfo=KST))
    assert away.mode == "AWAY"
    assert away.target_offset_c == -2.0

    preheat = advisor.evaluate_events(events, now_kst=datetime(2026, 11, 23, 17, 30, tzinfo=KST))
    assert preheat.mode == "PREHEAT"
    assert preheat.target_offset_c == 0.2


def test_gemini_daily_advisor_and_alerts(tmp_path: Path):
    cycles = [
        {"cycleId": "c1", "roomName": "living", "executionStartedAt": "2026-11-23T01:00:00+09:00",
         "savedAt": "2026-11-23T01:00:02+09:00", "roomTemp": 23.0, "targetTemp": 23.0, "safetyReason": "P000_OK"},
        {"cycleId": "c2", "roomName": "living", "executionStartedAt": "2026-11-23T01:05:00+09:00",
         "savedAt": "2026-11-23T01:05:02+09:00", "roomTemp": 23.4, "targetTemp": 23.0, "safetyReason": "P000_OK"},
    ]
    kpis = compute_daily_kpis(cycles)
    assert kpis["bandCompliancePct"] == 50.0
    assert kpis["maxOvershootC"] == 0.4

    advisor = GeminiThermalAdvisor(
        "dummy-key",
        http_post=lambda _u, _p: {"candidates": [{"content": {"parts": [{"text": "오버슈트 0.4C 관측."}]}}]},
    )
    res = advisor.generate_report(cycles)
    assert res["status"] == "OK"
    assert "오버슈트" in res["report"]

    fake_wav = base64.b64encode(b"RIFF....WAVE").decode("utf-8")
    tts = GeminiTTSAlert(
        "dummy-key",
        http_post=lambda _u, _p: {"candidates": [{"content": {"parts": [{"inlineData": {"data": fake_wav}}]}}]},
    )
    wav_file = tmp_path / "alert.wav"
    out = tts.synthesize_alert_if_critical("P000_OVERHEAT_CUTOFF", "거실", str(wav_file))
    assert out == str(wav_file)
    assert wav_file.read_bytes() == b"RIFF....WAVE"
    assert tts.synthesize_alert_if_critical("P000_OK", "거실", str(wav_file)) is None

    row = format_cycle_for_sheets(cycles[0])
    assert row[0] == "c1" and row[1] == "living"
