"""Adaptive 2-node RC parameter fitting engine for ITSS-P200."""

from __future__ import annotations

import math
from dataclasses import dataclass

# 피팅에 필요한 최소 전이(관측 쌍) 수
MIN_OBSERVATIONS = 30
# 패턴 탐색 반복 상한 (관측 2,880개 기준 수 초 이내)
MAX_ITERATIONS = 120


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


_BASELINE = {
    "c_floor": 1800.0,
    "c_room": 450.0,
    "r_floor_room": 2.2,
    "r_room_ambient": 12.0,
    "q_heat_kw": 1.8,
}

_BOUNDS = {
    "c_floor": (600.0, 5000.0),
    "c_room": (150.0, 2000.0),
    "r_floor_room": (0.5, 8.0),
    "r_room_ambient": (3.0, 40.0),
    "q_heat_kw": (0.5, 6.0),
}


def _loss(params: dict[str, float], observations: list[RCObservation]) -> float:
    """1-step 예측 MSE (바닥 + 실내)."""
    err = 0.0
    for obs in observations:
        q_in = params["q_heat_kw"] if obs.heating_on else 0.0
        q_fr = (obs.floor_temp - obs.room_temp) / params["r_floor_room"]
        q_ra = (obs.room_temp - obs.outdoor_temp) / params["r_room_ambient"]
        pred_f = obs.floor_temp + ((q_in - q_fr) * obs.dt_sec) / params["c_floor"]
        pred_r = obs.room_temp + ((q_fr - q_ra) * obs.dt_sec) / params["c_room"]
        err += (pred_f - obs.next_floor_temp) ** 2 + (pred_r - obs.next_room_temp) ** 2
    return err / len(observations)


def fit_rc_parameters(observations: list[RCObservation]) -> dict[str, float | str]:
    """Estimate 2-node RC parameters using bounded multiplicative pattern search.

    v2.7.0: 파라미터마다 1스텝만 시도하던 방식을, 개선이 없을 때 스텝을 절반으로 줄이며
    수렴할 때까지 반복하는 패턴 탐색으로 변경. 최소 30개 전이 필요.
    """
    if len(observations) < MIN_OBSERVATIONS:
        return {
            "status": "INSUFFICIENT_SAMPLES",
            "sampleCount": len(observations),
            "minRequired": MIN_OBSERVATIONS,
        }

    best = dict(_BASELINE)
    baseline_loss = _loss(best, observations)
    best_loss = baseline_loss
    step = 0.25
    iterations = 0
    while step >= 0.004 and iterations < MAX_ITERATIONS:
        iterations += 1
        improved = False
        for key, (lo, hi) in _BOUNDS.items():
            for direction in (-1.0, 1.0):
                cand = dict(best)
                cand[key] = max(lo, min(hi, best[key] * (1.0 + direction * step)))
                if cand[key] == best[key]:
                    continue
                cand_loss = _loss(cand, observations)
                if cand_loss < best_loss:
                    best = cand
                    best_loss = cand_loss
                    improved = True
        if not improved:
            step *= 0.5

    heat_samples = sum(1 for o in observations if o.heating_on)
    return {
        "status": "READY",
        "sampleCount": len(observations),
        "heatingSamples": heat_samples,
        "iterations": iterations,
        "mse": round(best_loss, 6),
        "rmseC": round(math.sqrt(best_loss / 2.0), 4),
        "baselineMse": round(baseline_loss, 6),
        **{k: round(v, 3) for k, v in best.items()},
    }
