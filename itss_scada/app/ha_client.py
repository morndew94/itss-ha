"""Home Assistant Supervisor Core API client for the I.T.S.S. Add-on."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import httpx

from .const import KST

_LOGGER = logging.getLogger(__name__)

_S6_TOKEN_PATHS = (
    "/run/s6/container_environment/SUPERVISOR_TOKEN",
    "/run/s6/container_environment/HASSIO_TOKEN",
)


def _discover_token(explicit_token: str | None = None) -> tuple[str, str]:
    """Locate HA API token from explicit config, env vars, or S6 container env files."""
    if explicit_token and explicit_token.strip():
        return explicit_token.strip(), "config_option"

    for env_key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HA_TOKEN"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val, f"env:{env_key}"

    for path in _S6_TOKEN_PATHS:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    val = f.read().strip()
                if val:
                    return val, f"file:{path}"
            except OSError:
                pass

    return "", "none"


class HomeAssistantClient:
    """Reads entity states and calls climate/notify services via HA Supervisor API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("HA_BASE_URL") or "http://supervisor/core/api").rstrip("/")
        self.token, self.token_source = _discover_token(token)
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
        """Return temperature sensors, all sensors, and climate entities from HA."""
        if not self.is_configured:
            return {"tempSensors": [], "sensors": [], "climates": []}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.base_url}/states", headers=self._headers())
                resp.raise_for_status()
                items = resp.json()
        except Exception as err:  # noqa: BLE001
            self.last_error = f"HA states list error: {err}"
            return {"tempSensors": [], "sensors": [], "climates": []}

        temp_sensors: list[dict[str, Any]] = []
        sensors: list[dict[str, Any]] = []
        climates: list[dict[str, Any]] = []
        for it in items:
            eid = str(it.get("entity_id", ""))
            attrs = it.get("attributes") or {}
            unit = str(attrs.get("unit_of_measurement", ""))
            dev_cls = str(attrs.get("device_class", ""))
            entry = {
                "entityId": eid,
                "name": attrs.get("friendly_name", eid),
                "state": it.get("state"),
                "unit": unit,
                "deviceClass": dev_cls,
            }
            if eid.startswith("sensor."):
                sensors.append(entry)
                if dev_cls == "temperature" or "°C" in unit or "temp" in eid.lower():
                    if "humidity" not in eid.lower():
                        temp_sensors.append(entry)
            elif eid.startswith("climate."):
                climates.append(entry)

        temp_sensors.sort(key=lambda x: x["entityId"])
        sensors.sort(key=lambda x: x["entityId"])
        climates.sort(key=lambda x: x["entityId"])
        return {"tempSensors": temp_sensors, "sensors": sensors, "climates": climates}

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Invoke a Home Assistant service (e.g., climate.set_temperature, notify.*)."""
        if not self.is_configured:
            self.last_error = f"HA not configured (no token) when calling {domain}.{service}"
            return
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self.base_url}/services/{domain}/{service}",
                    headers=self._headers(),
                    json=data,
                )
                if resp.status_code >= 400:
                    self.last_error = f"HA service {domain}.{service} failed (HTTP {resp.status_code}): {resp.text[:200]}"
                    _LOGGER.error("%s", self.last_error)
                resp.raise_for_status()
                self.last_error = None
        except Exception as err:  # noqa: BLE001
            if not self.last_error:
                self.last_error = f"HA service {domain}.{service} error: {err}"
            _LOGGER.error("%s", self.last_error)
            raise
