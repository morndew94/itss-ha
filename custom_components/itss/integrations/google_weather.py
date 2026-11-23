"""ITSS-P300 ATMOS Google Weather adapter using Gemini Search Grounding.

Periodically fetches outdoor temperature and wind speed for a given location
with TTL caching and automatic fail-safe fallback when offline or rate-limited.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable

import urllib.request


@dataclass(slots=True)
class WeatherSnapshot:
    """Parsed outdoor weather telemetry for P300 ATMOS."""

    outdoor_temp_c: float | None
    wind_speed_ms: float | None
    condition: str
    source: str  # "GEMINI_SEARCH_GROUNDING" or "FALLBACK"
    fetched_at_epoch: float


class GoogleWeatherClient:
    """Fetches outdoor weather via Gemini Search Grounding with local TTL cache."""

    def __init__(
        self,
        api_key: str,
        location: str,
        *,
        model_name: str = "gemini-3.8-flash",
        cache_ttl_sec: int = 1800,
        http_post: Callable[[str, dict], dict] | None = None,
    ) -> None:
        self.api_key = api_key
        self.location = location
        self.model_name = model_name
        self.cache_ttl_sec = cache_ttl_sec
        self._http_post = http_post or self._default_post
        self._cache: WeatherSnapshot | None = None

    def _default_post(self, url: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_weather(self, now_epoch: float | None = None) -> WeatherSnapshot:
        """Return cached or freshly grounded weather; never raises to control loop."""
        now = now_epoch if now_epoch is not None else time.time()
        if self._cache and (now - self._cache.fetched_at_epoch) < self.cache_ttl_sec:
            return self._cache

        if not self.api_key:
            return WeatherSnapshot(None, None, "no_api_key", "FALLBACK", now)

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model_name}:generateContent?key={self.api_key}"
        )
        prompt = (
            f"현재 {self.location}의 최신 기상 정보(기온 °C, 풍속 m/s, 날씨 상태)를 검색하여 "
            '반드시 JSON 객체만 출력하세요. 예: {"outdoor_temp_c": 3.5, "wind_speed_ms": 2.4, "condition": "맑음"}'
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
        }

        try:
            data = self._http_post(url, payload)
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = self._extract_json(text)
            snap = WeatherSnapshot(
                outdoor_temp_c=float(parsed["outdoor_temp_c"]),
                wind_speed_ms=float(parsed.get("wind_speed_ms", 0.0)),
                condition=str(parsed.get("condition", "unknown")),
                source="GEMINI_SEARCH_GROUNDING",
                fetched_at_epoch=now,
            )
            self._cache = snap
            return snap
        except Exception:  # noqa: BLE001
            # Fail-safe: return stale cache if available, otherwise None for P300 fallback
            if self._cache is not None:
                return self._cache
            return WeatherSnapshot(None, None, "fetch_error", "FALLBACK", now)

    @staticmethod
    def _extract_json(text: str) -> dict:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise ValueError("No JSON object found in weather response")
        return json.loads(match.group(0))
