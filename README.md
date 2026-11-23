# I.T.S.S. Thermal SCADA (Home Assistant Add-on)

Home Assistant **전용 애드온(Add-on + Ingress 웹 UI)**으로 바닥 난방의 **0.3°C 정밀 제어(목표 온도 ±0.15°C)**, **방별 설정온도 제어**, **수동/자동 운전 전환**, **단일 바닥 센서 기반 하이브리드 다중 방 제어**, **KST 정밀 시각 이력 DB**, **Google API 보조 연동**을 제공하는 SCADA 시스템입니다.

---

## 1. 주요 기능

- **HA 사이드바 내장 웹 UI (Ingress 지원)**: 별도 포트 개방 없이 Home Assistant 좌측 사이드바에서 실시간 SCADA 대시보드 접속
- **방별 목표 온도 설정 (`0.1°C` 단위)**: 거실/안방/작은방 등 방마다 독립적인 설정온도 조절 및 즉시 SQLite 저장
- **수동 운전 모드 (`AUTO` / `수동 ON` / `수동 OFF`)**: `P000 SENTINEL` 수동 조작 우선 규칙 준수 (단, 실내 27°C / 바닥 29°C 이상 극한 과열 차단은 항상 작동)
- **단일 바닥 온도 센서 + 방별 개별 밸브 하이브리드 제어**:
  - 동시 기동(Co-heating Batch)으로 보일러 단펄스 방지 + 목표 도달 방 개별 선차단으로 오버슈트 방지
  - 마스터 방(`MEASURED`)과 슬레이브 방(`ESTIMATED` + 오프셋) 구분 기록
- **Google API 보조 연동 (Fail-safe 폴백)**:
  - `P300 ATMOS`: Gemini 검색 그라운딩 외기/풍속 보정
  - `P400 CIRCADIA`: Google Calendar 외출/귀가 예열 보정
  - `P200 THERM`: 24시간 0.3°C 달성률 KPI + Gemini 일일 진단 리포트
  - `알림`: 긴급 과열/센서 이상 시 Gemini TTS 음성 경고 생성 및 Google Sheets 행 내보내기

---

## 2. 저장소 구조

```text
itss-ha/
├── repository.yaml                    # Home Assistant Add-on Store 저장소 정의
├── itss_scada/                        # Home Assistant 단일 애드온
│   ├── config.yaml                    # ingress: true, homeassistant_api: true
│   ├── Dockerfile
│   ├── run.sh
│   ├── requirements.txt
│   ├── DOCS.md
│   └── app/
│       ├── main.py                    # FastAPI SCADA 서버, 백그라운드 제어 루프, REST API
│       ├── ha_client.py               # HA Supervisor API 클라이언트 (상태 조회/제어/재조회)
│       ├── static/
│       │   └── index.html             # SCADA 대시보드 웹 UI
│       ├── storage/db.py              # SQLite (방 설정 + KST 회차 이력)
│       ├── models/schema.py           # KST 8종 정밀 시각 스키마
│       ├── protocols/                 # P000~P400, 다중 방 하이브리드, RC 피팅 엔진
│       └── integrations/              # Google Weather, Calendar, Gemini Advisor, TTS/Sheets
├── docs/ARCHITECTURE.md
├── tests/                             # 프로토콜, Google API, FastAPI 애드온 통합 테스트
└── .github/workflows/validate.yml
```

---

## 3. 설치 방법 (Home Assistant Add-on Store)

1. Home Assistant의 **설정 > 애드온 > 애드온 스토어 > 우측 상단 메뉴 > 저장소(Repositories)**를 엽니다.
2. `https://github.com/morndew94/itss-ha` 를 추가합니다.
3. **I.T.S.S. Thermal SCADA** 애드온을 설치하고 **사이드바에 표시(Show in sidebar)**를 켠 뒤 시작합니다.
4. 좌측 사이드바의 **I.T.S.S. SCADA** 메뉴에서 방별 엔티티 매핑, 목표 온도, 수동/자동 모드, 시험 운전(Dry-Run) 여부를 제어합니다.

---

## 4. 로컬 실행 및 테스트

```bash
pip install -r itss_scada/requirements.txt pytest
pytest -v

# 로컬에서 웹 UI 미리보기 실행 (http://127.0.0.1:8099)
uvicorn itss_scada.app.main:app --reload --port 8099
```
