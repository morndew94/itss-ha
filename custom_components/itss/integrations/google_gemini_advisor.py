"""ITSS-P200 daily diagnostic analyzer powered by Gemini API (advisory only)."""

from __future__ import annotations

import json
from typing import Any, Callable
import urllib.request

from ..const import DEFAULT_CONTROL_BAND_HALF


def compute_daily_kpis(cycles: list[dict[str, Any]], band_half_c: float = DEFAULT_CONTROL_BAND_HALF) -> dict[str, Any]:
    """Compute objective 0.3 degC control KPIs and null-rate audit metrics."""
    total = len(cycles)
    if total == 0:
        return {"totalCycles": 0, "bandCompliancePct": None, "nullTimestampRatePct": None}

    in_band = 0
    valid_temp = 0
    max_overshoot = 0.0
    safety_cutoffs = 0
    null_ts_count = 0

    for c in cycles:
        rt = c.get("roomTemp")
        tt = c.get("targetTemp")
        if rt is not None and tt is not None:
            valid_temp += 1
            err = rt - tt
            if abs(err) <= band_half_c:
                in_band += 1
            if err > max_overshoot:
                max_overshoot = err

        reason = str(c.get("safetyReason") or "")
        if reason.startswith("P000_") and reason != "P000_OK":
            safety_cutoffs += 1

        if not c.get("executionStartedAt") or not c.get("savedAt"):
            null_ts_count += 1

    return {
        "totalCycles": total,
        "bandCompliancePct": round((in_band / valid_temp) * 100.0, 1) if valid_temp else None,
        "maxOvershootC": round(max_overshoot, 2),
        "safetyInterventions": safety_cutoffs,
        "nullTimestampRatePct": round((null_ts_count / total) * 100.0, 1),
    }


class GeminiThermalAdvisor:
    """Generates a Korean diagnostic report from daily KPIs without altering live control."""

    def __init__(
        self,
        api_key: str,
        *,
        model_name: str = "gemini-3.8-flash",
        http_post: Callable[[str, dict], dict] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name
        self._http_post = http_post or self._default_post

    def _default_post(self, url: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def generate_report(self, cycles: list[dict[str, Any]]) -> dict[str, Any]:
        """Return KPI summary plus optional Gemini Korean diagnostic text."""
        kpis = compute_daily_kpis(cycles)
        if not self.api_key or kpis["totalCycles"] == 0:
            return {"kpis": kpis, "report": None, "status": "KPI_ONLY"}

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model_name}:generateContent?key={self.api_key}"
        )
        prompt = (
            "다음은 I.T.S.S. 0.3°C 바닥 난방 제어 시스템의 일일 KPI 지표입니다.\n"
            f"{json.dumps(kpis, ensure_ascii=False)}\n"
            "오버슈트 원인, 안전 개입 빈도, 누락 로그 개선 관점에서 3줄 이내로 간결한 한국어 진단 요약을 작성해 주세요."
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}

        try:
            data = self._http_post(url, payload)
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return {"kpis": kpis, "report": text, "status": "OK"}
        except Exception as err:  # noqa: BLE001
            return {"kpis": kpis, "report": None, "status": f"ERROR: {err}"}
