"""Integration tests for the I.T.S.S. SCADA Add-on v2.0 features."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from itss_scada.app.integrations.notifications import PushNotifier
from itss_scada.app.main import create_app


class FakeHAClient:
    """In-memory mock of HomeAssistantClient for deterministic testing."""

    def __init__(self) -> None:
        self.is_configured = True
        self.token_source = "mock"
        self.last_error = None
        self.states = {
            "sensor.living_floor_temperature": 25.0,
            "sensor.living_temperature": 22.7,
            "sensor.master_bedroom_temperature": 23.3,
        }
        self.calls: list[tuple[str, str, dict]] = []

    async def get_numeric_state(self, entity_id: str):
        val = self.states.get(entity_id)
        if val is None:
            return None, None, None
        return val, "2026-11-23T13:00:00+09:00", 5.0

    async def get_state(self, entity_id: str):
        return {"state": "off", "attributes": {"hvac_action": "idle"}}

    async def list_entities(self):
        return {
            "tempSensors": [{"entityId": "sensor.living_temperature", "name": "거실 온도", "state": "22.7", "unit": "°C", "deviceClass": "temperature"}],
            "sensors": [],
            "climates": [{"entityId": "climate.living_thermostat", "name": "거실 난방", "state": "off", "unit": "", "deviceClass": ""}],
        }

    async def call_service(self, domain: str, service: str, data: dict):
        self.calls.append((domain, service, data))


def test_addon_v2_full_workflow(tmp_path: Path):
    fake_ha = FakeHAClient()
    notifier = PushNotifier(
        ha_client=fake_ha,
        notify_service="mobile_app_phone",
        telegram_chat_id="12345",
    )
    app = create_app(db_path=str(tmp_path / "addon.db"), ha_client=fake_ha, notifier=notifier)

    with TestClient(app) as client:
        # 1. Web UI & HA entity dropdown endpoint
        r_html = client.get("/")
        assert r_html.status_code == 200
        assert "I.T.S.S. Thermal SCADA Dashboard v2.0" in r_html.text
        assert len(client.get("/api/v1/ha/entities").json()["tempSensors"]) == 1

        # 2. Configure master floor sensor & register rooms
        client.patch("/api/v1/settings", json={"masterFloorEntity": "sensor.living_floor_temperature", "dryRun": False})
        assert client.post("/api/v1/rooms", json={
            "roomName": "거실",
            "roomTempEntity": "sensor.living_temperature",
            "climateEntity": "climate.living_thermostat",
            "hasFloorSensor": True,
            "targetTemp": 23.4,
        }).status_code == 200
        assert client.post("/api/v1/rooms", json={
            "roomName": "안방",
            "roomTempEntity": "sensor.master_bedroom_temperature",
            "climateEntity": "climate.master_bedroom_thermostat",
            "hasFloorSensor": False,
            "targetTemp": 22.5,
        }).status_code == 200

        # 3. Manual OFF on 안방, AUTO HEAT on 거실 -> ON=24C, OFF=15C
        client.patch("/api/v1/rooms/안방", json={"controlMode": "MANUAL", "manualAction": "OFF"})
        r_run = client.post("/api/v1/control/run-now")
        assert r_run.status_code == 200
        records = {rec["roomName"]: rec for rec in r_run.json()["records"]}
        assert records["거실"]["commandSetpoint"] == 24.0
        assert records["안방"]["commandSetpoint"] == 15.0

        # 4. Boiler stats & P400 schedules
        status = client.get("/api/v1/status").json()
        assert status["boilerStats"]["heatingMinutes"] == 5.0
        assert status["boilerStats"]["estimatedGasKrw"] == 100
        scheds = client.get("/api/v1/schedules").json()
        assert len(scheds) == 4

        # 5. One-touch preset & auto-calibration & push notification
        assert client.post("/api/v1/preset", json={"preset": "AWAY"}).json()["offsetC"] == -2.0
        assert "rcFit" in client.post("/api/v1/calibrate/auto").json()
        n_res = client.post("/api/v1/notify/test").json()
        assert n_res["ha_notify"].startswith("sent_via_service:")
        assert n_res["telegram"] == "sent_to:12345"
        tg_calls = [d for dom, svc, d in fake_ha.calls if dom == "telegram_bot" and svc == "send_message"]
        assert len(tg_calls) == 1 and tg_calls[0]["target"] == 12345

        # 6. Diagnose & direct climate test endpoints
        diag = client.get("/api/v1/ha/diagnose").json()
        assert diag["haConnected"] is True
        assert len(diag["rooms"]) == 2
        t_res = client.post("/api/v1/rooms/거실/test-climate", json={"action": "HEAT"}).json()
        assert t_res["targetSetpoint"] == 24.0
        assert any(s["call"] == "climate.set_temperature(24.0)" and s["status"] == "ok" for s in t_res["steps"])
