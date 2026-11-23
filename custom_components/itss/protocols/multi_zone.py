"""Multi-zone heating coordination with a single shared floor temperature sensor.

Supports two control strategies:
  1. "hybrid" (recommended when each room has its own valve/climate entity):
     - Synchronized start: co-heat rooms needing heat together to prevent boiler short-cycling.
     - Individual early cutoff: close valves individually as each room reaches its upper band.
     - Single floor sensor sharing: master zone uses measured floor temp; slave zones use
       estimated floor temp (master + calibrated offset) with explicit source tagging.
  2. "simultaneous" (for single-thermostat or coupled-valve layouts):
     - Drives all zones together using weighted indoor temperature while enforcing
       P000 overheat protection across every room's indoor sensor.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ZoneReading:
    """Current telemetry for one room in a multi-zone home."""

    room_name: str
    room_temp: float
    target_temp: float
    is_heating: bool
    has_floor_sensor: bool = False
    floor_offset_from_master_c: float = 0.0
    weight: float = 1.0


@dataclass(slots=True)
class ZoneCommand:
    """Computed action and virtual floor telemetry for one room."""

    room_name: str
    action: str  # "HEAT", "OFF", "HOLD"
    effective_floor_temp: float
    floor_sensor_source: str  # "MEASURED" or "ESTIMATED"
    reason: str


@dataclass(slots=True)
class MultiZonePlan:
    """Coordinated multi-zone heating decision."""

    strategy: str
    boiler_demand: bool
    master_floor_temp: float
    commands: list[ZoneCommand] = field(default_factory=list)


class MultiZoneController:
    """Coordinates living room and bedrooms with a single physical floor sensor."""

    def __init__(
        self,
        *,
        strategy: str = "hybrid",
        band_half_c: float = 0.15,
        overheat_room_c: float = 27.0,
        overheat_floor_c: float = 29.0,
        general_off_floor_c: float = 28.0,
        coheat_margin_c: float = 0.05,
    ) -> None:
        if strategy not in ("hybrid", "simultaneous"):
            raise ValueError(f"Unsupported strategy: {strategy}")
        self.strategy = strategy
        self.band_half_c = band_half_c
        self.overheat_room_c = overheat_room_c
        self.overheat_floor_c = overheat_floor_c
        self.general_off_floor_c = general_off_floor_c
        self.coheat_margin_c = coheat_margin_c

    def _estimate_floor(self, zone: ZoneReading, master_floor_temp: float) -> tuple[float, str]:
        """Return (floor_temp, source) using real sensor for master and offset for slaves."""
        if zone.has_floor_sensor:
            return round(master_floor_temp, 1), "MEASURED"
        return round(master_floor_temp + zone.floor_offset_from_master_c, 1), "ESTIMATED"

    def evaluate(
        self,
        zones: list[ZoneReading],
        *,
        master_floor_temp: float,
    ) -> MultiZonePlan:
        """Compute coordinated commands for all zones."""
        if not zones:
            return MultiZonePlan(self.strategy, False, master_floor_temp, [])

        # Hard P000 floor limit applies globally when there is only one floor sensor
        floor_cutoff = (
            master_floor_temp >= self.overheat_floor_c
            or master_floor_temp >= self.general_off_floor_c
        )

        if self.strategy == "simultaneous":
            return self._evaluate_simultaneous(zones, master_floor_temp, floor_cutoff)
        return self._evaluate_hybrid(zones, master_floor_temp, floor_cutoff)

    def _evaluate_simultaneous(
        self,
        zones: list[ZoneReading],
        master_floor_temp: float,
        floor_cutoff: bool,
    ) -> MultiZonePlan:
        """All rooms switch ON/OFF together with global overheat guard."""
        any_overheat = any(z.room_temp >= self.overheat_room_c for z in zones)
        total_w = sum(max(z.weight, 0.01) for z in zones)
        weighted_error = sum((z.room_temp - z.target_temp) * max(z.weight, 0.01) for z in zones) / total_w
        any_heating = any(z.is_heating for z in zones)

        if floor_cutoff or any_overheat:
            action = "OFF"
            reason = "P000_GLOBAL_OVERHEAT_OR_FLOOR_LIMIT"
        elif any_heating:
            action = "OFF" if weighted_error >= self.band_half_c else "HEAT"
            reason = "SIMULTANEOUS_WEIGHTED_TARGET_REACHED" if action == "OFF" else "SIMULTANEOUS_CONTINUE"
        else:
            action = "HEAT" if weighted_error <= -self.band_half_c else "OFF"
            reason = "SIMULTANEOUS_WEIGHTED_DEMAND" if action == "HEAT" else "SIMULTANEOUS_IDLE"

        commands = []
        for z in zones:
            f_temp, f_src = self._estimate_floor(z, master_floor_temp)
            commands.append(ZoneCommand(z.room_name, action, f_temp, f_src, reason))

        return MultiZonePlan(
            strategy="simultaneous",
            boiler_demand=(action == "HEAT"),
            master_floor_temp=master_floor_temp,
            commands=commands,
        )

    def _evaluate_hybrid(
        self,
        zones: list[ZoneReading],
        master_floor_temp: float,
        floor_cutoff: bool,
    ) -> MultiZonePlan:
        """Synchronized start (co-heating batch) + individual early valve cutoff."""
        # Step 1: Determine if any room strictly triggers a heating cycle
        primary_demand = (not floor_cutoff) and any(
            (z.room_temp <= z.target_temp - self.band_half_c) and (z.room_temp < self.overheat_room_c)
            for z in zones
        )
        already_active = (not floor_cutoff) and any(
            z.is_heating and z.room_temp < (z.target_temp + self.band_half_c)
            and z.room_temp < self.overheat_room_c
            for z in zones
        )
        boiler_cycle_active = primary_demand or already_active

        commands: list[ZoneCommand] = []
        for z in zones:
            f_temp, f_src = self._estimate_floor(z, master_floor_temp)

            # Room-level or global safety cutoff
            if floor_cutoff or f_temp >= self.general_off_floor_c or z.room_temp >= self.overheat_room_c:
                commands.append(ZoneCommand(z.room_name, "OFF", f_temp, f_src, "P000_SAFETY_CUTOFF"))
                continue

            upper = z.target_temp + self.band_half_c
            lower = z.target_temp - self.band_half_c

            if z.room_temp >= upper:
                # Individual early cutoff to prevent room overshoot
                commands.append(ZoneCommand(z.room_name, "OFF", f_temp, f_src, "HYBRID_EARLY_CUTOFF"))
            elif boiler_cycle_active and (z.room_temp <= z.target_temp - self.coheat_margin_c or z.is_heating):
                # Co-heat together while boiler is running to avoid boiler short-cycling
                reason = "HYBRID_PRIMARY_HEAT" if z.room_temp <= lower else "HYBRID_COHEAT_BATCH"
                commands.append(ZoneCommand(z.room_name, "HEAT", f_temp, f_src, reason))
            else:
                commands.append(ZoneCommand(z.room_name, "OFF", f_temp, f_src, "HYBRID_IDLE"))

        boiler_demand = any(c.action == "HEAT" for c in commands)
        return MultiZonePlan(
            strategy="hybrid",
            boiler_demand=boiler_demand,
            master_floor_temp=master_floor_temp,
            commands=commands,
        )

