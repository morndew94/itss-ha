"""Diagnostic sensors exposing I.T.S.S. protocol telemetry in Home Assistant."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ItssCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register I.T.S.S. diagnostic sensors for a configured room."""
    coordinator: ItssCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ItssDiagnosticSensor(coordinator, "commandAction", "Command Action"),
            ItssDiagnosticSensor(coordinator, "safetyReason", "Sentinel Status"),
            ItssDiagnosticSensor(coordinator, "predictedRoom30m", "Predicted Room 30m", "°C"),
            ItssDiagnosticSensor(coordinator, "predictedFloor30m", "Predicted Floor 30m", "°C"),
        ]
    )


class ItssDiagnosticSensor(CoordinatorEntity[ItssCoordinator], SensorEntity):
    """Single telemetry sensor backed by the room ItssCoordinator."""

    def __init__(
        self,
        coordinator: ItssCoordinator,
        payload_key: str,
        label: str,
        unit: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._payload_key = payload_key
        self._attr_name = f"ITSS {coordinator.room_name} {label}"
        self._attr_unique_id = f"itss_{coordinator.entry.entry_id}_{payload_key}"
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self):
        """Return latest value from the coordinator payload."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._payload_key)

    @property
    def extra_state_attributes(self):
        """Expose full KST timestamp and protocol audit metadata."""
        return self.coordinator.data or {}

