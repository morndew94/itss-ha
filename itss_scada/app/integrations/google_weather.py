"""ITSS-P300 ATMOS outdoor weather fallback clients (Gemini Search Grounding + Open-Meteo).

When the local Home Assistant outdoor sensor (e.g., Naver Weather) fails or is
unavailable, fetches live outdoor temperature and wind speed for the configured
region name using:
  1. Gemini Search Grounding API (when gemini_api_key is configured)
  2. Open-Meteo free weather API (key-free fallback with Korean city coordinates)
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

# Built-in coordinates for major Korean cities/districts (used by Open-Meteo fallback)
KOREA_COORDS: dict[str, tuple[float, float]] = {
    "서울": (37.5665, 126.9780),
    "인천": (37.4563, 126.7052),
    "수원": (37.2636, 127.0286),
    "용인": (37.2411, 127.1776),
    "성남": (37.4200, 127.1265),
    "분당": (37.3827, 127.1189),
    "고양": (37.6584, 126.8320),
    "일산": (37.6750, 126.7700),
    "화성": (37.1995, 126.8312),
    "평택": (36.9921, 127.1129),
    "안양": (37.3943, 126.9568),
    "부천": (37.5034, 126.7660),
    "남양주": (37.6360, 127.2165),
    "김포": (37.6152, 126.7156),
    "파주": (37.7600, 126.7800),
    "광명": (37.4786, 126.8644),
    "부산": (35.1796, 129.0756),
    "대구": (35.8714, 128.6014),
    "대전": (36.3504, 127.3845),
    "광주": (35.1595, 126.8526),
    "울산": (35.5384, 129.3114),
    "세종": (36.4801, 127.2890),
    "청주": (36.6424, 127.4890),
    "천안": (36.8151, 127.1139),
    "전주": (35.8242, 127.1480),
    "창원": (35.2279, 128.6811),
    "포항": (36.0190, 129.3435),
    "춘천": (37.8813, 127.7298),
    "강릉": (37.7519, 128.8761),
    "제주": (33.4996, 126.5312),
}


@dataclass(slots=True)
class WeatherSnapshot:
    """Parsed outdoor weather telemetry for P300 ATMOS."""

    outdoor_temp_c: float | None
    wind_speed_ms: float | None
    condition: str
    source: str  # "GEMINI_SEARCH_GROUNDING", "OPEN_METEO_API", or "FALLBACK"
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

    def set_location(self, location: str) -> None:
        if location != self.location:
            self.location = location
            self._cache = None

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
            if self._cache is not None:
                return self._cache
            return WeatherSnapshot(None, None, "fetch_error", "FALLBACK", now)

    @staticmethod
    def _extract_json(text: str) -> dict:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise ValueError("No JSON object found in weather response")
        return json.loads(match.group(0))


class OpenMeteoWeatherClient:
    """Key-free fallback weather client using Open-Meteo API with 30-min TTL cache."""

    def __init__(
        self,
        location: str = "서울",
        *,
        cache_ttl_sec: int = 1800,
        http_get: Callable[[str], dict] | None = None,
    ) -> None:
        self.location = location or "서울"
        self.cache_ttl_sec = cache_ttl_sec
        self._http_get = http_get or self._default_get
        self._cache: WeatherSnapshot | None = None
        self._coords_cache: dict[str, tuple[float, float]] = {}

    def set_location(self, location: str) -> None:
        if location and location != self.location:
            self.location = location
            self._cache = None

    def _default_get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "ITSS-SCADA/2.4"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _resolve_coords(self) -> tuple[float, float]:
        loc = self.location.strip()
        if loc in self._coords_cache:
            return self._coords_cache[loc]
        # Match built-in Korean city/district substring first (instant, offline-safe)
        for city, coords in KOREA_COORDS.items():
            if city in loc:
                self._coords_cache[loc] = coords
                return coords
        # Otherwise query Open-Meteo geocoding API
        q = urllib.parse.quote(loc)
        geo = self._http_get(
            f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=ko&format=json"
        )
        results = geo.get("results") or []
        if results:
            coords = (float(results[0]["latitude"]), float(results[0]["longitude"]))
            self._coords_cache[loc] = coords
            return coords
        return KOREA_COORDS["서울"]

    def get_weather(self, now_epoch: float | None = None) -> WeatherSnapshot:
        """Fetch live outdoor temperature & wind speed from Open-Meteo without API key."""
        now = now_epoch if now_epoch is not None else time.time()
        if self._cache and (now - self._cache.fetched_at_epoch) < self.cache_ttl_sec:
            return self._cache

        try:
            lat, lon = self._resolve_coords()
            url = (
                f"https://api.open-meteo.com/v1/forecast?latitude={lat:.4f}&longitude={lon:.4f}"
                "&current=temperature_2m,wind_speed_10m&wind_speed_unit=ms&timezone=Asia%2FSeoul"
            )
            data = self._http_get(url)
            cur = data.get("current") or {}
            temp = cur.get("temperature_2m")
            if temp is None:
                raise ValueError("Missing temperature_2m in Open-Meteo response")
            wind = cur.get("wind_speed_10m", 0.0)
            snap = WeatherSnapshot(
                outdoor_temp_c=round(float(temp), 1),
                wind_speed_ms=round(float(wind), 1),
                condition="open_meteo",
                source="OPEN_METEO_API",
                fetched_at_epoch=now,
            )
            self._cache = snap
            return snap
        except Exception:  # noqa: BLE001
            if self._cache is not None:
                return self._cache
            return WeatherSnapshot(None, None, "fetch_error", "FALLBACK", now)


class FallbackWeatherProvider:
    """Chains Gemini Search Grounding (if key set) -> Open-Meteo API for configured region."""

    def __init__(
        self,
        location: str = "서울",
        *,
        gemini_api_key: str = "",
        gemini_client: GoogleWeatherClient | None = None,
        open_meteo_client: OpenMeteoWeatherClient | None = None,
    ) -> None:
        self.location = location or "서울"
        self.gemini = gemini_client or (GoogleWeatherClient(gemini_api_key, self.location) if gemini_api_key else None)
        self.open_meteo = open_meteo_client or OpenMeteoWeatherClient(self.location)

    def set_location(self, location: str) -> None:
        clean = location.strip() or "서울"
        self.location = clean
        if self.gemini is not None:
            self.gemini.set_location(clean)
        self.open_meteo.set_location(clean)

    def get_weather(self, now_epoch: float | None = None) -> WeatherSnapshot:
        """Try Gemini Search Grounding first (if configured), then Open-Meteo API."""
        if self.gemini is not None:
            g = self.gemini.get_weather(now_epoch=now_epoch)
            if g.outdoor_temp_c is not None:
                return g
        return self.open_meteo.get_weather(now_epoch=now_epoch)
