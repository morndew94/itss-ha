"""Home Assistant Supervisor Core API client for the I.T.S.S. Add-on."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import httpx

from .const import KST

_LOGGER = logging.getLogger(__name__)


class HomeAssistantClient:
    """Reads entity states and calls climate services via HA Supervisor API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("HA_BASE_URL") or "http://supervisor/core/api").rstrip("/")
        self.token = token or os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN") or ""
        self.last_error: str | None = None

    @property
    def is_configured(self) -> bool:
        """Return True when a Supervisor or Long-Lived Access Token is available."""
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """Fetch raw state dictionary for one Home Assistant entity."""
        if not self.is_configured or not entity_id:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.base_url}/states/{entity_id}", headers=self._headers())
                if resp.status_code == 404:
                    self.last_error = f"Entity not found (404): {entity_id}"
                    return None
                resp.raise_for_status()
                self.last_error = None
                return resp.json()
        except Exception as err:  # noqa: BLE001
            self.last_error = f"HA API error ({entity_id}): {err}"
            _LOGGER.warning("%s", self.last_error)
            return None

    async def get_numeric_state(self, entity_id: str) -> tuple[float | None, str | None, float | None]:
        """Return (value_0p1, last_updated_kst_iso, age_sec) for a numeric sensor."""
        raw = await self.get_state(entity_id)
        if not raw or raw.get("state") in (None, "unknown", "unavailable"):
            return None, None, None
        try:
            val = round(float(raw["state"]), 1)
        except (TypeError, ValueError):
            return None, None, None

        ts_str = raw.get("last_updated")
        updated_iso = None
        age_sec = None
        if ts_str:
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).astimezone(KST)
            updated_iso = dt.isoformat(timespec="seconds")
            age_sec = round((datetime.now(tz=KST) - dt).total_seconds(), 1)
        return val, updated_iso, age_sec

    async def list_entities(self) -> dict[str, list[dict[str, Any]]]:
        """Return sensor and climate entities from Home Assistant for UI selection."""
        if not self.is_configured:
            return {"sensors": [], "climates": []}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.base_url}/states", headers=self._headers())
                resp.raise_for_status()
                items = resp.json()
        except Exception as err:  # noqa: BLE001
            self.last_error = f"HA states list error: {err}"
            return {"sensors": [], "climates": []}

        sensors: list[dict[str, Any]] = []
        climates: list[dict[str, Any]] = []
        for it in items:
            eid = str(it.get("entity_id", ""))
            attrs = it.get("attributes") or {}
            entry = {
                "entityId": eid,
                "name": attrs.get("friendly_name", eid),
                "state": it.get("state"),
                "unit": attrs.get("unit_of_measurement", ""),
                "deviceClass": attrs.get("device_class", ""),
            }
            if eid.startswith("sensor."):
                sensors.append(entry)
            elif eid.startswith("climate."):
                climates.append(entry)

        sensors.sort(key=lambda x: x["entityId"])
        climates.sort(key=lambda x: x["entityId"])
        return {"sensors": sensors, "climates": climates}

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Invoke a Home Assistant service (e.g., climate.set_temperature)."""
        if not self.is_configured:
            return
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/services/{domain}/{service}",
                headers=self._headers(),
                json=data,
            )
            resp.raise_for_status()
