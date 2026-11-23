"""Home Assistant Supervisor Core API client for the I.T.S.S. Add-on."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx

from .const import KST


class HomeAssistantClient:
    """Reads entity states and calls climate services via HA Supervisor API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("HA_BASE_URL") or "http://supervisor/core/api").rstrip("/")
        self.token = token or os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN") or ""

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
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.base_url}/states/{entity_id}", headers=self._headers())
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

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

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """Invoke a Home Assistant service (e.g., climate.set_hvac_mode)."""
        if not self.is_configured:
            return
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/services/{domain}/{service}",
                headers=self._headers(),
                json=data,
            )
            resp.raise_for_status()

