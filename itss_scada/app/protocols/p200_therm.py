"""ITSS-P200 THERM: Thermodynamic response and predictive modeling protocol.

Provides a unified ThermalPredictor interface so the system can smoothly
transition from v1.2 linear extrapolation (F30 / Room30) to an adaptive
2-node RC thermodynamic model (C_f, C_r, R_fr, R_ra) without changing P000/P100.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..const import DEFAULT_CONTROL_BAND_HALF, MODEL_ADAPTIVE_RC, MODEL_LINEAR_V12
from ..models.schema import ThermalInput, ThermalPrediction


@dataclass(slots=True)
class ResidualHeatStats:
    """Tracked post-shutoff residual heat rise (R/P) statistics."""

    sample_count: int = 0
    mean_rise_c: float = 0.0
    min_samples_required: int = 2

    def status(self, is_transient: bool = False) -> str:
        """Return explicit learning readiness state."""
        if is_transient:
            return "TRANSIENT_SKIP"
        if self.sample_count < self.min_samples_required:
            return "INSUFFICIENT_SAMPLES"
        if self.sample_count < 5:
            return "LEARNING"
        return "READY"


class ThermalPredictor(ABC):
    """Abstract interface for ITSS-P200 thermal prediction engines."""

    model_name: str

    @abstractmethod
    def predict(
        self,
        data: ThermalInput,
        residual_stats: ResidualHeatStats | None = None,
    ) -> ThermalPrediction:
        """Compute predicted temperatures and recommended heating action."""


class LinearExtrapolationV12(ThermalPredictor):
    """ITSS-P200 v1.2 baseline: 30-minute linear extrapolation (F30 / Room30)."""

    model_name = MODEL_LINEAR_V12

    def __init__(self, band_half_c: float = DEFAULT_CONTROL_BAND_HALF) -> None:
        self.band_half_c = band_half_c

    def predict(
        self,
        data: ThermalInput,
        residual_stats: ResidualHeatStats | None = None,
    ) -> ThermalPrediction:
        stats = residual_stats or ResidualHeatStats()
        # Detect sharp slope transients where linear extrapolation is unreliable
        is_transient = abs(data.v_room_per_min) > 0.08 or abs(data.v_floor_per_min) > 0.20
        learning_status = stats.status(is_transient=is_transient)

        effective_target = data.target_temp + data.circadia_target_offset_c + data.atmos_loss_offset_c
        f_pred = data.floor_temp + data.v_floor_per_min * data.horizon_min
        r_pred = data.room_temp + data.v_room_per_min * data.horizon_min

        # Apply learned residual rise when learning state is LEARNING or READY
        use_residual = learning_status in ("LEARNING", "READY")
        residual_rise = stats.mean_rise_c if use_residual else 0.0
        upper_bound = effective_target + self.band_half_c
        lower_bound = effective_target - self.band_half_c

        # Estimate radiant heat gain still stored in the warm floor slab
        slab_surplus_c = max(0.0, data.floor_temp - effective_target)
        # Floor above target + 3.5C (or projected floor surge) signals enough stored slab heat
        floor_already_charged = (f_pred >= effective_target + 4.0 and data.v_floor_per_min > 0.03)

        if data.is_heating:
            # Predictive OFF when projected room temp + learned residual reaches upper band,
            # OR when rapidly rising floor slab is already overcharged relative to target
            action = "OFF" if ((r_pred + residual_rise) >= upper_bound or floor_already_charged) else "HEAT"
        else:
            # Predictive ON when projected room temp falls below lower band,
            # unless slab is actively heating up and already warmer than target
            if r_pred <= lower_bound and not (slab_surplus_c >= 0.8 and data.v_floor_per_min >= 0.03):
                action = "HEAT"
            else:
                action = "OFF"

        return ThermalPrediction(
            model_name=self.model_name,
            recommended_action=action,
            predicted_room_temp=round(r_pred, 2),
            predicted_floor_temp=round(f_pred, 2),
            effective_target_temp=round(effective_target, 2),
            residual_learning_status=learning_status,
            estimated_residual_rise_c=round(residual_rise, 2) if use_residual else None,
            diagnostics={
                "v_room_per_min": data.v_room_per_min,
                "v_floor_per_min": data.v_floor_per_min,
                "horizon_min": data.horizon_min,
            },
        )


@dataclass(slots=True)
class RCParameters:
    """2-node floor/room RC thermodynamic parameters.

    Units:
      c_floor, c_room: effective thermal capacitance (kJ/K)
      r_floor_room: thermal resistance between floor slab and room air (K/kW)
      r_room_ambient: thermal resistance between room air and outdoor air (K/kW)
      q_heat_kw: effective hydronic heating power injected into floor slab (kW)
    """

    c_floor: float = 1800.0
    c_room: float = 450.0
    r_floor_room: float = 2.2
    r_room_ambient: float = 12.0
    q_heat_kw: float = 1.8


class AdaptiveRCModel(ThermalPredictor):
    """ITSS-P200 next-gen: 2-node RC thermodynamic trajectory predictor."""

    model_name = MODEL_ADAPTIVE_RC

    def __init__(
        self,
        params: RCParameters | None = None,
        band_half_c: float = DEFAULT_CONTROL_BAND_HALF,
        dt_sec: float = 60.0,
    ) -> None:
        self.params = params or RCParameters()
        self.band_half_c = band_half_c
        self.dt_sec = dt_sec

    def simulate(
        self,
        t_floor: float,
        t_room: float,
        t_ambient: float,
        *,
        heating_on: bool,
        horizon_min: int,
    ) -> tuple[float, float]:
        """Forward-integrate the 2-node RC state equations over horizon_min."""
        p = self.params
        q_in = p.q_heat_kw if heating_on else 0.0
        steps = max(1, int((horizon_min * 60) / self.dt_sec))

        tf, tr = t_floor, t_room
        for _ in range(steps):
            q_fr = (tf - tr) / p.r_floor_room
            q_ra = (tr - t_ambient) / p.r_room_ambient
            # dT = (net kW * dt_sec) / C(kJ/K)
            tf += ((q_in - q_fr) * self.dt_sec) / p.c_floor
            tr += ((q_fr - q_ra) * self.dt_sec) / p.c_room
        return tf, tr

    def predict(
        self,
        data: ThermalInput,
        residual_stats: ResidualHeatStats | None = None,
    ) -> ThermalPrediction:
        stats = residual_stats or ResidualHeatStats()
        learning_status = stats.status(is_transient=False)

        # Fallback outdoor temperature when P300 weather sensor is unavailable
        t_ambient = data.outdoor_temp if data.outdoor_temp is not None else 5.0
        effective_target = data.target_temp + data.circadia_target_offset_c + data.atmos_loss_offset_c

        # Simulate coasting trajectory (heating OFF) to quantify floor slab residual heat
        f_coast, r_coast = self.simulate(
            data.floor_temp,
            data.room_temp,
            t_ambient,
            heating_on=False,
            horizon_min=data.horizon_min,
        )
        # Simulate continued heating trajectory
        f_heat, r_heat = self.simulate(
            data.floor_temp,
            data.room_temp,
            t_ambient,
            heating_on=True,
            horizon_min=data.horizon_min,
        )

        upper_bound = effective_target + self.band_half_c
        lower_bound = effective_target - self.band_half_c
        # Combine physics coasting with learned empirical residual rise (take conservative max)
        physics_residual = max(0.0, r_coast - data.room_temp)
        learned_residual = stats.mean_rise_c if learning_status in ("LEARNING", "READY") else 0.0
        residual_rise = max(physics_residual, learned_residual)
        projected_coast = max(r_coast, data.room_temp + learned_residual)

        if data.is_heating:
            # Cut off early if coasting or learned residual reaches target band
            if projected_coast >= effective_target or r_heat >= upper_bound:
                action = "OFF"
                pred_f, pred_r = f_coast, projected_coast
            else:
                action = "HEAT"
                pred_f, pred_r = f_heat, r_heat
        else:
            if projected_coast <= lower_bound:
                action = "HEAT"
                pred_f, pred_r = f_heat, r_heat
            else:
                action = "OFF"
                pred_f, pred_r = f_coast, projected_coast

        return ThermalPrediction(
            model_name=self.model_name,
            recommended_action=action,
            predicted_room_temp=round(pred_r, 2),
            predicted_floor_temp=round(pred_f, 2),
            effective_target_temp=round(effective_target, 2),
            residual_learning_status=learning_status,
            estimated_residual_rise_c=round(residual_rise, 2),
            diagnostics={
                "r_coast_pred": round(r_coast, 2),
                "r_heat_pred": round(r_heat, 2),
                "t_ambient_used": t_ambient,
            },
        )

