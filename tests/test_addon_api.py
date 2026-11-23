"""Integration tests for the I.T.S.S. SCADA Add-on Web UI, per-room target temp, and manual mode."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from itss_scada.app.main import create_app


class FakeHAClient:
    """In-memory mock of HomeAssistantClient for deterministic testing."""

    def __init__(self) -> None:
        self.is_configured = True
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

    async def call_service(self, domain: str, service: str, data: dict):
        self.calls.append((domain, service, data))


def test_addon_web_ui_room_target_and_manual_mode(tmp_path: Path):
    fake_ha = FakeHAClient()
    app = create_app(db_path=str(tmp_path / "addon.db"), ha_client=fake_ha)

    with TestClient(app) as client:
        # 1. Web UI HTML served at root
        r_html = client.get("/")
        assert r_html.status_code == 200
        assert "I.T.S.S. Thermal SCADA Dashboard" in r_html.text

        # 2. Update target temperature for living room
        r_patch = client.patch("/api/v1/rooms/거실", json={"targetTemp": 23.4})
        assert r_patch.status_code == 200
        assert r_patch.json()["targetTemp"] == 23.4

        # 3. Switch master bedroom to MANUAL HEAT override
        r_man = client.patch("/api/v1/rooms/안방", json={"controlMode": "MANUAL", "manualAction": "HEAT"})
        assert r_man.status_code == 200
        assert r_man.json()["controlMode"] == "MANUAL"
        assert r_man.json()["manualAction"] == "HEAT"

        # 4. Run control cycle and verify MANUAL override takes effect
        r_run = client.post("/api/v1/control/run-now")
        assert r_run.status_code == 200
        records = {rec["roomName"]: rec for rec in r_run.json()["records"]}
        assert records["거실"]["commandAction"] == "HEAT"
        assert records["거실"]["floorSensorSource"] == "MEASURED"
        assert records["안방"]["safetyReason"] == "P000_MANUAL_OVERRIDE"
        assert records["안방"]["commandAction"] == "HEAT"
        assert records["안방"]["floorSensorSource"] == "ESTIMATED"
