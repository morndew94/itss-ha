# I.T.S.S. Thermal SCADA (Home Assistant Add-on)

Home Assistant **전용 애드온(Add-on + Ingress 웹 UI)** 입니다. 온돌(바닥 난방) 방별 제어, 단일 마스터 바닥 sensor 기반 하이브리드 다중 방 제어, 센서 보고 주기 학습, KST 회차 이력 DB, 실운전 분석용 로그/트레이스를 제공합니다. 버전은 **2.9.1** 입니다.

핵심 요약

| 항목 | 내용 |
| :--- | :--- |
| 배포 형태 | Add-on 전용 (HA Ingress). `custom_components` 통합구성요소 형태는 없습니다. |
| 아키텍처 | `amd64` 전용 (`config.yaml` `arch: [amd64]`) |
| 외부 노출 | `config.yaml` 에 `ports` 없음. Ingress(Supervisor)만 접속 가능 |
| 기본 운전 모드 | 시험 운전(`dry_run: true`). 실제 조절기 명령은 LIVE 전환 후에만 전송 |
| 과열 차단 | 실내 24.0 °C / 바닥 25.0 °C (`const.py`, AUTO/MANUAL 공통) |

---

## 1. 주요 기능

- **HA 사이드바 웹 UI (Ingress)**: 별도 포트 없이 HA 사이드바에서 SCADA 대시보드에 접속합니다.
- **방별 목표 온도 (`0.1 °C` 단위, 10.0 ~ 27.0 °C)**: 방마다 독립 설정하고 SQLite 에 즉시 저장합니다.
- **운전 모드 `AUTO` / `MANUAL`(`HEAT`/`OFF`/`HOLD`)**: 수동 운전에도 최소 안전 조건이 적용됩니다(3장 참조).
- **단일 마스터 바닥 sensor + 방별 밸브 하이브리드 제어**:
  - 동시 기동(Co-heating Batch)으로 보일러 단펄스를 줄이고, 목표 상한 도달 방은 개별 선차단합니다.
  - 마스터 방은 `MEASURED`, 나머지 방은 `ESTIMATED`(마스터 바닥값 + `floorOffsetC`) 또는 `ESTIMATE_OFF`(독립 제어)로 구분 기록합니다.
  - **바닥 기준 동시 제어 그룹 / 독립 제어 (v2.9.0)**: 방 테이블 토글은 `동시 제어(바닥 공유)` / `독립 제어` 입니다(API 필드 `floorEstimateEnabled` 동일). offset `±0.1 °C` 버튼(−5.0 ~ +5.0 °C)과 RC 피팅 제안 `제안 … 적용` 버튼은 그대로입니다.
    - **동시 제어 그룹**: 바닥 sensor 방(leader, 예: 방3) + "바닥 기준 공유" ON 인 센서 없는 방(follower, 예: 방2) 중 `AUTO` 방이 그룹을 이룹니다. 그룹 수요(방 weight 가중평균 편차)로 함께 기동/차단하며, leader 가 공유 바닥에 대한 전체 안전(과열, 상한, 상승률 기반 예상 peak, 최대 가동, 휴지, 잔열)을 수행하고 follower 는 방 기준 안전만 수행한 뒤 leader 의 결정을 따릅니다.
    - **독립 제어**(`ESTIMATE_OFF`, 예: 거실, 안방): 바닥값·안전 바닥이 없습니다(v2.8 마스터 대리 판정 제거). 독립 방끼리 기존 hybrid multi-zone 으로 제어하며, 실내 과열 24 °C·외기 연동 최대 가동·잔열 휴지·창문/환기 보호를 받습니다. 마스터 바닥 과열/누락의 영향을 받지 않습니다.
    - 바닥 sensor 방을 MANUAL 로 두면 AUTO 추정 방은 첫 추정 방을 leader 로 그룹을 구성하지만 바닥 sensor 는 그 난방을 보지 못하므로 장시간 사용하지 마십시오.
  - **바닥 상승률 모델 (v2.9.0)**: `floorRiseRateCPerMin` 을 학습하여 기동 전 가드와 가동 중 차단을 같은 예측(시작 바닥 + rate x 가동 분 >= 바닥 cap - 0.2 °C)으로 통일합니다.
  - **외부 조작 가동 유지 (v2.9.1)**: `AUTO` 방을 월패드/다른 HA 자동화로 켜면 ITSS 가 되돌리지 않고 `externalBoostMin`(기본 15 min, 0 ~ 60, 0 = 즉시 되돌림) 동안 가동을 유지한 뒤 자동 중지합니다(8장 참조).
  - 전략: `hybrid`(기본) / `simultaneous`(가중평균 일괄 기동).
- **센서 보고 주기 감지 (v2.7.0)**: HA `last_reported`(없으면 `last_updated`) 기준으로 sensor별 보고 간격을 학습하여 stale 임계, 기울기, 바닥 지연 보상에 반영합니다. UI 패널 "센서 보고 주기 감지 현황"에서 확인합니다.
- **예측 제어(P200)**: `linear_v12`(선형 외삽) 또는 `adaptive_rc`(2-node RC) 모델로 30분(학습값에 따라 가변) 후 온도를 예측하여 예측 기동/선차단을 판단합니다.
- **보일러 통계/차트**: 최근 24 h(시간 기준) KPI, 보일러 가동 시간·duty·단펄스 횟수·추정 가스비, `GET /api/v1/chart` 기반 24 h 차트. 시험 운전의 가상 HEAT 는 가동 시간에서 제외합니다.
- **P400 스케줄**: DB 에 저장된 시간대별 목표 offset 슬롯(기본 4개)을 UI 에서 편집합니다.
- **프리셋**: `AUTO_ALL`, `AWAY`(-2.0 °C), `SLEEP`(-0.3 °C), `PREHEAT`(+0.2 °C), `VENTILATION`, `ALL_OFF`.
- **외기 입력**: HA 센서(`outdoor_temp_entity`) 우선 -> 없으면 Open-Meteo API -> 실패 시 Gemini Search Grounding(API key 설정 시) -> 모두 실패하면 시간대 폴백.
- **알림**: HA `notify` 서비스 및 HA `telegram_bot`(`chat_id`, 구형 HA 는 `target` 으로 재시도). 보일러 ON/OFF 변화, 치명 안전 사유, 조절기 명령 실패, 난방 부족, 제어 루프 연속 실패를 알립니다.
- **Gemini 일일 리포트 (v2.9.1, 자동 발송, 참고용)**: 매일 `dailyReportHour`(기본 08시 KST)에 설정된 알림(HA notify / Telegram)으로 24 h 요약 + Gemini 분석을 발송합니다. UI 버튼(`POST /api/v1/report/daily`)으로 미리보기/즉시 발송도 가능합니다. API key 가 없으면 요약만 발송합니다. 제어 설정은 절대 변경하지 않습니다(8장 참조).
- **실내 센서 보정값 (v2.9.1)**: 방별 `roomTempOffsetC`(-3.0 ~ +3.0 °C)로 실내 온도 센서 편차를 보정하고, 동시 비교 측정으로 보정값을 제안합니다(8장 참조).
- **쾌적 학습 (v2.9.1, 제안 전용)**: 월패드 조작/ITSS 수동 ON 을 "추움" 신호로 기록하여 목표 온도/바닥 최저 온도 상향을 **제안**만 합니다(자동 적용 없음, 8장 참조).
- **RC 피팅/offset 제안**: `POST /api/v1/calibrate/auto`. 마스터 방의 실측 바닥만 사용하고, 슬레이브 방 floor offset 은 **제안값만** 반환·저장(`suggested_floor_offsets`)하며 자동 적용하지 않습니다. UI 에서 방별 버튼으로 적용합니다.

### 라이브러리 전용 모듈 (제어 루프에 연결되어 있지 않음)

아래 모듈은 코드에 존재하고 단위 테스트 대상이지만 **현재 제어 루프/UI 에 연결되지 않았으므로 활성 기능이 아닙니다.**

| 모듈 | 상태 |
| :--- | :--- |
| `GoogleCalendarAdvisor` (`integrations/google_calendar.py`) | 라이브러리 전용. 일정 기반 예열 보정은 동작하지 않음 |
| `GeminiTTSAlert` (`integrations/google_alerts.py`) | 라이브러리 전용. `responseModalities: ["AUDIO"]` 요청 후 24 kHz / 16-bit / mono PCM 을 WAV 로 포장하는 기능만 구현. 경보 흐름에서 호출되지 않음 |
| `format_cycle_for_sheets` | 라이브러리 전용. Google Sheets 내보내기는 동작하지 않음 |
| `CircadiaProtocol` (`protocols/p400_circadia.py`) | 라이브러리 전용. 실제 P400 보정은 DB 스케줄 슬롯(`db.active_schedule_offset`)을 사용 |

---

## 2. 보안 모델

| 항목 | 동작 |
| :--- | :--- |
| 포트 | `config.yaml` 에 `ports` 를 선언하지 않습니다. Ingress(`ingress_port: 8099`) 경유만 가능합니다. |
| IP 필터 | 앱이 요청 클라이언트 IP 를 검사하여 `172.30.32.2`(Supervisor/Ingress)와 loopback(`127.0.0.1`, `::1`) 외에는 **HTTP 403** 으로 거부합니다. |
| 허용 IP 추가 | 환경변수 `ITSS_ALLOWED_IPS="ip1,ip2"` 로 추가합니다. `"*"` 는 필터 해제(로컬 개발 전용)입니다. |
| 입력 검증 | 방 이름(`<>"'&/\` 및 제어문자 금지, 1~40자), entity ID(`domain.object_id` 형식), 설정 범위를 서버에서 검증합니다. UI 는 모든 동적 텍스트를 escape 합니다. |
| 비밀값 | `ha_token`, `gemini_api_key` 는 `password` 타입 옵션입니다. Gemini API key 는 URL query 가 아닌 `x-goog-api-key` 헤더로 전송합니다. |
| 디버그 엔드포인트 | `/api/v1/debug/*` 도 동일한 IP 필터를 적용받습니다(Ingress 전용). |

### 안전 동작 요약

- **마스터 바닥 sensor 미설정**: `AUTO` 방은 강제 OFF(`P000_FLOOR_UNCONFIGURED`)이며, `dryRun=false`(LIVE) 전환 요청은 **HTTP 409** 로 거부됩니다.
- **과열 차단**: 실내 >= 24.0 °C 또는 바닥 >= 25.0 °C 이면 `P000_OVERHEAT_CUTOFF` 로 OFF 합니다(`const.py` 의 `DEFAULT_OVERHEAT_ROOM_C`, `DEFAULT_OVERHEAT_FLOOR_C`, `AUTO`/`MANUAL` 공통).
  - `floor_off_limit_c`(기본 26.5 °C)가 25.0 °C 이상이면 과열 차단이 항상 먼저 작동합니다. 이 경우 `/health` 의 `warnings` 와 UI 에 경고가 표시됩니다.
- **수동 ON 보호**: 센서 누락/stale 이면 수동 ON 도 차단하고, 바닥이 `floorOffLimitC` 에 도달하면 래치 OFF 하며 `floorOffLimitC - 1.0 °C` 이하로 내려가야 해제됩니다(1.0 °C hysteresis).
- **제어 회차 직렬화**: 루프, `run-now`, 프리셋, 수동 전환, 방 삭제, 조절기 테스트가 `asyncio.Lock` 으로 직렬화됩니다.
- **설정 교차 검증**: `heatOffSetpoint <= heatOnSetpoint - 3.0 °C` 를 만족해야 하며 위반 시 **HTTP 422** 입니다.
- **채터링 방지**: 재량(discretionary) 반전에만 적용합니다. HEAT -> OFF 최소 300 s, OFF -> HEAT 최소 600 s. 안전 차단은 대기 없이 즉시 적용됩니다. 1회 최소 가동 펄스는 5 min 입니다.
- **제어 루프 감시**: 회차 실패는 로그로 남기고 3회 연속 실패 시 알림을 보내며 `/health` 는 `degraded` 를 반환합니다.

---

## 3. 저장소 구조

```text
itss-ha/
├── repository.yaml                    # Home Assistant Add-on Store 저장소 정의
├── requirements-dev.txt               # 개발/CI 의존성 (pytest, ruff, httpx 등, 버전 고정)
├── pyproject.toml                     # pytest 설정
├── itss_scada/                        # Home Assistant 단일 애드온
│   ├── config.yaml                    # ingress: true, homeassistant_api: true, ports 없음, arch: amd64
│   ├── Dockerfile                     # amd64-base-python:3.12-alpine3.23-2026.10.3 고정
│   ├── run.sh                         # uvicorn --factory app.main:create_app
│   ├── requirements.txt               # 런타임 의존성 (== 고정)
│   ├── DOCS.md                        # HA 애드온 화면에 표시되는 사용 설명서
│   └── app/
│       ├── main.py                    # FastAPI 서버, ItssEngine(제어 루프), REST API, IP guard
│       ├── const.py                   # 안전 한계/타이머 상수 (과열 기준 단일 출처)
│       ├── comfort.py                 # 쾌적 학습(v2.9.1): 추움 신호 기록/분석, 제안 전용
│       ├── debug_trace.py             # 텍스트 로그 회전 파일 + JSONL 판단 트레이스
│       ├── ha_client.py               # HA Supervisor API 클라이언트 (last_reported 우선)
│       ├── static/index.html          # SCADA 대시보드 웹 UI
│       ├── storage/db.py              # SQLite (rooms, schedules, kv_store, cycles) / WAL
│       ├── models/schema.py           # CycleRecord 등 KST 스키마
│       ├── protocols/
│       │   ├── p000_sentinel.py       # 안전 인터락
│       │   ├── p100_core.py           # 조절기 0.5 °C 반올림, 반대 명령 간격
│       │   ├── p200_therm.py          # linear_v12 / adaptive_rc 예측
│       │   ├── p300_atmos.py          # 외기 보정 offset, 외기 연동 최대 가동 시간
│       │   ├── p400_circadia.py       # CircadiaProtocol (라이브러리 전용)
│       │   ├── multi_zone.py          # hybrid / simultaneous 다중 방 조율
│       │   ├── sensor_tracker.py      # 센서 보고 주기 학습, 적응형 stale, 보간 기울기
│       │   ├── thermal_lag_learner.py # 방별 잔열/지연 학습
│       │   └── rc_optimizer.py        # RC 파라미터 패턴 탐색 피팅
│       └── integrations/              # weather(Open-Meteo/Gemini), notifications, advisor, (calendar/TTS 라이브러리)
├── docs/ARCHITECTURE.md               # 제어 파이프라인/스키마/트레이스 규격
├── tests/
│   ├── conftest.py                    # 테스트 중 외부 네트워크 차단
│   ├── test_v27_hardening.py          # v2.7.0 안전/센서/통계/디버그 회귀 테스트
│   ├── test_v28_floor_estimate.py     # v2.8.0 바닥 추정 ON/OFF·offset 테스트
│   ├── test_v291_comfort_report.py    # v2.9.1 쾌적 학습/일일 리포트/센서 보정 테스트 (11개)
│   ├── test_addon_api.py, test_protocols.py, test_lag_and_restart.py, test_google_and_multizone.py
└── .github/workflows/validate.yml     # ruff + pytest (Python 3.12)
```

---

## 4. 설치 방법 (Home Assistant Add-on Store)

1. **설정 > 애드온 > 애드온 스토어 > 우측 상단 메뉴 > 저장소(Repositories)** 를 엽니다.
2. `https://github.com/morndew94/itss-ha` 를 추가합니다.
3. **I.T.S.S. Thermal SCADA** 를 설치하고 **사이드바에 표시(Show in sidebar)** 를 켠 뒤 시작합니다.
4. 사이드바 **I.T.S.S. SCADA** 에서 다음 순서로 설정합니다.
   1. 방 등록: 실내 온도 sensor, `climate` entity, (선택) 창문 접점 sensor.
   2. **마스터 바닥 온도 sensor**(`master_floor_entity`) 설정. 미설정 시 `AUTO` 난방이 차단됩니다.
   3. 시험 운전(Dry-Run) 상태에서 `itss.cycle` 로그/트레이스로 판단을 검증합니다.
   4. 판단이 타당하면 `dry_run` 을 해제(LIVE)합니다.

> 이미지는 `ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.23-2026.10.3` 로 로컬 빌드되며, 런타임 의존성은 `requirements.txt` 에 `==` 로 고정되어 있습니다.

---

## 5. 옵션 (`config.yaml`)

HA **애드온 구성** 화면의 옵션입니다.

| 옵션 | 기본값 | 범위 / 단위 | 설명 |
| :--- | :--- | :--- | :--- |
| `db_path` | `/data/itss_scada.db` | 문자열 | SQLite 경로. 로그/트레이스는 같은 디렉터리(`/data`) 하위에 저장 |
| `update_interval_sec` | 60 | 30 ~ 3600 s | 제어 회차 주기. 옵션 전용(UI 변경 불가) |
| `zone_strategy` | `hybrid` | `hybrid` / `simultaneous` | 다중 방 전략 |
| `predictor_model` | `linear_v12` | `linear_v12` / `adaptive_rc` | P200 예측 모델 |
| `dry_run` | `true` | bool | `true` 면 명령 미전송(`DRY_RUN` 기록) |
| `heat_on_setpoint` | 24.0 | 15 ~ 30 °C | HEAT 시 조절기 기본 설정온도 |
| `heat_off_setpoint` | 15.0 | 5 ~ 25 °C | OFF 시 조절기 기본 설정온도. `heat_on_setpoint - 3.0 °C` 이하여야 함 |
| `floor_off_limit_c` | 26.5 | 22 ~ 29 °C | 바닥 OFF 상한 (25.0 °C 이상이면 과열 차단이 먼저 작동) |
| `max_run_cap_min` | 15 | 3 ~ 15 min | 1회 최대 연속 가동 상한 (내부적으로 최소 5 min 보장) |
| `cooldown_min` | 20 | 5 ~ 90 min | 차단 후 잔열 휴지 기본값 (학습값이 있으면 학습값 사용) |
| `ventilation_hold_min` | 20 | 5 ~ 60 min | 환기 보호 유지 시간 |
| `history_retention_days` | 30 | 1 ~ 365 일 | 회차 이력(DB) 보존 기간 |
| `master_floor_entity` | `""` | entity ID | 마스터 바닥 온도 sensor. 비어 있으면 AUTO 차단, LIVE 전환 불가 |
| `outdoor_temp_entity` | `""` | entity ID | 외기 sensor 또는 `weather.*` entity (선택) |
| `gas_krw_per_hour` | 1200 | 100 ~ 10000 KRW/h | 추정 가스비 계산 단가 |
| `ha_token` | `""` | password | 비워두면 Supervisor token 사용 |
| `notify_service` | `""` | 문자열 | HA `notify` 서비스/entity 이름 |
| `telegram_chat_id` | `""` | 문자열 | HA `telegram_bot` 대상 chat_id |
| `gemini_api_key` | `""` | password | 외기 폴백/진단 리포트용 (선택) |
| `weather_location` | `서울` | 문자열 | Open-Meteo/Gemini 외기 조회 지역 |
| `log_level` | `info` | `debug`/`info`/`warning`/`error` | `debug` 면 방별 판단 상세 로그 출력 |
| `debug_trace` | `true` | bool | JSONL 판단 트레이스 기록 여부 |
| `debug_trace_days` | 7 | 1 ~ 30 일 | 트레이스 파일 보존 기간 |

**옵션 우선순위**: 기본 옵션 값 위에 UI 에서 저장한 값이 덮어씁니다. 단, 지난 부팅 이후 HA 애드온 구성 화면에서 값이 바뀐 옵션은 옵션 값이 이깁니다. 업그레이드 후 첫 부팅(이전 옵션 snapshot 없음)에서는 기존 UI 저장값을 유지합니다.

**DB**: SQLite WAL 모드이며 `history_retention_days` 가 지난 회차는 주기적으로(60 회차마다 및 부팅 시) 삭제됩니다.

---

## 6. 디버깅 / 실운전 분석

### 6.1 텍스트 로그

- 출력: stdout(HA 애드온 로그 탭) + `<data>/logs/itss.log` (회전, 2 MB x 5개).
- 로거와 내용:

| 로거 | INFO | DEBUG |
| :--- | :--- | :--- |
| `itss.cycle` | 회차 요약(`회차 #N`), 방별 판단 변경(`판단 변경 A -> B`) | 방별 전체 trace 1줄 |
| `itss.sensor` | 보고 주기 학습/변경, stale 임계 변경, 값 복구 (누락은 WARNING) | 새 보고 1건마다 |
| `itss.dispatch` | `SENT` (FAILED 는 ERROR) | `NO_CHANGE`, `DRY_RUN` |
| `itss.weather` | 외기 소스 변경 | 매 판독 |

- 방별 상세 로그가 필요하면 `log_level` 을 `debug` 로 설정합니다.
- UI **"디버그 로그 보기"** 버튼 또는 `GET /api/v1/debug/logs?lines=N` (10 ~ 5000, 기본 300).

### 6.2 JSONL 판단 트레이스

`<data>/trace/trace-YYYYMMDD.jsonl` 에 **회차 x 방 1줄** 이 기록됩니다(`debug_trace: true`, 보존 `debug_trace_days`). 지난 일자 파일은 `trace-YYYYMMDD.jsonl.gz` 로 자동 압축되며, 다운로드 API 는 압축 파일도 그대로 읽어 JSONL 로 반환합니다. 용량 실측: 약 3.0 KB/줄 (smoke 시뮬레이션 8줄 평균) -> 60 s 주기·방 4개 기준 비압축 약 17 MB/일(계산값).

최상위 키: `ts`, `cycleId`, `room`, `mode`, `manualAction`, `cycle`, `inputs`, `dynamics`, `limits`, `lagProfile`, `timersBefore`, `p200`, `multiZone`, `sentinel`, `decision`, `command`, `timersAfter`, `elapsedMs`.

| 엔드포인트 (Ingress 전용) | 설명 |
| :--- | :--- |
| `GET /api/v1/debug/logs?lines=N` | 텍스트 로그 마지막 N줄 |
| `GET /api/v1/debug/trace/files` | 트레이스 파일 목록, 활성 여부, 보존 일수 |
| `GET /api/v1/debug/trace?date=today\|YYYYMMDD&room_name=&limit=` | 해당 일자 JSONL 다운로드 (없으면 404) |

UI 버튼 **"오늘 판단 트레이스(JSONL)"** 도 같은 데이터를 내려받습니다.

```python
import pandas as pd

# 다운로드한 JSONL 을 DataFrame 으로 읽기
df = pd.read_json("itss-trace-20261124.jsonl", lines=True)

# 중첩 dict(inputs, decision, sentinel ...)를 평탄화: inputs.roomTemp, decision.reason ...
flat = pd.json_normalize(df.to_dict("records"))
print(flat[["ts", "room", "inputs.roomTemp", "inputs.floorTemp", "decision.action", "decision.reason"]].tail())
```

> 필드별 의미는 `docs/ARCHITECTURE.md` 의 트레이스 스키마를 참고하십시오.

---

## 7. 로컬 개발 및 테스트

```bash
pip install -r requirements-dev.txt
pytest -v          # 전체 84개 테스트. tests/conftest.py 가 외부 네트워크를 차단합니다

# 로컬에서 웹 UI 실행 (http://127.0.0.1:8099)
ITSS_ALLOWED_IPS="*" uvicorn --factory itss_scada.app.main:create_app --port 8099
```

- loopback(`127.0.0.1`, `::1`)은 기본 허용이므로 같은 PC 브라우저 접속에는 `ITSS_ALLOWED_IPS` 가 필요 없습니다. 다른 PC/컨테이너에서 접속할 때만 `"*"` 또는 해당 IP 를 지정합니다.
- 로컬에서는 `ITSS_OPTIONS_PATH`(기본 `/data/options.json`), `ITSS_DB_PATH`(기본 `./itss_scada.db`)로 옵션/DB 경로를 바꿀 수 있습니다. HA 연결이 없으면 조절기 명령은 전송되지 않고 `DRY_RUN` 으로 기록됩니다.
- CI(`.github/workflows/validate.yml`)는 `ruff check --select E9,F63,F7,F82,F401,F841` 후 `pytest -v` 를 실행합니다.

---

## 8. v2.9.1 변경 사항

2026-11-25 field trace: 월패드(또는 다른 HA 자동화)에서 설정온도를 24 °C 로 올려도 ITSS 가 약 1 분 안에 15 °C 로 되돌렸고, 0 min 가동 후에도 30 min 잔열 휴지를 시작했습니다. 사용자가 수동으로 난방을 켤 수 없는 문제였습니다.

- **외부 조작 감지**: `AUTO` 방에서 직전 회차 관측이 비가열, 현재 회차 관측이 가열, ITSS 마지막 명령이 HEAT 가 아니면 외부 수동 ON 으로 판단합니다.
- **유지 후 자동 중지**: `externalBoostMin`(기본 15 min, 범위 0 ~ 60, 0 = 기존 동작) 동안 명령 없이 가동을 유지합니다(외부 설정온도는 그대로 둠). 종료 시 OFF 를 보내고 정상 잔열 휴지에 들어갑니다(가동 5 min 이상일 때만 휴지).
- **외부 OFF**: boost 중 사용자가 외부에서 끄면 다시 켜지 않습니다(`EXTERNAL_BOOST_CANCELLED`).
- **안전 유지**: boost 중에도 실내 24 °C / 바닥 25 °C 과열 차단, 창문, 환기 보호, 센서 누락/stale, 바닥 기준 방(방3 센서 방, 방2 추정 방)의 상승률 기반 예상 peak 차단(`P000_FLOOR_PREDICTED_LIMIT`)이 적용됩니다. 잔열 휴지, 1회 최대 가동, 그룹/multi-zone 수요는 boost 중 우회합니다.
- **방3 · 방2 연동**: 바닥을 공유하는 두 방(`AUTO`, 실측/추정 바닥) 중 한 방이라도 외부에서 켜면 **두 방이 같은 종료 시각**(`externalBoostMin`)으로 함께 가동합니다. 일반 그룹 경로를 사용하므로 leader(방3, 실측 바닥)가 바닥 안전(상승률 기반 예상 peak 차단, 과열 등)을 판단하고 follower 가 따라가며, 타이머/휴지는 동기화됩니다. boost 중 follower 의 warm skip 은 사용자 의도를 우선해 우회합니다. 켠 방은 외부 설정온도를 유지하고 ITSS 가 다른 방에 HEAT 를 보냅니다. 한 방을 외부에서 끄면 연동 boost 가 두 방 모두 취소되고(`EXTERNAL_BOOST_CANCELLED`), 종료 시 두 방 모두 OFF(`EXTERNAL_BOOST_END`) + 같은 잔열 휴지에 들어갑니다. 연동 boost 회차에서 최대 가동/휴지는 우회하지만 바닥 안전은 우회하지 않습니다. 취소 판정은 boost 중 실제 가열이 관측된 뒤에만 인정하며, 연동 방의 ITSS HEAT 가 아직 적용 전이면 취소 대신 HEAT 를 재전송합니다. 독립 방(거실/안방)은 단독 boost 이며 multi-zone 계획에서 계속 제외됩니다.
- **오탐 방지**: ITSS 자신의 OFF 가 조절기에 적용되지 않은 경우(직전 관측도 가열)는 외부로 보지 않고 OFF 를 재전송합니다. ITSS 자신의 HEAT 와 ITSS `MANUAL` 방은 감지 대상이 아닙니다.
- **사유 코드**: `EXTERNAL_BOOST`(외부 조작으로 켜짐 감지), `EXTERNAL_BOOST_END`(가동 시간 종료, 잔열 휴지 진입), `EXTERNAL_BOOST_CANCELLED`(외부에서 끔 또는 조절기 자체 설정온도 도달).
- **설정/API/UI**: `externalBoostMin`(`PATCH /api/v1/settings`, UI "월패드·외부 자동화로 켜면 유지 (분, 0=즉시 되돌림)"), `/api/v1/status` `liveByRoom.externalBoostUntil`, 방 카드 배지 "외부 조작 가동 (HH:MM 자동 중지)". 트레이스 최상위 `externalBoost`{`event`: START/ACTIVE/END/CANCELLED, `until`}, diagnostics `externalBoostGroup`. Telegram 시작 알림에 "외부 조작 감지: HH:MM까지 15분 유지 후 자동 중지" 줄이 추가되며, 연동 방은 "외부 조작 감지(방3 · 방2 연동): HH:MM까지 15분 유지 후 자동 중지" 로 표시됩니다.
- **ITSS 수동 HEAT 안전 강화 (1회 요청화)**: 근거(2026-11-25 trace): 수동 HEAT 가 거실 약 154 min 연속 가동(독립 방이라 실내 과열 24 °C 만 정지 조건, 실내는 23.4 °C 까지만 상승), 방3 36.9 min 가동에 바닥 최고 27.1 °C(cap 25 °C).
  - 신규 설정 `manualHeatMaxMin`(기본 15, 범위 5 ~ 60 min, UI "ITSS 수동 ON 1회 최대 (분, 경과 시 자동 복귀)"). 경과 시 OFF 전송, 방을 AUTO(controlMode AUTO, manualAction HOLD)로 복귀시키고 5 min 이상 가동했으면 잔열 휴지를 시작합니다. 사유 `MANUAL_HEAT_TIMEOUT`("수동 난방 설정 시간 경과(자동 중지 · 자동 제어 복귀)").
  - 바닥 기준 방(방3 실측, 방2 추정)의 수동 HEAT 에도 AUTO 와 같은 상승률 기반 바닥 예측을 적용합니다. 시작 전 바닥 + 상승률 x 5 min >= 바닥 cap - 0.2 °C 이면 차단, 가동 중 가열 시작 바닥 + 상승률 x (가동 + poll) >= cap - 0.2 °C 이면 차단합니다.
  - 수동 HEAT 중 바닥 한계(`P000_FLOOR_LIMIT_OFF`), 예상 한계(`P000_FLOOR_PREDICTED_LIMIT`), 과열(`P000_OVERHEAT_CUTOFF`)이 발생하면 수동 요청을 **종료하고 AUTO 로 복귀**합니다(이전에는 MANUAL 로 남아 바닥이 한계보다 1 °C 식으면 스스로 재가열).
  - 추정 방(방2, floorOffsetC 음수)은 수동 모드에서도 실측 마스터 바닥보다 낮지 않은 바닥(공유 바닥)으로 판정합니다. 2026-11-25 22:27 방2 단독 재가열 사례를 수정합니다.
  - 수동 HEAT 중 월패드/자동화가 조절기를 외부에서 끄면 24 °C 를 다시 지시하지 않고 수동 요청을 종료하며 AUTO 로 복귀합니다. 사유 `MANUAL_HEAT_CANCELLED`("수동 난방 중 외부에서 끔(자동 제어 복귀)").
  - Telegram: ON 알림에 "수동 난방: HH:MM까지 최대 N분 후 자동 제어 복귀", 종료 알림에 "수동 난방 종료 → 자동 제어(AUTO) 복귀". `/api/v1/status` `liveByRoom.manualHeatUntil`, 방 카드 배지 "수동 가동 (HH:MM 자동 복귀)". 트레이스 diagnostics `manualEnded`.
- **바닥 최저 온도 (floor comfort minimum)**:
  - 설정 `floorMinC`(기본 0 = 사용 안 함, 범위 0 ~ 23 °C, UI "바닥 최저 온도 (°C, 0=사용 안 함 · 방3/방2 바닥 기준)"). 바닥 기준 그룹(방3/방2)에만 적용되며 독립 방(거실/안방)은 바닥이 없어 영향이 없습니다.
  - 시작: 그룹이 가열 중이 아니고 leader 바닥 < `floorMinC` 이며 그룹 가중 실내 편차 < +0.3 °C(`FLOOR_MIN_AIR_MARGIN_C`, 실내 과열 방지)이면 그룹 HEAT(`GROUP_FLOOR_MIN_HEAT`).
  - 유지: rate 모델 예상 peak(가열 시작 바닥 + 상승률 x 가동 min) < `floorMinC` + 1.0 °C(`FLOOR_MIN_HYST_C`)인 동안 `GROUP_FLOOR_MIN_CONTINUE`, 도달하면 두 방 OFF(`GROUP_FLOOR_MIN_REACHED`, 잔열 휴지 진입).
  - 기존 안전(바닥 cap/예상 차단, 과열, 휴지, 기동 전 차단, 채터링 방지)은 모두 그대로 적용됩니다.
  - 근거: 2026-11-25 09:48 ~ 19:56 약 10 h 동안 실내가 목표 이상이라 AUTO 난방이 없었고 바닥은 23.1 -> 19.7 °C 로 식었습니다. 시작값 권장 21 °C(사용자 조정).

### 8-A. 실내 센서 보정값 (`roomTempOffsetC`)

- 방 필드 `roomTempOffsetC`(-3.0 ~ +3.0 °C, 기본 0). DB 컬럼 `room_temp_offset_c`(자동 마이그레이션, 기존 방은 0).
- **제어용 실내 온도 = 센서 원시값 + 보정값.** 모든 제어/안전(실내 과열 24 °C 포함), 그룹 가중 평균, 기록/차트/KPI 에 적용됩니다. 센서 추적(보고 주기, 기울기, 환기 급락 감지)은 **원시값**을 유지하므로 보정값을 바꿔도 가짜 step 변화가 생기지 않습니다.
- API: `POST /api/v1/rooms`(`roomTempOffsetC` 선택, 재등록 시 생략하면 기존 값 유지), `PATCH /api/v1/rooms/{room}` `{"roomTempOffsetC": x}`(|x| > 3 이면 422). `/api/v1/status` `liveByRoom` 에 `roomTemp`(보정값), `roomTempRaw`, `roomTempOffsetC`. 트레이스 inputs 에 `roomTempRaw`/`roomTempOffsetC`, 회차 diagnostics 에 0 이 아닐 때 `roomTempOffsetC` 저장.
- UI: 방 카드 "센서 보정 -0.1/+0.1" 버튼과 "(원시 x)" 표시.
- **동시 비교 측정 도우미**: `POST /api/v1/calibration/room-sensors` body `{start, end}`(ISO KST), `reference`(`""` = 전체 센서 중앙값(합의), 방 이름 = 그 센서(현재 보정값 포함)), `referenceTempC`(선택, 기준 온도계 값). 구간 2 h 미만이면 422, 모르는 reference 방이면 404.
  - 방별 원시 median(원시 = 기록 roomTemp - 기록 보정값), 보고 수(서로 다른 보고 3개 미만이면 `FEW_REPORTS`), spread(p90 - p10, 1.0 °C 초과 시 경고), `suggestedOffsetC` = 기준 - 원시 median(±3.0 °C clip, 0.1 °C 단위). 적용은 UI [적용] 버튼(PATCH)으로 수동입니다.
  - 절차: ① 측정 중 시험 운전(Dry-Run) 또는 전체 방 수동 OFF 로 전환(센서가 방에 없으므로 제어가 개입하면 안 됨) ② 모든 방 센서를 한 곳(외풍/햇빛/열원 없음)에 모음 ③ 난방 OFF 상태로 4 h 이상(가능하면 밤새) 유지 — 방3/방2 센서는 약 55~60 min 마다 보고 ④ UI "실내 센서 보정 — 동시 비교 측정" 패널에 구간 입력 후 [적용].
  - 상수(`const.py`): `ROOM_TEMP_OFFSET_MAX_C`=3.0 °C, `CALIBRATION_MIN_SPAN_H`=2.0 h, `CALIBRATION_MIN_REPORTS`=3, `CALIBRATION_REF_SPREAD_WARN_C`=1.0 °C.

### 8-B. 쾌적 학습 (`comfort.py`, 제안 전용)

- 신호: (a) `WALLPAD` = `AUTO` 방에서 외부 조작 가동 유지(external boost) **시작** 감지(월패드/다른 HA 자동화). 실제로 조작한 방만 기록하며 연동된 방3/방2 상대 방은 기록하지 않음. (b) `ITSS_MANUAL` = ITSS 수동 ON(수동 세션당 1회).
- 이벤트 저장값: 시각, 방, 출처, 당시 실내 원시 온도 + 보정값, 기본 목표, 실효 목표, 바닥(가드 바닥) + `floorSrc`, 외기. KV `comfort_events`(60 일 보존, 최대 500 개).
- 분석: 최근 14 일. 실내 온도는 원시값 + **현재** 보정값으로 재계산(나중에 센서를 보정하면 과거 신호도 재해석). dev = 실내 - 이벤트 당시 실효 목표.
  - dev >= -0.5 °C → "목표 근처인데 추움", dev < -0.5 °C → "난방 부족"(목표가 아닌 제어 문제). 바닥 차가움(floor-cold) = 바닥 방(MEASURED/ESTIMATED)이고 바닥 < 21.5 °C 이며 dev >= -0.3 °C.
- 제안(자동 적용 없음, UI [적용] 시 confirm):

| 제안 | 조건 | 제안값 |
| :--- | :--- | :--- |
| `TARGET_UP` (방별) | 목표 근처 추움 이벤트 3개 이상 | median(이벤트 실내 + 0.4 °C - 당시 스케줄/외기/프리셋 offset). 현재 + 1.0 °C 이하, 23.5 °C 이하(실내 과열 24.0 - 0.5), 현재 + 0.2 °C 이상일 때만. 절대값 방식이라 적용 후 같은 이벤트가 다시 올리지 않음 |
| `FLOOR_MIN` (전역 `floorMinC`) | 방3/방2 바닥 차가움 이벤트를 합산(바닥 공유) 3개 이상 | median(이벤트 바닥) + 1.0 °C 를 0.5 °C 단위 올림, 20.0 ~ 23.0 °C clip, 현재 `floorMinC` + 0.25 °C 초과일 때만. 목표 근처 추움 이벤트의 2/3 이상이 바닥 차가움인 방은 `TARGET_UP` 대신 `FLOOR_MIN` |
| `CONTROL_CHECK` (정보만, 적용 없음) | 난방 부족 이벤트 3개 이상이고 목표 근처 추움 이상 | 차단 사유/센서/가동 시간 점검 안내 |

- 신뢰도: LOW(이벤트 6개 미만) / MEDIUM.
- **한계**: "춥다" 방향 단방향 신호이므로 목표를 **낮추는** 제안은 하지 않습니다. 스케줄로 난방을 켜는 HA 자동화도 `WALLPAD` 신호로 집계됩니다.
- API: `GET /api/v1/comfort`(분석 + 최근 이벤트, `?limit`), `POST /api/v1/comfort/reset`(신호 전체 삭제). `/api/v1/status` 에 `comfort` 분석 포함. UI 패널 "쾌적 학습".
- 상수: `COMFORT_WINDOW_DAYS`=14 일, `COMFORT_KEEP_DAYS`=60 일, `COMFORT_MAX_EVENTS`=500, `COMFORT_MIN_EVENTS`=3, `COMFORT_AT_TARGET_DEV_C`=-0.5 °C, `COMFORT_TARGET_MARGIN_C`=0.4 °C, `COMFORT_TARGET_MAX_STEP_C`=1.0 °C, `COMFORT_FLOOR_COLD_C`=21.5 °C, `COMFORT_FLOOR_MIN_LOW_C`=20.0 °C.

### 8-C. Gemini 일일 리포트 (자동 발송, 참고용)

- 설정(UI, `PATCH /api/v1/settings`): `dailyReportEnabled`(bool, 기본 true), `dailyReportHour`(KST 0 ~ 23 시, 기본 8, 범위 밖 422). UI "자동 발송", "발송 시각".
- 하루 1회, 현재 KST 시각이 `dailyReportHour` 이후 4 h 이내일 때 설정된 알림(HA notify / Telegram)으로 발송합니다(`DAILY_REPORT_SEND_WINDOW_H`=4 h, 재시작 후 늦은 발송 방지). 발송 전에 KV `daily_report_last` 에 날짜를 기록하므로 실패해도 반복 발송하지 않습니다. 백그라운드 task 로 실행되어 1 min 제어 루프를 막지 않습니다.
- 내용(결정적, 항상 발송): 기간/커버 시간, 보일러 가동/duty/단펄스/가스비, KPI(목표 ±0.15 °C 대역 비율, 최대 overshoot, 안전 개입 횟수), 방별 평균 실내/실효 목표/목표 - 0.3 °C 미만 시간 비율/난방 시간(min), 바닥/외기 범위, 사용자 난방 요청(최근 24 h 쾌적 신호), 쾌적 제안. 이어서 "Gemini 분석": 모델 `gemini-3.8-flash`(`generateContent` v1beta, `x-goog-api-key` 헤더), 한국어 plain text 8줄 이하(한 줄 요약, 숫자 포함 이상 징후 1~3개, 설정 제안 1~2개 — 로컬 쾌적 제안 평가). thought 파트는 제외. API key 없음/실패 시 요약만 발송(status `NO_API_KEY`/`ERROR`). 본문은 3500 자로 자릅니다(Telegram 4096 자 한도, `DAILY_REPORT_MAX_CHARS`=3500).
- 개인정보: Gemini 전송 전 방 이름을 `ROOM_A`, `ROOM_B`... 로 익명화하고 응답에서 원래 이름으로 되돌립니다.
- API: `POST /api/v1/report/daily?send=false|true` → `{kpis, report(Gemini 텍스트), status, text(전체 조합), context, sent?}`. UI 버튼 "일일 리포트 미리보기 (Gemini)", "리포트 알림 발송".
- 코드: `integrations/google_gemini_advisor.py` 에 `compute_room_daily_stats`, `format_daily_report_text`, `GeminiThermalAdvisor.generate_daily_report` 추가(기존 `generate_report` 유지). `stats_rows_since` 가 `floorTemp`/`floorSrc`/`outdoorTemp` 도 반환. 상수 `DAILY_REPORT_DEFAULT_HOUR`=8, `DAILY_REPORT_SEND_WINDOW_H`=4 h, `DAILY_REPORT_MAX_CHARS`=3500.
- 제어 설정은 절대 변경하지 않습니다.

## 8.1 v2.9.0 변경 사항 (이력)

2026-11-25 00:01~08:12 trace 분석 결과 3가지 문제를 수정했습니다. 바닥 sensor(DS18B20)는 방3에만 있고 방2은 인접, 거실/안방은 바닥 기준이 없습니다.

| 구분 | 문제 | 수정 |
| :--- | :--- | :--- |
| A | 시작 게이트는 바닥 >= cap - 0.5 °C 만 차단, 가동 중에는 바닥 + surge >= cap 에서 차단 -> [cap - surge, cap - 0.5) 에서 시작 1분 만에 차단 (05:56, 06:40 방3, 각 1.1 min 펄스) | 기동 전 가드 `6e` 신설: 바닥 + 최소 5 min 펄스 예상 상승 >= cap - margin 이면 `P000_FLOOR_PREDICTED_LIMIT`. 기존 `cap - 0.5` 규칙(5c) 제거. 레거시 surge 모드는 바닥 + max(0.5, surge) >= cap |
| B | v2.8 추정 OFF 방이 마스터 바닥을 대리 안전값으로 사용했으나 타 방 난방에 반응하지 않음 (01:56~04:22 거실/방2 4회 가열 동안 마스터 바닥 21.4 -> 20.3 °C 하락, 07:40 방3 25.7 °C 도달 시 4개 방 전체 과열 차단) | 바닥 기준 동시 제어 그룹(leader/follower) + 독립 제어(바닥 안전 없음, 방 기준 보호). 대리 판정 제거 |
| C | 잔열 상승을 펄스 길이와 무관한 단일 중앙값으로 학습 -> 1분 펄스(+0.3~0.5 °C)가 1.2 °C 하한까지 끌어내려 8분 펄스(실제 +2.2 °C, 07:46 peak 25.7 °C)를 과소 예측 | 가동 1분당 바닥 상승률 `floorRiseRateCPerMin` 학습 (>= 3 min 펄스, peak 확정만). 0 샘플 기본 0.30 °C/min, 1~2 샘플 max(기본, 최대), 3 이상 75 percentile, clamp 0.05~0.80 °C/min. 예상 peak >= cap - 0.2 °C 에서 차단, 최대 가동 = min(외기 연동, (cap - 0.2 - 시작 바닥)/rate), 5 min 미만이면 시작 안 함 |

- 그룹 수요: idle 에서 가중평균 편차 <= -0.15 °C 면 HEAT, 가열 중 >= 0 면 OFF. 사유 코드 `GROUP_DEMAND_HEAT`, `GROUP_CONTINUE`, `GROUP_TARGET_REACHED`, `GROUP_IDLE`, `GROUP_NO_ROOM_DATA`, `GROUP_MEMBER_WARM_SKIP`, `GROUP_LEADER_UNAVAILABLE`. 방별 P200 단독 판단(`P200_PREDICTIVE_ON/OFF`)은 AUTO 에서 더 이상 쓰이지 않고 예측은 기록/그룹 수요 입력으로만 사용합니다.
- `/api/v1/status` `liveByRoom` 에 `groupRole`, `groupLeader`, `floorRiseRateCPerMin` 추가 (독립 방 `guardFloorTemp` = null).
- 트레이스: `group`, `limits.floorCapC/floorRiseRateCPerMin/heatingStartFloorC`, `lagProfile.floorRiseRateCPerMin/floorRateSamples`, `diagnostics.groupRole/groupLeader`.
- UI: "동시 제어(바닥 공유)"/"독립 제어" 토글, 등록 폼 "바닥 기준 공유 (센서 없는 방)", lag 표 "가동 1분당 상승률", 버튼 "P200 RC 파라미터 보기".
- 알림: "바닥: None°C" 수정(독립 방 "미사용(독립 제어)"), 목표에 실효 목표 병기, 그룹 줄 추가.
- 과열 상수 변경 없음 (실내 24.0 °C / 바닥 25.0 °C).

## 8.2 v2.8.0 변경 사항 (이력)

- 바닥 sensor 없는 방의 바닥 추정 ON/OFF (`floorEstimateEnabled`, 방 등록 폼·방 테이블 토글, `PATCH /api/v1/rooms/{name}`)
- 방 테이블에서 floor offset `±0.1 °C` 조절, RC 피팅 제안 offset 원클릭 적용 (`/status` 의 `suggestedFloorOffsets`)
- 추정 OFF 방: P200 생략·실내 기준 multi-zone 제어, 과열/설정 바닥 상한은 마스터 실측 대리 판정 (`ESTIMATE_OFF`, `guardFloor`) — **v2.9.0 에서 대리 판정 제거**
- 바닥 sensor 방에 추정/offset 설정 시 HTTP 409, offset 범위 −5.0 ~ +5.0 °C (HTTP 422)
- DB `rooms.floor_estimate_enabled` 컬럼 자동 마이그레이션 (기존 방은 ON 유지)

## 9. v2.7.0 변경 사항

| 구분 | 내용 |
| :--- | :--- |
| 배포/보안 | `ports` 제거(Ingress 전용), 앱 IP 필터(403), `arch: amd64` 전용, 베이스 이미지 날짜 태그 고정, 의존성 `==` 고정, `run.sh` 가 `uvicorn --factory` 로 기동 |
| 안전 | 과열 기준 `const.py` 단일 출처(실내 24.0 °C / 바닥 25.0 °C, AUTO/MANUAL 공통), 마스터 바닥 미설정 시 AUTO 차단 + LIVE 전환 409, 수동 ON 보호(센서 누락/stale, 바닥 상한 래치), 제어 회차 `asyncio.Lock` 직렬화, 설정 교차 검증(422), 입력 검증/UI escape |
| 센서 | `last_reported` 기반 보고 주기 학습, 적응형 stale 임계(`last_reported` 기준 간격 6개 이상 + 관측 6 h 이상일 때만 `clamp(2.0 x 최대 보고 간격, 600 s, 7200 s)` (최대 보고 간격 = max(최근 간격, 최근 24 h 내 120 s 이상 공백)). 그 외 7200 s), 센서 시각 기준 선형 보간 기울기(이벤트형 Zigbee 환기 오탐 수정), 바닥 지연 보상(상한 1.0 °C), 보고 주기에 따른 추세 구간 확장 |
| 제어 | 재량 반전에만 채터링 방지, 최소 펄스 5 min, 목표 상태면 명령 생략(`NO_CHANGE`, 30 min 재동기화), 조절기 `current_temperature` 기반 동적 HEAT/OFF 설정온도(HEAT 최대 +3.0 °C, `min_temp`/`max_temp` 준수), 명령 실패 알림, 난방 부족 알림(목표 - 0.3 °C 미달 90 min), 루프 3회 연속 실패 알림 + `/health degraded` |
| 통계/학습 | KPI/보일러 통계/차트를 24 h 시간 기준으로 집계, dry-run 가상 HEAT 제외, `GET /api/v1/chart`, RC 피팅은 마스터 실측 바닥 + 실제 dt + 회차별 외기 + 패턴 탐색(>= 30 샘플), 슬레이브 offset 은 제안만 |
| 외부 연동 | 외기는 Open-Meteo 우선 -> Gemini Search Grounding 폴백, 실패 600 s backoff, API key 는 `x-goog-api-key` 헤더, Telegram `chat_id`(구형 `target` 폴백), TTS `responseModalities: AUDIO` + WAV 포장(라이브러리 전용) |
| 옵션/DB | `history_retention_days`(30), `debug_trace`(true), `debug_trace_days`(7) 추가, 옵션 우선순위 규칙, SQLite WAL + 보존 정리 |
| 디버깅 | `itss.*` 로거, 회전 파일 로그, JSONL 트레이스, `/api/v1/debug/*` 및 UI 버튼 |

---

## 다음 단계 (Next Steps)

1. Dry-Run 으로 1~2일 운전하며 `itss.cycle` 로그와 JSONL 트레이스의 `decision.reason` 분포를 확인하십시오.
2. 마스터 바닥 sensor 의 관측 보고 주기가 UI 에 학습된 뒤(표본 6개 이상, 관측 6 h 이상) stale 임계가 적절한지 점검하십시오.
3. `floor_off_limit_c`(26.5 °C)와 과열 차단 바닥 기준(25.0 °C)의 관계를 의도에 맞게 정리하십시오(현재는 과열 차단이 먼저 작동).
