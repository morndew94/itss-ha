"""DataUpdateCoordinator orchestrating ITSS P000-P400 protocols per room."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_CLIMATE_ENTITY,
    CONF_FLOOR_TEMP_ENTITY,
    CONF_HUMIDITY_ENTITY,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PREDICTOR_MODEL,
    CONF_ROOM_NAME,
    CONF_ROOM_TEMP_ENTITY,
    CONF_TARGET_TEMP,
    CONF_UPDATE_INTERVAL_SEC,
    DEFAULT_DB_FILENAME,
    DEFAULT_TARGET_TEMP,
    DEFAULT_UPDATE_INTERVAL_SEC,
    DOMAIN,
    KST,
    MODEL_ADAPTIVE_RC,
)
from .models.schema import CycleRecord, SensorSnapshot, ThermalInput, now_kst_iso
from .protocols.p000_sentinel import SentinelProtocol
from .protocols.p100_core import compute_opposing_interval, round_thermostat_setpoint
from .protocols.p200_therm import AdaptiveRCModel, LinearExtrapolationV12, ThermalPredictor
from .protocols.p300_atmos import AtmosProtocol
from .protocols.p400_circadia import CircadiaProtocol
from .storage.db import ScadaDatabase

_LOGGER = logging.getLogger(__name__)


class ItssCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Periodic control loop coordinator for a single I.T.S.S. room."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.room_name: str = entry.data[CONF_ROOM_NAME]
        interval = int(entry.data.get(CONF_UPDATE_INTERVAL_SEC, DEFAULT_UPDATE_INTERVAL_SEC))

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.room_name}",
            update_interval=timedelta(seconds=interval),
        )

        self.sentinel = SentinelProtocol()
        self.atmos = AtmosProtocol()
        self.circadia = CircadiaProtocol()
        self.predictor: ThermalPredictor = (
            AdaptiveRCModel()
            if entry.data.get(CONF_PREDICTOR_MODEL) == MODEL_ADAPTIVE_RC
            else LinearExtrapolationV12()
        )
        self.db = ScadaDatabase(hass.config.path(DEFAULT_DB_FILENAME))

        self._prev_room_temp: float | None = None
        self._prev_floor_temp: float | None = None
        self._prev_sample_time: datetime | None = None
        self._last_command_action: str | None = None
        self._last_command_time: datetime | None = None

    def _read_float(self, entity_id: str | None) -> tuple[float | None, str | None, float | None]:
        """Read numeric state, KST last_updated timestamp, and age in seconds."""
        if not entity_id:
            return None, None, None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None, None, None
        try:
            value = round(float(state.state), 1)
        except ValueError:
            return None, None, None
        updated_kst = state.last_updated.astimezone(KST)
        age_sec = (datetime.now(tz=KST) - updated_kst).total_seconds()
        return value, updated_kst.isoformat(timespec="seconds"), round(age_sec, 1)

    async def _async_update_data(self) -> dict[str, Any]:
        """Execute one full P000 -> P100 -> P200 -> P300 -> P400 cycle."""
        now = datetime.now(tz=KST)
        scheduled_at = now.isoformat(timespec="seconds")
        execution_started_at = now_kst_iso()
        cycle_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{self.room_name}"

        room_temp, room_ts, room_age = self._read_float(self.entry.data.get(CONF_ROOM_TEMP_ENTITY))
        floor_temp, floor_ts, floor_age = self._read_float(self.entry.data.get(CONF_FLOOR_TEMP_ENTITY))
        humidity, _, _ = self._read_float(self.entry.data.get(CONF_HUMIDITY_ENTITY))
        outdoor_temp, _, _ = self._read_float(self.entry.data.get(CONF_OUTDOOR_TEMP_ENTITY))

        climate_id = self.entry.data.get(CONF_CLIMATE_ENTITY)
        climate_state = self.hass.states.get(climate_id) if climate_id else None
        hvac_mode = climate_state.state if climate_state else None
        hvac_action = climate_state.attributes.get("hvac_action") if climate_state else None
        is_heating = hvac_mode == "heat"
        combustion_confirmed = (hvac_action == "heating") if hvac_action is not None else None

        # Compute short-term temperature derivatives (degC/min)
        v_room, v_floor = 0.0, 0.0
        if (
            self._prev_sample_time is not None
            and room_temp is not None
            and floor_temp is not None
            and self._prev_room_temp is not None
            and self._prev_floor_temp is not None
        ):
            dt_min = max((now - self._prev_sample_time).total_seconds() / 60.0, 1e-3)
            v_room = (room_temp - self._prev_room_temp) / dt_min
            v_floor = (floor_temp - self._prev_floor_temp) / dt_min

        self._prev_room_temp = room_temp
        self._prev_floor_temp = floor_temp
        self._prev_sample_time = now

        snapshot = SensorSnapshot(
            room_name=self.room_name,
            room_temp=room_temp,
            floor_temp=floor_temp,
            humidity=humidity,
            outdoor_temp=outdoor_temp,
            thermostat_hvac_mode=hvac_mode,
            combustion_confirmed=combustion_confirmed,
            sensor_measured_at_room=room_ts,
            sensor_measured_at_floor=floor_ts,
            room_sensor_age_sec=room_age,
            floor_sensor_age_sec=floor_age,
        )

        sec_since_switch = (
            (now - self._last_command_time).total_seconds() if self._last_command_time else None
        )
        verdict = self.sentinel.evaluate(
            snapshot,
            is_heating=is_heating,
            v_room_per_min=v_room,
            seconds_since_last_switch=sec_since_switch,
        )

        target_temp = float(self.entry.data.get(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP))
        record = CycleRecord(
            cycle_id=cycle_id,
            room_name=self.room_name,
            scheduled_at=scheduled_at,
            execution_started_at=execution_started_at,
            sensor_measured_at_room=room_ts,
            sensor_measured_at_floor=floor_ts,
            room_temp=room_temp,
            floor_temp=floor_temp,
            target_temp=target_temp,
            safety_reason=verdict.reason_code,
            device_mode_event=hvac_mode,
            combustion_confirmed=combustion_confirmed,
        )

        if verdict.force_off:
            record.command_action = "OFF"
            record.execution_status = "SKIPPED_SAFETY"
        elif verdict.hold_current_state or room_temp is None or floor_temp is None:
            record.command_action = "HOLD"
            record.execution_status = "SKIPPED_SAFETY"
        else:
            atmos_offset = self.atmos.compute_offset(outdoor_temp=outdoor_temp, now_kst=now)
            circadia_offset = self.circadia.compute_offset(now_kst=now)
            prediction = self.predictor.predict(
                ThermalInput(
                    room_temp=room_temp,
                    floor_temp=floor_temp,
                    target_temp=target_temp,
                    is_heating=is_heating,
                    v_floor_per_min=v_floor,
                    v_room_per_min=v_room,
                    outdoor_temp=outdoor_temp,
                    atmos_loss_offset_c=atmos_offset,
                    circadia_target_offset_c=circadia_offset,
                )
            )
            record.predictor_model = prediction.model_name
            record.predicted_room_30m = prediction.predicted_room_temp
            record.predicted_floor_30m = prediction.predicted_floor_temp
            record.residual_learning_status = prediction.residual_learning_status
            record.command_action = prediction.recommended_action

        if record.command_action in ("HEAT", "OFF"):
            record.command_setpoint = round_thermostat_setpoint(
                target_temp, action=record.command_action
            )
            record.opposing_command_interval_sec = compute_opposing_interval(
                self._last_command_action, record.command_action, sec_since_switch
            )
            record.control_issued_at = now_kst_iso()
            # Re-read state timestamp placeholder; real climate service call hooks here
            record.control_confirmed_at = now_kst_iso()
            record.observed_at = now_kst_iso()
            self._last_command_action = record.command_action
            self._last_command_time = now

        await self._async_save_cycle(record)
        return record.to_payload()

    async def _async_save_cycle(self, record: CycleRecord) -> None:
        """Persist cycle audit record directly to the local SQLite database."""
        record.saved_at = now_kst_iso()
        try:
            await self.hass.async_add_executor_job(self.db.insert_cycle, record.to_payload())
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to save I.T.S.S. cycle record: %s", err)

