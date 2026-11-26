"""ITSS-P200 daily diagnostic analyzer powered by Gemini API (advisory only)."""

from __future__ import annotations

import json
from collections import Counter
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
        # 스케줄/프리셋 보정이 반영된 실제 목표가 있으면 그것을 기준으로 달성률 계산
        tt = c.get("effectiveTargetTemp") if c.get("effectiveTargetTemp") is not None else c.get("targetTemp")
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


# 일일 리포트에서 '주요 판단 사유' 집계 제외 (정상 대기/유지)
_ROUTINE_REASONS = {
    "", "None", "P000_OK", "P000_MANUAL_OVERRIDE", "P200_PREDICTIVE_ON", "P200_PREDICTIVE_OFF",
    "HYBRID_PRIMARY_HEAT", "HYBRID_COHEAT_BATCH", "HYBRID_EARLY_CUTOFF", "HYBRID_IDLE",
    "SIMULTANEOUS_WEIGHTED_DEMAND", "SIMULTANEOUS_WEIGHTED_TARGET_REACHED", "SIMULTANEOUS_CONTINUE", "SIMULTANEOUS_IDLE",
    "GROUP_DEMAND_HEAT", "GROUP_CONTINUE", "GROUP_TARGET_REACHED", "GROUP_IDLE",
}


def compute_room_daily_stats(rows: list[dict[str, Any]], *, interval_sec: int = 60) -> dict[str, dict[str, Any]]:
    """방별 24시간 요약: 평균/최저/최고 실내, 실효 목표, 추움/더움 비율, 가동 분, 주요 판단 사유."""
    step_min = max(0.5, interval_sec / 60.0)
    by_room: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_room.setdefault(str(r.get("roomName")), []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for name, rs in sorted(by_room.items()):
        temps = [float(r["roomTemp"]) for r in rs if r.get("roomTemp") is not None]
        devs: list[float] = []
        targets: list[float] = []
        for r in rs:
            tt = r.get("effectiveTargetTemp") if r.get("effectiveTargetTemp") is not None else r.get("targetTemp")
            if tt is None:
                continue
            targets.append(float(tt))
            if r.get("roomTemp") is not None:
                devs.append(float(r["roomTemp"]) - float(tt))
        heat_n = sum(
            1 for r in rs
            if r.get("combustionConfirmed") is True or (r.get("commandAction") == "HEAT" and not r.get("dryRun"))
        )
        reasons = Counter(
            str(r.get("safetyReason")) for r in rs if str(r.get("safetyReason")) not in _ROUTINE_REASONS
        )
        out[name] = {
            "samples": len(rs),
            "avgRoomC": round(sum(temps) / len(temps), 2) if temps else None,
            "minRoomC": round(min(temps), 1) if temps else None,
            "maxRoomC": round(max(temps), 1) if temps else None,
            "avgEffTargetC": round(sum(targets) / len(targets), 2) if targets else None,
            "meanDevC": round(sum(devs) / len(devs), 2) if devs else None,
            "coldPct": round(100.0 * sum(1 for d in devs if d < -0.3) / len(devs), 1) if devs else None,
            "hotPct": round(100.0 * sum(1 for d in devs if d > 0.5) / len(devs), 1) if devs else None,
            "heatingMin": round(heat_n * step_min, 1),
            "topReasons": reasons.most_common(3),
        }
    return out


def _fmt(v: Any, nd: int = 1, unit: str = "") -> str:
    if v is None:
        return "--"
    try:
        return f"{float(v):.{nd}f}{unit}"
    except (TypeError, ValueError):
        return str(v)


def format_daily_report_text(ctx: dict[str, Any], ai_text: str | None) -> str:
    """결정론적 24시간 요약 + (있으면) Gemini 분석을 알림용 일반 텍스트로 구성."""
    k = ctx.get("kpis") or {}
    b = ctx.get("boiler") or {}
    p_start = str(ctx.get("periodStart"))[5:16].replace("T", " ")
    p_end = str(ctx.get("periodEnd"))[5:16].replace("T", " ")
    lines = [
        f"기간 {p_start} ~ {p_end} (기록 {_fmt(ctx.get('coverageHours'))}시간)",
        f"🔥 보일러 가동 {_fmt(b.get('heatingMinutes'), 0)}분 ({_fmt(b.get('dutyCyclePct'))}%)"
        f" · 단펄스 {b.get('shortCycles', 0)}회 · 가스비 약 {int(b.get('estimatedGasKrw') or 0):,}원",
        f"🎯 목표 ±0.15°C 유지 {_fmt(k.get('bandCompliancePct'))}% · 최대 초과 {_fmt(k.get('maxOvershootC'), 2)}°C"
        f" · 안전 개입 {k.get('safetyInterventions', 0)}회",
        "🏠 방별 평균 실내 / 실효 목표 / 추움(목표-0.3°C 미만) 비율 / 가동",
    ]
    for name, r in (ctx.get("rooms") or {}).items():
        lines.append(
            f"- {name}: {_fmt(r.get('avgRoomC'))}°C / {_fmt(r.get('avgEffTargetC'))}°C"
            f" / {_fmt(r.get('coldPct'), 0)}% / {_fmt(r.get('heatingMin'), 0)}분"
        )
    fr, orng = ctx.get("floorRangeC"), ctx.get("outdoorRangeC")
    if fr or orng:
        lines.append(
            "바닥 " + (f"{fr[0]}~{fr[1]}°C" if fr else "--") + " · 외기 " + (f"{orng[0]}~{orng[1]}°C" if orng else "--")
        )
    reqs = ctx.get("userHeatRequests") or []
    if reqs:
        lines.append(
            f"🙋 난방 요청 {len(reqs)}회: "
            + ", ".join(
                f"{q.get('at')} {q.get('room')}({q.get('source')}, 실내 {_fmt(q.get('roomTemp'))}°C)" for q in reqs[:6]
            )
            + (" 외" if len(reqs) > 6 else "")
        )
    sugg = list(ctx.get("comfortSuggestions") or [])
    if sugg:
        lines.append("💡 쾌적 학습 제안 (UI 에서 승인해야 적용)")
        for sg in sugg:
            if sg.get("type") == "TARGET_UP":
                lines.append(f"- {sg['room']} 목표 {_fmt(sg['current'])} → {_fmt(sg['suggested'])}°C (근거 {sg['events']}회)")
            elif sg.get("type") == "FLOOR_MIN":
                lines.append(f"- 바닥 최저 온도 {_fmt(sg['current'])} → {_fmt(sg['suggested'])}°C (근거 {sg['events']}회)")
            else:
                lines.append(f"- {sg.get('room')}: {sg.get('reason')}")
    if ai_text:
        lines.append("🤖 Gemini 분석 (자문, 자동 적용 안 함)")
        lines.append(ai_text.strip())
    else:
        lines.append("(Gemini 분석 없음: API 키 미설정 또는 호출 실패 — 요약만 발송)")
    return "\n".join(lines)


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
        # API 키는 x-goog-api-key 헤더로 전달
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _endpoint(self) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent"

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        """응답 parts 중 사고(thought) 파트를 제외한 텍스트를 이어 붙인다."""
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        text = "".join(str(p.get("text") or "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise ValueError("빈 응답")
        return text.strip()

    @staticmethod
    def _anonymize(ctx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        """방 이름(가족 이름 등 개인정보 가능)을 ROOM_A.. 로 치환한 사본과 역치환 표."""
        names = sorted(
            {*(ctx.get("rooms") or {}).keys(), *(ctx.get("roomConfig") or {}).keys()}, key=len, reverse=True
        )
        alias = {n: f"ROOM_{chr(ord('A') + i)}" for i, n in enumerate(sorted(names))}

        def _sub(text: str) -> str:
            for n in names:
                text = text.replace(n, alias[n])
            return text

        def _walk(v: Any) -> Any:
            if isinstance(v, dict):
                return {(_sub(k) if isinstance(k, str) else k): _walk(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [_walk(x) for x in v]
            if isinstance(v, str):
                return _sub(v)
            return v

        return _walk(ctx), {a: n for n, a in alias.items()}

    def generate_daily_report(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """24시간 요약(ctx)을 근거로 한국어 자문 분석 생성. 제어에는 반영하지 않는다.

        방 이름은 ROOM_A.. 로 익명화해 전송하고, 응답에서 원래 이름으로 되돌린다.
        """
        if not self.api_key:
            return {"report": None, "status": "NO_API_KEY"}
        if not (ctx.get("kpis") or {}).get("totalCycles"):
            return {"report": None, "status": "NO_DATA"}
        ctx, reverse = self._anonymize(ctx)
        prompt = (
            "너는 한국 아파트 온수 바닥난방(방별 온도조절기, 보일러 1대) 제어 시스템 I.T.S.S. 의 자문 엔지니어다.\n"
            "아래 JSON 은 지난 24시간 기록 요약이다. 항목 의미: rooms=방별 통계(coldPct=목표보다 0.3°C 넘게 낮은 시간 비율), "
            "userHeatRequests=사용자가 월패드 또는 수동으로 난방을 켠 기록(추웠다는 신호), "
            "comfortSuggestions=로컬 쾌적 학습이 낸 설정 제안, roomConfig.sensorOffsetC=실내 센서 보정값.\n"
            f"{json.dumps(ctx, ensure_ascii=False)}\n\n"
            "다음 형식으로 한국어 일반 텍스트(마크다운 기호 *, #, ** 금지) 최대 8줄로 답하라.\n"
            "1) 한 줄 총평\n"
            "2) 불편·이상 징후 1~3개 (반드시 JSON 의 수치와 단위를 근거로)\n"
            "3) 설정 조정 제안 1~2개 (comfortSuggestions 와 일치하는지 평가 포함, 사용자가 승인해야 적용됨)\n"
            "데이터에 없는 사실은 추측하지 말고, 근거가 부족하면 '근거 부족'이라고 써라. "
            "방 이름은 ROOM_A 처럼 JSON 표기 그대로 써라."
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        try:
            text = self._extract_text(self._http_post(self._endpoint(), payload))
            # 긴 별칭부터 되돌림 (ROOM_A 가 ROOM_AB 일부를 치환하지 않도록 — 방 26개 이하라 실제로는 1글자)
            for a in sorted(reverse, key=len, reverse=True):
                text = text.replace(a, reverse[a])
            return {"report": text, "status": "OK"}
        except Exception as err:  # noqa: BLE001
            return {"report": None, "status": f"ERROR: {err}"}

    def generate_report(self, cycles: list[dict[str, Any]]) -> dict[str, Any]:
        """Return KPI summary plus optional Gemini Korean diagnostic text."""
        kpis = compute_daily_kpis(cycles)
        if not self.api_key or kpis["totalCycles"] == 0:
            return {"kpis": kpis, "report": None, "status": "KPI_ONLY"}

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model_name}:generateContent"
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

