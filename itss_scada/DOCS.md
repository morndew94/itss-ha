# I.T.S.S. Thermal SCADA 애드온 사용 설명서 (v2.7.0)

온돌 방별 제어 + 단일 마스터 바닥 sensor 하이브리드 제어 애드온입니다. **HA Ingress 전용**이며 외부 포트를 열지 않습니다.

## 1. 설치 및 접속

1. **설정 > 애드온 > 애드온 스토어 > 저장소** 에 `https://github.com/morndew94/itss-ha` 를 추가합니다.
2. **I.T.S.S. Thermal SCADA** 를 설치하고 **사이드바에 표시** 를 켠 뒤 시작합니다.
3. 사이드바 **I.T.S.S. SCADA** 를 열고 방(실내 온도 sensor, `climate` entity)을 등록합니다.
4. **마스터 바닥 온도 sensor**(`master_floor_entity`)를 반드시 설정합니다. 미설정이면 `AUTO` 방은 강제 OFF(`P000_FLOOR_UNCONFIGURED`)이고, 실제 제어(LIVE, `dry_run=false`) 전환은 거부(HTTP 409)됩니다.
5. 시험 운전(`dry_run: true`, 기본값)에서 판단을 확인한 뒤 LIVE 로 전환합니다.

접속 제한: Supervisor/Ingress(`172.30.32.2`)와 loopback 이외의 IP 는 HTTP 403 으로 거부됩니다. 동작 아키텍처는 `amd64` 만 지원합니다.

## 2. 주요 기능

- 방별 목표 온도 `0.1 °C` 단위 설정 (10.0 ~ 27.0 °C)
- 운전 모드 `AUTO` / `MANUAL`(`HEAT`/`OFF`/`HOLD`), 프리셋(`AUTO_ALL`, `AWAY`, `SLEEP`, `PREHEAT`, `VENTILATION`, `ALL_OFF`)
- 하이브리드 다중 방 제어(동시 기동 + 개별 선차단) 또는 `simultaneous` 전략
- 센서 보고 주기 자동 학습: UI 패널 **"센서 보고 주기 감지 현황"**
- 24 h 차트, KPI, 보일러 가동 시간/duty/단펄스/추정 가스비 (시험 운전의 가상 HEAT 제외)
- P400 시간대별 목표 offset 스케줄(UI 편집), RC 피팅 및 슬레이브 floor offset **제안**(자동 적용 안 함)
- 알림: HA `notify`, Telegram(`chat_id`). Gemini 일일 진단 리포트(버튼, API key 필요)

> Google Calendar 예열, Gemini TTS 음성 경보, Google Sheets 내보내기는 코드에 라이브러리로만 존재하며 **현재 동작하지 않습니다.**

## 3. 옵션

| 옵션 | 기본값 | 범위 / 단위 | 설명 |
| :--- | :--- | :--- | :--- |
| `update_interval_sec` | 60 | 30 ~ 3600 s | 제어 주기 |
| `zone_strategy` | `hybrid` | `hybrid`/`simultaneous` | 다중 방 전략 |
| `predictor_model` | `linear_v12` | `linear_v12`/`adaptive_rc` | 예측 모델 |
| `dry_run` | `true` | bool | 명령 미전송 시험 운전 |
| `heat_on_setpoint` | 24.0 | 15 ~ 30 °C | HEAT 시 조절기 기본 설정온도 |
| `heat_off_setpoint` | 15.0 | 5 ~ 25 °C | OFF 시 조절기 기본 설정온도 (ON 값 - 3.0 °C 이하 필수, 위반 시 422) |
| `floor_off_limit_c` | 26.5 | 22 ~ 29 °C | 바닥 OFF 상한 |
| `max_run_cap_min` | 15 | 3 ~ 15 min | 1회 최대 가동 상한 (최소 5 min 보장) |
| `cooldown_min` | 20 | 5 ~ 90 min | 잔열 휴지 기본값 |
| `ventilation_hold_min` | 20 | 5 ~ 60 min | 환기 보호 유지 |
| `history_retention_days` | 30 | 1 ~ 365 일 | 이력 DB 보존 |
| `master_floor_entity` | `""` | entity ID | 마스터 바닥 sensor |
| `outdoor_temp_entity` | `""` | entity ID | 외기 sensor 또는 `weather.*` |
| `gas_krw_per_hour` | 1200 | 100 ~ 10000 KRW/h | 가스비 추정 단가 |
| `ha_token` | `""` | password | 비우면 Supervisor token |
| `notify_service` | `""` | 문자열 | HA notify 서비스/entity |
| `telegram_chat_id` | `""` | 문자열 | Telegram chat_id |
| `gemini_api_key` | `""` | password | 외기 폴백/리포트 (선택) |
| `weather_location` | `서울` | 문자열 | 외기 조회 지역 |
| `log_level` | `info` | debug/info/warning/error | `debug` 면 방별 상세 로그 |
| `debug_trace` | `true` | bool | JSONL 트레이스 기록 |
| `debug_trace_days` | 7 | 1 ~ 30 일 | 트레이스 보존 |
| `db_path` | `/data/itss_scada.db` | 문자열 | DB 경로 |

옵션 우선순위: UI 에서 저장한 값이 우선하지만, 지난 부팅 이후 HA 구성 화면에서 바뀐 옵션은 옵션 값이 이깁니다. `update_interval_sec` 는 옵션으로만 변경됩니다.

## 4. 안전 동작

| 상황 | 동작 |
| :--- | :--- |
| 실내 >= 23.0 °C 또는 바닥 >= 24.0 °C | 즉시 OFF (`P000_OVERHEAT_CUTOFF`, `AUTO`/`MANUAL` 공통) |
| `floor_off_limit_c` > 24.0 °C (기본 26.5 °C) | 과열 차단이 먼저 작동 (`/health` warnings 및 UI 에 경고) |
| 센서 누락 / stale | 강제 OFF. 수동 ON 도 차단 |
| 수동 ON 중 바닥이 `floor_off_limit_c` 도달 | 래치 OFF, `floor_off_limit_c - 1.0 °C` 이하에서 해제 |
| 환기 급락 / 창문 열림 | 난방 중지 후 보호 시간 유지 |
| 채터링 방지 | 재량 반전만 적용: HEAT -> OFF 300 s, OFF -> HEAT 600 s. 최소 펄스 5 min |
| 조절기 상태가 이미 목표와 같음 | 명령 생략(`NO_CHANGE`), 30 min 마다 재동기화 |

센서 stale 임계: `last_reported` 기준 간격 6개 이상 + 관측 6 h 이상일 때만 `clamp(2.0 x 최대 보고 간격, 600 s, 7200 s)` (최대 보고 간격 = max(최근 간격, 최근 24 h 내 120 s 이상 공백)). 그 외 7200 s 입니다. 이벤트형 sensor 가 온도 안정 구간에서 heartbeat 간격까지 조용해지는 경우를 학습한 뒤에만 임계를 줄여 오탐을 막습니다. 이벤트형 sensor 는 센서 보고 시각 기준 선형 보간으로 변화율을 계산하므로 환기 오탐이 줄어듭니다.

알림이 오는 경우: 보일러 ON/OFF 변화, 치명 안전 사유, 조절기 명령 실패(**"조절기 명령 실패"**), 난방 부족(목표 - 0.3 °C 미달이 90 min 지속), 제어 루프 3회 연속 실패.

## 5. 디버깅

- **텍스트 로그**: 애드온 로그 탭 + `/data/logs/itss.log`(2 MB x 5개 회전). 로거 `itss.cycle`, `itss.sensor`, `itss.dispatch`, `itss.weather`. 방별 상세는 `log_level: debug`.
- UI **"디버그 로그 보기"** 버튼 = `GET /api/v1/debug/logs?lines=N`
- **판단 트레이스**: `/data/trace/trace-YYYYMMDD.jsonl` (회차 x 방 1줄, 지난 일자는 `.jsonl.gz` 로 자동 압축). UI **"오늘 판단 트레이스(JSONL)"** 버튼 또는
  - `GET /api/v1/debug/trace/files`
  - `GET /api/v1/debug/trace?date=today|YYYYMMDD&room_name=&limit=`
- 분석 예시:

```python
import pandas as pd
df = pd.read_json("itss-trace-20261124.jsonl", lines=True)
flat = pd.json_normalize(df.to_dict("records"))   # inputs.roomTemp, decision.reason ...
```

주요 키: `inputs`, `dynamics`, `limits`, `lagProfile`, `timersBefore`, `p200`, `multiZone`, `sentinel`, `decision`, `command`, `timersAfter`, `elapsedMs`.

## 6. 문제 해결

| 증상 | 확인 |
| :--- | :--- |
| 난방이 안 켜지고 사유가 `P000_FLOOR_UNCONFIGURED` | `master_floor_entity` 설정 |
| LIVE 전환이 409 로 거부됨 | 마스터 바닥 sensor 설정 필요 |
| 설정 저장이 422 | `heat_off_setpoint <= heat_on_setpoint - 3.0 °C` 확인 |
| 사유가 `P000_OVERHEAT_CUTOFF` 로 반복 | 실내 23.0 °C / 바닥 24.0 °C 도달 여부, 트레이스 `limits` 확인 |
| 접속 403 | Ingress(사이드바)로 접속하십시오 |
| `/health` 가 `degraded` | 로그의 "제어 회차 실패" 항목 확인 |
