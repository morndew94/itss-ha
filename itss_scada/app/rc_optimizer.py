"""Adaptive 2-node RC parameter fitting engine for ITSS-P200."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RCObservation:
    """Single time-step observation for RC system identification."""

    dt_sec: float
    floor_temp: float
    room_temp: float
    outdoor_temp: float
    heating_on: bool
    next_floor_temp: float
    next_room_temp: float


def fit_rc_parameters(observations: list[RCObservation]) -> dict[str, float | str]:
    """Estimate 2-node RC parameters using lightweight coordinate search.

    Requires at least 10 valid transitions; otherwise reports insufficient samples.
    """
    if len(observations) < 10:
        return {
            "status": "INSUFFICIENT_SAMPLES",
            "sampleCount": len(observations),
        }

    # Initial physical baseline for Korean hydronic floor heating (Ondol)
    best = {
        "c_floor": 1800.0,
        "c_room": 450.0,
        "r_floor_room": 2.2,
        "r_room_ambient": 12.0,
        "q_heat_kw": 1.8,
    }

    def loss(params: dict[str, float]) -> float:
        err = 0.0
        for obs in observations:
            q_in = params["q_heat_kw"] if obs.heating_on else 0.0
            q_fr = (obs.floor_temp - obs.room_temp) / params["r_floor_room"]
            q_ra = (obs.room_temp - obs.outdoor_temp) / params["r_room_ambient"]
            pred_f = obs.floor_temp + ((q_in - q_fr) * obs.dt_sec) / params["c_floor"]
            pred_r = obs.room_temp + ((q_fr - q_ra) * obs.dt_sec) / params["c_room"]
            err += (pred_f - obs.next_floor_temp) ** 2 + (pred_r - obs.next_room_temp) ** 2
        return err / len(observations)

    best_loss = loss(best)
    # Simple bounded coordinate descent (dependency-free for Alpine containers)
    bounds = {
        "c_floor": (600.0, 5000.0),
        "c_room": (150.0, 2000.0),
        "r_floor_room": (0.5, 8.0),
        "r_room_ambient": (3.0, 40.0),
        "q_heat_kw": (0.5, 6.0),
    }

    for step_scale in (0.2, 0.08, 0.03):
        for key, (lo, hi) in bounds.items():
            for direction in (-1.0, 1.0):
                cand = dict(best)
                cand[key] = max(lo, min(hi, best[key] * (1.0 + direction * step_scale)))
                cand_loss = loss(cand)
                if cand_loss < best_loss:
                    best = cand
                    best_loss = cand_loss

    return {
        "status": "READY",
        "sampleCount": len(observations),
        "mse": round(best_loss, 6),
        **{k: round(v, 3) for k, v in best.items()},
    }

