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

v2.9.0:
  - 바닥 기준 없는 독립 방(floor_estimate_enabled=False)은 바닥값을 갖지 않으며(source "NONE")
    마스터 바닥 과열/상한 차단 대상에서 제외한다. (v2.8 PROXY 폐기: 마스터 바닥은 다른 방
    가열에 반응하지 않음 — 2026-11-25 01:56~04:22 거실·방2 4회 가열 중 마스터 바닥 계속 하강)
  - evaluate_floor_group(): 같은 바닥 기준(실측 방 + 추정 방)을 공유하는 방들을 한 묶음으로
    동시 ON/OFF 하기 위한 가중 평균 편차 판정.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..const import (
    DEFAULT_CONTROL_BAND_HALF,
    DEFAULT_GENERAL_OFF_FLOOR_C,
    DEFAULT_OVERHEAT_FLOOR_C,
    DEFAULT_OVERHEAT_ROOM_C,
)


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
    floor_estimate_enabled: bool = True  # False: 바닥 기준 없는 독립 방 (바닥 판정 제외)

    @property
    def has_floor_reference(self) -> bool:
        return self.has_floor_sensor or self.floor_estimate_enabled


@dataclass(slots=True)
class ZoneCommand:
    """Computed action and virtual floor telemetry for one room."""

    room_name: str
    action: str  # "HEAT", "OFF", "HOLD"
    effective_floor_temp: float | None
    floor_sensor_source: str  # "MEASURED", "ESTIMATED" or "NONE"
    reason: str


@dataclass(slots=True)
class MultiZonePlan:
    """Coordinated multi-zone heating decision."""

    strategy: str
    boiler_demand: bool
    master_floor_temp: float | None
    commands: list[ZoneCommand] = field(default_factory=list)


@dataclass(slots=True)
class GroupMember:
    """바닥 기준을 공유하는 동시 제어 그룹의 구성 방."""

    room_name: str
    room_temp: float | None
    target_temp: float  # 실효 목표 (스케줄/프리셋/외기 보정 포함)
    weight: float = 1.0
    projected_coast_c: float | None = None  # P200 무가열 예측 실내 (없으면 실내 + 학습 잔열)
    residual_rise_c: float = 0.0


@dataclass(slots=True)
class GroupDecision:
    """동시 제어 그룹 판정 결과."""

    action: str  # "HEAT" | "OFF"
    reason: str
    weighted_dev_c: float | None
    members_used: list[str] = field(default_factory=list)


def evaluate_floor_group(
    members: list[GroupMember],
    *,
    is_heating: bool,
    band_half_c: float = DEFAULT_CONTROL_BAND_HALF,
) -> GroupDecision:
    """가중 평균 예측 편차로 그룹 동시 ON/OFF 판정.

    편차 = Σ w x (예측 실내 - 실효 목표) / Σ w
      - 가열 중 : 편차 >= 0 이면 OFF (목표 도달 예상), 아니면 계속
      - 휴지 중 : 편차 <= -band 이면 HEAT (그룹 기동), 아니면 대기
    바닥 안전(과열/상한/예측 피크/최대 가동/휴지)은 호출 측에서 그룹 공통 바닥으로 별도 판정한다.
    """
    valid = [m for m in members if m.room_temp is not None]
    if not valid:
        return GroupDecision("OFF", "GROUP_NO_ROOM_DATA", None, [])
    total_w = sum(max(m.weight, 0.01) for m in valid)
    dev = 0.0
    for m in valid:
        proj = m.projected_coast_c if m.projected_coast_c is not None else m.room_temp + max(0.0, m.residual_rise_c)
        dev += (proj - m.target_temp) * max(m.weight, 0.01)
    dev = round(dev / total_w, 3)
    names = [m.room_name for m in valid]
    if is_heating:
        if dev >= 0.0:
            return GroupDecision("OFF", "GROUP_TARGET_REACHED", dev, names)
        return GroupDecision("HEAT", "GROUP_CONTINUE", dev, names)
    if dev <= -band_half_c:
        return GroupDecision("HEAT", "GROUP_DEMAND_HEAT", dev, names)
    return GroupDecision("OFF", "GROUP_IDLE", dev, names)


class MultiZoneController:
    """Coordinates living room and bedrooms with a single physical floor sensor."""

    def __init__(
        self,
        *,
        strategy: str = "hybrid",
        band_half_c: float = DEFAULT_CONTROL_BAND_HALF,
        overheat_room_c: float = DEFAULT_OVERHEAT_ROOM_C,
        overheat_floor_c: float = DEFAULT_OVERHEAT_FLOOR_C,
        general_off_floor_c: float = DEFAULT_GENERAL_OFF_FLOOR_C,
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

    def _estimate_floor(self, zone: ZoneReading, master_floor_temp: float | None) -> tuple[float | None, str]:
        """Return (floor_temp, source) using real sensor for master and offset for slaves."""
        if not zone.has_floor_reference:
            return None, "NONE"
        if master_floor_temp is None:
            return None, "MEASURED" if zone.has_floor_sensor else "ESTIMATED"
        if zone.has_floor_sensor:
            return round(master_floor_temp, 1), "MEASURED"
        return round(master_floor_temp + zone.floor_offset_from_master_c, 1), "ESTIMATED"

    def evaluate(
        self,
        zones: list[ZoneReading],
        *,
        master_floor_temp: float | None,
    ) -> MultiZonePlan:
        """Compute coordinated commands for all zones."""
        if not zones:
            return MultiZonePlan(self.strategy, False, master_floor_temp, [])

        # 마스터 바닥 과열/상한 차단은 바닥 기준을 공유하는 방에만 적용 (독립 방 제외)
        floor_cutoff = master_floor_temp is not None and (
            master_floor_temp >= self.overheat_floor_c
            or master_floor_temp >= self.general_off_floor_c
        )

        if self.strategy == "simultaneous":
            return self._evaluate_simultaneous(zones, master_floor_temp, floor_cutoff)
        return self._evaluate_hybrid(zones, master_floor_temp, floor_cutoff)

    def _evaluate_simultaneous(
        self,
        zones: list[ZoneReading],
        master_floor_temp: float | None,
        floor_cutoff: bool,
    ) -> MultiZonePlan:
        """All rooms switch ON/OFF together with global overheat guard."""
        floor_cutoff = floor_cutoff and any(z.has_floor_reference for z in zones)
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
        master_floor_temp: float | None,
        floor_cutoff: bool,
    ) -> MultiZonePlan:
        """Synchronized start (co-heating batch) + individual early valve cutoff."""

        def _floor_blocked(z: ZoneReading) -> bool:
            # 바닥 기준이 있는 방만 마스터 바닥 차단 대상
            return z.has_floor_reference and floor_cutoff

        # Step 1: Determine if any room strictly triggers a heating cycle
        primary_demand = any(
            not _floor_blocked(z)
            and (z.room_temp <= z.target_temp - self.band_half_c) and (z.room_temp < self.overheat_room_c)
            for z in zones
        )
        already_active = any(
            not _floor_blocked(z)
            and z.is_heating and z.room_temp < (z.target_temp + self.band_half_c)
            and z.room_temp < self.overheat_room_c
            for z in zones
        )
        boiler_cycle_active = primary_demand or already_active

        commands: list[ZoneCommand] = []
        for z in zones:
            f_temp, f_src = self._estimate_floor(z, master_floor_temp)

            # Room-level or global safety cutoff
            if (
                _floor_blocked(z)
                or (f_temp is not None and f_temp >= self.general_off_floor_c)
                or z.room_temp >= self.overheat_room_c
            ):
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

