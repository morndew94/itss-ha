"""Google API alerts and reporting helpers (library only, not wired into the control loop).

- GeminiTTSAlert: 치명 P000 이벤트용 한국어 음성 WAV 생성 (Gemini TTS)
- format_cycle_for_sheets: CycleRecord -> Google Sheets 행 변환

v2.7.0 기준 두 기능 모두 제어 루프/알림에 연결되어 있지 않은 보조 라이브러리이다.
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.request
import wave
from typing import Any, Callable

# Gemini TTS 출력 형식: raw PCM s16le, 24 kHz, mono (공식 문서 기준)
TTS_SAMPLE_RATE_HZ = 24000
TTS_SAMPLE_WIDTH_BYTES = 2
TTS_CHANNELS = 1


def pcm_to_wav_bytes(
    pcm: bytes,
    *,
    sample_rate: int = TTS_SAMPLE_RATE_HZ,
    sample_width: int = TTS_SAMPLE_WIDTH_BYTES,
    channels: int = TTS_CHANNELS,
) -> bytes:
    """raw PCM 바이트에 WAV(RIFF) 헤더를 붙인다."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class GeminiTTSAlert:
    """Synthesizes Korean WAV voice warnings for critical P000 SENTINEL events."""

    CRITICAL_REASONS = {
        "P000_OVERHEAT_CUTOFF": "경고. 실내 또는 바닥 과열이 감지되어 난방을 긴급 차단했습니다.",
        "P000_SENSOR_STALE": "경고. 온도 센서 수신이 중단되어 안전을 위해 난방을 중지했습니다.",
        "P000_SENSOR_MISSING": "경고. 온도 센서를 찾을 수 없어 난방을 안전 차단했습니다.",
    }

    def __init__(
        self,
        api_key: str,
        *,
        model_name: str = "gemini-3.1-flash-tts-preview",
        voice_name: str = "Kore",
        http_post: Callable[[str, dict], dict] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name
        self.voice_name = voice_name
        self._http_post = http_post or self._default_post

    def _default_post(self, url: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def synthesize_alert_if_critical(
        self,
        reason_code: str,
        room_name: str,
        output_wav_path: str,
    ) -> str | None:
        """Create a WAV alert file when reason_code is critical; returns path or None."""
        template = self.CRITICAL_REASONS.get(reason_code)
        if not template or not self.api_key:
            return None

        message = f"{room_name} 구역 {template}"
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model_name}:generateContent"
        )
        # TTS 는 responseModalities=AUDIO 와 speechConfig 가 필요
        payload = {
            "contents": [{"parts": [{"text": message}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice_name}}},
            },
        }

        try:
            data = self._http_post(url, payload)
            part = data["candidates"][0]["content"]["parts"][0]
            audio_bytes = base64.b64decode(part["inlineData"]["data"])
            # 응답은 raw PCM 이므로 WAV 헤더를 붙여 저장 (이미 RIFF 면 그대로 저장)
            if not audio_bytes.startswith(b"RIFF"):
                audio_bytes = pcm_to_wav_bytes(audio_bytes)
            parent = os.path.dirname(os.path.abspath(output_wav_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(output_wav_path, "wb") as f:
                f.write(audio_bytes)
            return output_wav_path
        except Exception:  # noqa: BLE001
            return None


def format_cycle_for_sheets(cycle_payload: dict[str, Any]) -> list[Any]:
    """Convert a CycleRecord payload into an ordered Google Sheets row."""
    return [
        cycle_payload.get("cycleId"),
        cycle_payload.get("roomName"),
        cycle_payload.get("executionStartedAt"),
        cycle_payload.get("roomTemp"),
        cycle_payload.get("floorTemp"),
        cycle_payload.get("targetTemp"),
        cycle_payload.get("predictedRoom30m"),
        cycle_payload.get("safetyReason"),
        cycle_payload.get("commandAction"),
        cycle_payload.get("commandSetpoint"),
        cycle_payload.get("combustionConfirmed"),
        cycle_payload.get("savedAt"),
    ]
