# I.T.S.S. 아키텍처 및 데이터 규격서 (v2.7.0)

이 문서는 Home Assistant **Add-on(HA Ingress 전용)** 구조를 기준으로 작성되었습니다. 이전 버전(v1.0)의 `custom_components/itss` 통합구성요소 구조는 더 이상 존재하지 않습니다.

## 1. 구성 요소

| 구성 | 파일 | 역할 |
| :--- | :--- | :--- |
| 서버/엔진 | `itss_scada/app/main.py` | FastAPI 앱(`create_app`), `ItssEngine`(제어 루프), REST API, IP guard middleware |
| 상수 | `app/const.py` | 안전 한계, 타이머, Ingress 허용 IP (과열 기준 단일 출처) |
| HA 클라이언트 | `app/ha_client.py` | 상태 조회(`last_reported` 우선), 서비스 호출, entity/service 목록 |
| 안전 | `protocols/p000_sentinel.py` | 우선순위 기반 안전 인터락 |
| 센서 추적 | `protocols/sensor_tracker.py` | 보고 주기 학습, 적응형 stale, 보간 기울기, 지연 보상 |
| 예측 | `protocols/p200_therm.py` | `linear_v12`, `adaptive_rc` |
| 외기 보정 | `protocols/p300_atmos.py` | 외기 offset, 외기 연동 최대 가동 시간 |
| 다중 방 | `protocols/multi_zone.py` | `hybrid`/`simultaneous` 전략 |
| 학습 | `protocols/thermal_lag_learner.py`, `rc_optimizer.py` | 방별 잔열/지연, RC 피팅 |
| 명령 | `protocols/p100_core.py` | 조절기 0.5 °C 반올림, 반대 명령 간격 |
| 저장 | `app/storage/db.py` | SQLite(WAL) |
| 관측 | `app/debug_trace.py` | 회전 파일 로그, JSONL 트레이스 |

기동: `run.sh` -> `uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8099`. 제어 루프는 FastAPI `lifespan` 안의 백그라운드 task 로 `update_interval_sec`(기본 60 s) 마다 `run_control_cycle()` 을 호출합니다.

### 접근 제어

`ingress_ip_guard` middleware 가 모든 요청의 클라이언트 IP 를 검사합니다. 허용: `172.30.32.2`, `127.0.0.1`, `::1` (+ 환경변수 `ITSS_ALLOWED_IPS` 추가 IP, `"*"` 면 필터 해제). 그 외는 HTTP 403(`Forbidden: HA Ingress only`). `config.yaml` 에는 `ports` 가 없습니다.

---

## 2. 제어 파이프라인 (회차당 순서)

`run_control_cycle()` 은 `asyncio.Lock`(`cycle_lock`)을 잡고 `_run_cycle_locked()` 를 실행합니다. 수동 실행, 프리셋, 방 모드 전환, 방 삭제, 조절기 테스트도 같은 lock 을 사용하여 직렬화됩니다.

1. **마스터 바닥 sensor 읽기**: `masterFloorEntity` 값/보고 시각/경과/시각 출처를 읽고 `sensor_tracker.observe()` 로 트랙에 반영합니다. stale 임계, 회귀 기울기(`vFloor`), 10분(가변) 추세, 5 min 변화량, 지연 보상(`floorLatency`)을 계산합니다. 미설정이면 `floorSrc=UNCONFIGURED`.
2. **외기 판독**: HA 센서(`outdoorTempEntity`, `weather.*` 이면 attributes 의 `temperature`/`wind_speed`) -> Open-Meteo -> Gemini Search Grounding -> 시간대 폴백. 소스가 바뀔 때 `itss.weather` INFO 로그.
3. **P300 보정**: `atmos_offset = min(0.25, max(0, -outdoor x 0.015) + (풍속 >= 6.0 m/s 이면 0.05))` °C. 외기 미확인 시 시간대 프로파일. 외기 연동 최대 가동 시간(<= -5 °C: 15 min, <= 0 °C: 13 min, <= 5 °C: 11 min, <= 10 °C: 9 min, 그 외 7 min)을 `maxRunCapMin` 으로 제한합니다.
4. **P400 + 프리셋 offset**: DB 스케줄 슬롯(`active_schedule_offset(hour)`) + 프리셋 offset(`AWAY` -2.0 °C, `SLEEP` -0.3 °C, `PREHEAT` +0.2 °C).
5. **주기 작업**: 30 회차마다(또는 프로파일 없을 때) 잔열/지연 학습 갱신, 60 회차마다 이력/트레이스 보존 정리.
6. **방별 telemetry 수집**: 실내 sensor 읽기 + `observe`, `climate` 상태, 보일러 가동 추정(`detect_boiler_running`), 바닥값(`MEASURED`/`ESTIMATED`/`ESTIMATE_OFF`/`MISSING`/`UNCONFIGURED`, `_room_floor()` 가 제어용 `effFloor` 와 안전 판정용 `guardFloor` 를 함께 결정 — 추정 OFF 방은 `effFloor=None`, `guardFloor=마스터 실측`, P200 생략, sentinel `floor_is_proxy=True` 로 4a 목표연동 상한·4b·5c·6d 생략), 창문 접점, 기울기/추세, `effTarget = targetTemp + atmos + schedule + preset`, P200 예측(실내/바닥값 모두 있을 때).
7. **다중 방 계획(`MultiZoneController`)**: 마스터 바닥값이 있을 때만 `AUTO` 방에 대해 수립. 바닥 실측이 없으면 계획을 만들지 않습니다(가짜 바닥값 사용 금지).
8. **방별 판단 `_decide_room`** (3장 우선순위) -> 명령 설정온도 계산 -> `_dispatch` -> 알림 -> 이력 저장 -> 로그/트레이스.
9. **상태 저장**: `room_timers`, `sensor_tracks`(사용 중인 sensor 만 유지) 를 KV 에 저장. 방 하나가 실패해도 `INTERNAL_ERROR` 회차 기록을 남기고 나머지 방은 계속 처리하며, 모든 방이 실패하면 회차 실패로 집계합니다.

### 센서 보고 주기 추적 (`sensor_tracker.py`)

| 항목 | 동작 |
| :--- | :--- |
| 시각 출처 | HA `last_reported`, 없으면 `last_updated`, 둘 다 없으면 현재 제어 시각(`poll`). 값 또는 보고 시각이 바뀌면 새 보고로 간주 |
| 보관 | 보고 간격 최대 24개, 샘플 최대 60개/120 min. 간격은 1 s ~ 6 h 범위만 기록 |
| 대표 주기 | `typicalInterval` = 간격 중앙값 (>= 3개), `maxInterval` = 최대값 |
| 적응형 stale | `tsSource == last_reported`, 간격 >= 6개, 관측 기간(`firstReportAt`~`lastReportAt`) >= 6 h 일 때만 `clamp(2.0 x maxInterval, 600 s, 7200 s)`. `maxInterval` = max(최근 24개 간격, 최근 24 h `longGaps`(120 s 이상 공백)). 그 외 7200 s |
| 변화량/기울기 | 센서 보고 시각 기준 선형 보간(`value_at`, `delta_over`), 최근 보고 쌍 변화율(`recent_rate_per_min`, 3 min 초과 시 0), 회귀 기울기(span >= 8 min, 점 >= 3개, 아니면 0 처리) |
| 구간 확장 | `adaptive_window_min`: 대표 주기가 길면 구간을 늘려 보고점 2~3개 확보 (기울기 구간 기본 16 min, 추세 10 min, 상한 45/30 min) |
| 지연 보상 | `latency_rise_c = min(1.0 °C, 상승 기울기 x min(마지막 보고 후 경과, max(60 s, 대표 주기)))`. 가열 중 바닥 상한 판정에 가산 |

효과: 이벤트형(Zigbee) sensor 가 수 분 누적된 변화를 한 번에 보고해도 제어 주기 기준 급락으로 오인(환기 오탐)하지 않습니다.

---

## 3. 방별 판단 우선순위 (`_decide_room`)

위에서부터 먼저 해당되는 항목이 결정합니다.

| 순서 | 조건 | 결과 (`safetyReason`) | 비고 |
| :--- | :--- | :--- | :--- |
| 1 | 실내 >= 24.0 °C 또는 바닥 >= 25.0 °C | OFF, `P000_OVERHEAT_CUTOFF` | `const.py`. `AUTO`/`MANUAL` 공통. 잔열 휴지 시작 |
| 2 | `MANUAL` | `manualAction` 수행, `P000_MANUAL_OVERRIDE` | `HEAT` 일 때만 최소 안전 조건 검사: `P000_FLOOR_UNCONFIGURED`, `P000_SENSOR_MISSING`, `P000_SENSOR_STALE`, `P000_FLOOR_LIMIT_OFF`(래치, 1.0 °C hysteresis) 이면 OFF |
| 3 | `AUTO` + 마스터 바닥 미설정 | OFF, `P000_FLOOR_UNCONFIGURED` | |
| 4 | `SentinelProtocol.evaluate()` | 아래 4.1 | `force_off` -> OFF, `hold` -> HOLD |
| 5 | P200 / Multi-zone | `P200_PREDICTIVE_ON/OFF`, `HYBRID_*`, `SIMULTANEOUS_*` | 전략 `hybrid` + 예측 있음이면 P200 우선 |
| 6 | 재량 반전 채터링 방지 | HOLD, `P000_ANTI_CHATTER` | 안전 차단이 아닐 때만. HEAT -> OFF < 300 s, OFF -> HEAT < 600 s 이면 직전 명령 유지 |

### 3.1 P000 SENTINEL 내부 우선순위

| 순서 | 검사 | 코드 |
| :--- | :--- | :--- |
| 1 | 수동 override 플래그 (엔진이 항상 `False` 로 전달. 수동은 위 2 단계에서 별도 처리) | `P000_MANUAL_OVERRIDE` |
| 2 | 실내/바닥 값 누락 | `P000_SENSOR_MISSING` |
| 3 | sensor 경과 > sensor별 stale 임계 | `P000_SENSOR_STALE` |
| 4 | 과열 차단 (1 단계와 동일 기준) | `P000_OVERHEAT_CUTOFF` |
| 5 | 바닥 + (가열 중이면) 지연 보상 >= 상한 (`min(floorOffLimitC, 목표 연동 상한)`) | `P000_FLOOR_LIMIT_OFF` |
| 6 | 가열 중 예상 바닥 peak(잔열 surge, 기울기 x 10 min) >= 상한, 또는 비가열 중 바닥이 상한 - 0.5 °C 이내 | `P000_FLOOR_PREDICTED_LIMIT` |
| 7 | 1회 연속 가동 >= 최대 가동 시간 | `P000_MAX_RUN_CUTOFF` |
| 8 | 잔열 휴지 남음 | `P000_RESIDUAL_COOLDOWN` |
| 9 | 창문 접점 열림 | `P000_WINDOW_OPEN` |
| 10 | 환기 급락: 최근 보고 변화율 <= -0.15 °C/min, 또는 5 min 실내 <= -0.5 °C(바닥 하락 > -0.2 °C), 또는 5 min <= -0.8 °C | `P000_VENTILATION_PROTECT` (보호 시간 래치) |
| 11 | 환기 보호/창문 닫힘 후 10 min 회복 대기 | `P000_VENTILATION_HOLD` |
| 12 | (비가열 시) 바닥 잔열 방출 중/실내 반등 중 | `P000_FLOOR_RESIDUAL_ACTIVE` |
| 13 | Sentinel 내부 최소 가동/휴지 dwell | `P000_ANTI_CHATTER` |
| - | 모두 통과 | `P000_OK` |

- 목표 연동 바닥 상한: `min(floorOffLimitC, effTarget + margin)`. margin = 4.5 °C(외기 <= -5 °C), 3.0 °C(외기 > 5 °C), 그 외 3.5 °C.
- `floorOffLimitC`(기본 26.5 °C)가 과열 바닥 기준(25.0 °C)보다 높으면 과열 차단이 먼저 작동합니다. `/health` 의 `warnings` 로 알립니다.
- 1회 최대 가동 시간은 외기 연동 값에서 학습된 `recommendedRunTrimMin` 을 빼고, 바닥 여유가 적으면 축소(최소 5 min)합니다.

### 3.2 예측 모델 (P200)

- `linear_v12`: `F30 = floor + vFloor x horizon`, `Room30 = room + vRoom x horizon` (horizon 기본 30 min, 잔열 학습값에 따라 가변). 급변(`|vRoom| > 0.08 °C/min` 또는 `|vFloor| > 0.20 °C/min`) 시 `TRANSIENT_SKIP`. 제어 대역은 목표 +/- 0.15 °C.
- `adaptive_rc`: 2-node RC(바닥/실내/외기) 전진 적분으로 coast/heat 궤적을 비교합니다. 파라미터는 `fitted_rc_params`(KV)가 `READY` 이면 사용, 아니면 기본값.
- 잔열 학습 상태: `READY`, `LEARNING`, `INSUFFICIENT_SAMPLES`, `TRANSIENT_SKIP`.

---

## 4. 명령 전송 (`_dispatch`)

1. **설정온도 계산** (`compute_command_setpoint`): HEAT 는 `heatOnSetpoint`, 조절기 `current_temperature + 0.5` 가 이를 넘으면 0.5 단위 올림으로 상향(최대 +3.0 °C, `max_temp` 이내). OFF 는 `heatOffSetpoint`, 조절기 현재온도 - 1.0 °C 보다 높으면 내림으로 하향(`min_temp` 이상).
2. **전송 생략 판정**: `dryRun` 이거나 HA 미연결이면 `DRY_RUN`(미전송). 조절기 상태가 이미 목표와 같으면 `NO_CHANGE`(`control_issued_at=null`)이며 같은 명령은 30 min 마다 1회 재동기화합니다.
3. **HEAT**: 모드가 `heat` 가 아니면 `climate.set_hvac_mode(heat)` -> 0.8 s 대기 -> `climate.set_temperature`.
4. **OFF**: 설정온도가 현재온도 - 0.4 °C 이상이고 `hvac_modes` 에 `off` 가 있으면 `set_hvac_mode(off)`, 아니면 `set_temperature`(실패 시 `set_hvac_mode(off)` 폴백).
5. **재조회**: 2.0 s 후 상태를 다시 읽어 `deviceModeEvent`, `combustionConfirmed`(설정온도 > 현재온도 추정, 실연소 계측 아님) 기록. 결과 `SENT`.
6. **실패**: `FAILED`, `executionStatus=FAILED`. 알림은 "조절기 명령 실패"(30 min 내 중복 억제).

알림 정책: 보일러 ON/OFF 방향이 바뀔 때, 치명 사유(`CRITICAL_REASONS`)가 바뀔 때, 명령 실패, 난방 부족(실내 < 실효 목표 - 0.3 °C 가 90 min 지속, 1회), 제어 루프 3회 연속 실패(복구 시 복구 알림).

---

## 5. 영속화 (SQLite, WAL)

| 테이블 | 주요 컬럼 |
| :--- | :--- |
| `rooms` | `room_name`(PK), `room_temp_entity`, `climate_entity`, `window_sensor_entity`, `has_floor_sensor`, `floor_estimate_enabled`(v2.8.0, 기본 1), `floor_offset_c`, `target_temp`, `control_mode`, `manual_action`, `weight`, `updated_at` |
| `schedules` | `slot_id`(PK), `label`, `start_hour`, `end_hour`, `offset_c`, `enabled` (기본 4개 슬롯) |
| `kv_store` | `key`(PK), `value_json` |
| `cycles` | `cycle_id`(PK), `room_name`, 시각 컬럼(`scheduled_at`, `execution_started_at`, `control_issued_at`, `control_confirmed_at`, `observed_at`, `saved_at`), `execution_status`, `command_action`, `safety_reason`, `room_temp`, `floor_temp`, `target_temp`, `payload_json`, v2.7.0 추가: `combustion_confirmed`, `dry_run`, `effective_target`, `outdoor_temp`, `floor_sensor_source` |

- 인덱스: `idx_cycles_room_time(room_name, execution_started_at DESC)`, `idx_cycles_time(execution_started_at)`. 통계/차트 쿼리는 `payload_json` 파싱 없이 컬럼만 사용합니다.
- 기존 DB 는 `ALTER TABLE` 로 안전하게 마이그레이션합니다.
- 보존: `history_retention_days` 이전 `cycles` 삭제(부팅 시 + 60 회차마다), 트레이스 파일은 `debug_trace_days`.

**KV 키**

| 키 | 내용 |
| :--- | :--- |
| `settings` | 현재 설정(UI 저장값 포함) |
| `options_snapshot` | 부팅 시점의 애드온 옵션 값. 다음 부팅에서 옵션 변경 여부 판단에 사용 |
| `active_preset` | 활성 프리셋 |
| `room_timers` | 방별 `heatingStartedAt`, `cooldownUntil`, `ventilationUntil`, `lastCommandAction`, `lastSwitchAt`, `lastDispatchAt/Action`, 알림 상태, `manualFloorLatched`, `underheatSince` 등 (재시작 후 잔열 휴지 복원) |
| `sensor_tracks` | entity별 `lastReportAt`, `lastValue`, `tsSource`, `reportCount`, `intervals`, `samples`, `missingSince` |
| `thermal_lag_profiles` | 방별 잔열/지연 학습 결과 |
| `fitted_rc_params` | RC 피팅 결과(`status=READY` 일 때만 저장) |

**옵션 우선순위** (`resolve_settings`): 기본값 <- 애드온 옵션 <- UI 저장값. 단 `options_snapshot` 과 비교하여 지난 부팅 이후 옵션이 바뀐 항목은 옵션 값이 우선합니다(snapshot 이 없는 업그레이드 첫 부팅은 UI 저장값 유지). `update_interval_sec` 는 옵션 전용입니다.

---

## 6. 이력 스키마 (`CycleRecord`, camelCase payload)

모든 시각은 ISO-8601 KST(`+09:00`) 문자열입니다. `protocolVersion` 은 `ITSS-v2.7`.

| 필드 | 설명 |
| :--- | :--- |
| `cycleId` | `YYYYMMDD-HHMMSS-<seq 5자리>-<room>` (같은 초에도 유일) |
| `roomName`, `protocolVersion` | 방 이름, 프로토콜 버전 |
| `scheduledAt`, `executionStartedAt` | 회차 예약/실행 시작 |
| `sensorMeasuredAtRoom`, `sensorMeasuredAtFloor` | 센서 보고 시각 |
| `controlIssuedAt`, `controlConfirmedAt`, `observedAt`, `savedAt` | 명령 전송, 수락 확인, 재조회 관측, 저장 |
| `executionStatus` | `EXECUTED`, `SKIPPED_SAFETY`, `FAILED` (`DELAYED` 는 스키마 정의만 존재) |
| `delaySec`, `opposingCommandIntervalSec` | 지연, 직전 반대 명령(HEAT<->OFF)과의 간격(s) |
| `roomTemp`, `floorTemp`, `floorSensorSource` | 실내/바닥(실효값) 온도, `MEASURED`/`ESTIMATED`/`ESTIMATE_OFF`/`MISSING`/`UNCONFIGURED` (`ESTIMATE_OFF` 는 `floorTemp=null`, `diagnostics.guardFloor` 에 대리값) |
| `targetTemp` | 방 설정 목표 |
| `effectiveTargetTemp` | **(v2.7.0)** 외기/스케줄/프리셋 보정 후 실효 목표 |
| `outdoorTemp` | **(v2.7.0)** 회차 당시 외기 (RC 피팅 입력) |
| `predictedRoom30m`, `predictedFloor30m`, `predictorModel`, `residualLearningStatus` | P200 결과 (`MANUAL` 방은 `manual`) |
| `safetyReason`, `commandAction` | 판단 사유, `HEAT`/`OFF`/`HOLD` |
| `commandSetpoint`, `deviceModeEvent`, `combustionConfirmed` | 조절기 설정온도, 관측 모드, 가동 추정 |
| `dryRun` | **(v2.7.0)** 시험 운전 회차 여부 (통계/학습에서 제외 판단에 사용) |
| `dispatchResult` | **(v2.7.0)** `SENT`, `NO_CHANGE`, `DRY_RUN`, `FAILED`, `NONE` |
| `diagnostics` | **(v2.7.0)** `floorReportIntervalSec`, `floorStaleThresholdSec`, `floorLatencyC`, `roomReportIntervalSec`, `roomStaleThresholdSec`, `vRoomRate`, `vRoomSmooth`, `vFloor`, `drop5mRoom`, `trend10mRoom`, `trend10mFloor`, `dispatchCalls`, `antiChatterBlocked`, 실패 시 `error` |

### 통계/학습 규칙

- KPI(`kpis`), 보일러 통계(`boilerStats`), 차트는 최근 **24 h(시간 기준)** 행으로 계산합니다(행 개수 기준 아님). `statsCoverageHours` 로 실제 커버 시간을 제공합니다.
- 보일러 ON 판정: `combustionConfirmed == true` 또는 (`commandAction == HEAT` 이고 `dryRun` 이 아님). 같은 분의 여러 방은 보일러 1대로 합산하며 실제 슬롯 간격(최대 1주기)으로 적산합니다. 단펄스는 연속 가동 10 min 미만 횟수입니다.
- 차트: `GET /api/v1/chart?room_name=&hours=1~168&max_points=50~2000` (구간 평균 축약, 보일러 ON 은 구간 내 하나라도 ON 이면 ON).
- RC 피팅(`POST /api/v1/calibrate/auto`): 마스터 방(`hasFloorSensor`)의 실측 바닥만 사용, 인접 회차의 실제 dt(공칭 주기의 0.5 ~ 2.5 배만 허용), 회차별 `outdoorTemp`(없으면 5.0 °C 폴백 후 카운트), 환기/창문 회차 제외, 패턴 탐색, 최소 30 샘플. 슬레이브 floor offset 은 `suggestedFloorOffsets` 로 **제안만** 하며 `floorOffsetsApplied=false`.

---

## 7. 트레이스 / 로그 스키마

### 7.1 로그

`itss` 로거 -> stdout + `<data>/logs/itss.log`(RotatingFileHandler, 2 MB x 5개). 하위 로거: `itss.cycle`, `itss.sensor`, `itss.dispatch`, `itss.weather`.

### 7.2 JSONL 트레이스 (`<data>/trace/trace-YYYYMMDD.jsonl`, 지난 일자는 `.jsonl.gz` 압축)

회차 x 방 1줄. 최상위 키:

| 키 | 내용 |
| :--- | :--- |
| `ts`, `cycleId`, `room`, `mode`, `manualAction` | 식별 정보, 운전 모드 |
| `cycle` | 회차 공통 컨텍스트: `seq`, `scheduledAt`, `dryRun`, `strategy`, `predictor`, `outdoorTemp`, `windSpeedMs`, `weatherSource`, `atmosOffsetC`, `schedule`, `preset`, `outdoorMaxRunMin`, `floorOffLimitC`, `floor`(마스터 바닥 sensor 값/보고 시각/경과/주기/stale/기울기/추세/지연) |
| `inputs` | `roomTemp`, `roomReportAt`, `roomAgeSec`, `roomTsSource`, `roomIntervalSec`, `roomMaxIntervalSec`, `roomStaleSec`, `floorTemp`, `floorSrc`, `floorOffsetC`, `floorAgeSec`, `floorStaleSec`, `targetTemp`, `effTarget`, `climate`, `isHeating`, `combustion`, `windowOpen` |
| `dynamics` | `vRoomSmooth`, `vRoomRate`, `vFloor`, `drop5mRoom`, `drop5mFloor`, `trend10mRoom`, `trend10mFloor`, `floorLatencyC` |
| `limits` | `overheatRoomC`, `overheatFloorC`, `effFloorLimitC`, `learnedFloorSurgeC`, `baseMaxRunMin`, `roomMaxRunMin`, `cooldownMin`, `pollIntervalMin` |
| `lagProfile` | `status`, `sampleCount`, `floorResidualRiseC`, `roomResidualRiseC`, `recommendedCooldownMin`, `recommendedHorizonMin`, `recommendedRunTrimMin` |
| `timersBefore` / `timersAfter` | 판단 전: `runMin`, `cooldownRemainMin`, `ventilationRemainMin`, `lastCommand`, `secsSinceSwitch` / 판단 후: `heatingStartedAt`, `cooldownUntil`, `ventilationUntil`, `lastSwitchAt`, `underheatSince` |
| `p200` | `model`, `action`, `predRoom`, `predFloor`, `effTarget`, `learning`, `residualRiseC`, `diag` (없으면 `null`) |
| `multiZone` | `action`, `reason` (없으면 `null`) |
| `sentinel` | `reason`, `forceOff`, `hold`, `message` (Sentinel 미실행이면 `null`) |
| `decision` | `action`, `reason`, `executionStatus`, `antiChatterBlocked` |
| `command` | `setpoint`, `dispatch`, `calls`, `issuedAt`, `confirmedAt`, `observedAt`, `deviceModeAfter`, `runningAfter`, `opposingIntervalSec` |
| `elapsedMs` | 방 처리 소요 시간(ms) |

트레이스 기록 실패는 제어를 중단시키지 않고 1회 경고만 남깁니다. 날짜 파라미터는 `today` 또는 `YYYYMMDD` 형식만 허용되어 경로 조작이 불가능합니다.

---

## 8. 외부 연동과 라이브러리 전용 모듈

| 대상 | 상태 |
| :--- | :--- |
| Open-Meteo (기상/geocoding) | 활성. 외기 1순위 API |
| Gemini Search Grounding | 활성(폴백, API key 필요). 실패 시 600 s backoff, key 는 `x-goog-api-key` 헤더 |
| Gemini 일일 진단 리포트 | 활성(UI 버튼, `POST /api/v1/report/daily`) |
| HA `notify`, `telegram_bot`(`chat_id`, 구형 `target` 폴백) | 활성 |
| `GoogleCalendarAdvisor`, `GeminiTTSAlert`, `format_cycle_for_sheets`, `CircadiaProtocol` | **라이브러리 전용. 제어 루프에 연결되지 않음** (P400 은 DB 스케줄 사용) |

---

## 다음 단계 (Next Steps)

1. 실운전 후 JSONL 트레이스의 `sentinel.reason` / `decision.reason` 분포를 집계하여 어떤 보호 로직이 주로 개입하는지 확인하십시오.
2. 라이브러리 전용 모듈을 활성화할 계획이 있다면 별도 이슈로 연결 지점(제어 루프/알림 흐름)을 정의하십시오.
3. `DELAYED` 상태는 스키마에만 있고 현재 코드에서 설정되지 않으므로, 사용하지 않을 경우 정리를 검토하십시오.
