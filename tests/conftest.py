"""공통 테스트 픽스처: 네트워크 차단, 애드온 옵션 파일 격리, Ingress IP 필터 테스트 클라이언트 허용."""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # 실제 /data/options.json 이나 로컬 옵션 파일을 읽지 않도록 격리
    monkeypatch.setenv("ITSS_OPTIONS_PATH", str(tmp_path / "no_options.json"))
    monkeypatch.delenv("ITSS_DB_PATH", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Starlette TestClient 의 기본 클라이언트 호스트("testclient")를 허용 목록에 추가
    monkeypatch.setenv("ITSS_ALLOWED_IPS", "testclient")

    # 외부 네트워크 호출 금지 (Open-Meteo/Gemini 기본 HTTP 경로)
    def _blocked(*_args, **_kwargs):
        raise RuntimeError("network access disabled in tests")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    yield
