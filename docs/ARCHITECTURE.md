# I.T.S.S. 아키텍처 및 데이터 규격서 (v2.9.1)

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
| 쾌적 학습 | `app/comfort.py` | (v2.9.1) 추움 신호 기록/분석, 제안 전용 (3.0.4) |

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
6. **방별 telemetry 수집**: 실내 sensor 읽기 + `observe`, `climate` 상태, 보일러 가동 추정(`detect_boiler_running`), 바닥값(`MEASURED`/`ESTIMATED`/`ESTIMATE_OFF`/`MISSING`/`UNCONFIGURED`, `_room_floor()` 가 제어용 `effFloor` 를 결정. **v2.9.0**: `ESTIMATE_OFF`(UI 표기 "독립 제어") 방은 `effFloor=None` 이며 바닥값도 안전 바닥도 없음 — v2.8 의 마스터 바닥 대리(proxy) 판정은 제거됨. 마스터 바닥 과열/센서 누락/미설정의 영향을 받지 않음), 창문 접점, 기울기/추세, `effTarget = targetTemp + atmos + schedule + preset`, P200 예측(실내/바닥값 모두 있을 때. 바닥 방은 trace 기록 및 그룹 수요의 `projected_coast` 용).
7. **다중 방 계획(`MultiZoneController`)**: 마스터 바닥값이 있을 때만 `AUTO` 방에 대해 수립. 바닥 실측이 없으면 계획을 만들지 않습니다(가짜 바닥값 사용 금지). **v2.9.0**: 바닥 기준 그룹(3.3)은 별도 그룹 수요로 판단하고, 하이브리드 계획은 독립 제어 방끼리만 수립합니다.
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
| 2 | `MANUAL` | `manualAction` 수행, `P000_MANUAL_OVERRIDE` | `HEAT` 일 때만 최소 안전 조건 검사: `P000_FLOOR_UNCONFIGURED`, `P000_SENSOR_MISSING`, `P000_SENSOR_STALE`, `P000_FLOOR_LIMIT_OFF`. **(v2.9.1)** 바닥 기준 방은 상승률 기반 예측(`P000_FLOOR_PREDICTED_LIMIT`) 적용, 추정 방은 마스터 이상 바닥으로 판정. 바닥 한계/예상 한계/과열, 외부 OFF(`MANUAL_HEAT_CANCELLED`), 1회 최대 경과(`MANUAL_HEAT_TIMEOUT`, `manualHeatMaxMin`)이면 수동 요청 종료 + AUTO 복귀 |
| 3 | `AUTO` + 마스터 바닥 미설정 | OFF, `P000_FLOOR_UNCONFIGURED` | |
| 4 | `SentinelProtocol.evaluate()` | 아래 4.1 | `force_off` -> OFF, `hold` -> HOLD |
| 4b | **(v2.9.1)** `AUTO` + 외부 조작 가동 유지(external boost) 중 | 유지 시 명령 없음(`EXTERNAL_BOOST`), 종료 시 OFF(`EXTERNAL_BOOST_END`), 외부 OFF 면 종료(`EXTERNAL_BOOST_CANCELLED`) | 감지: 직전 관측 비가열 + 현재 관측 가열 + ITSS 마지막 명령이 HEAT 아님(취소는 boost 중 가열 관측 후에만 인정). Sentinel 안전(과열/창문/환기/센서/바닥 예상 peak)은 유지, 잔열 휴지/최대 가동은 우회. 독립 방은 단독 boost + multi-zone 제외. 방3/방2 는 두 방 연동: 일반 그룹 경로(leader 바닥 안전, follower 복사, warm skip 우회, 동일 종료 시각/휴지) |
| 5 | 그룹 수요 / Multi-zone | `GROUP_*`(그룹 방), `HYBRID_*`, `SIMULTANEOUS_*`(독립 방) | v2.9.0: 방별 P200 단독 분기(`P200_PREDICTIVE_ON/OFF`)는 AUTO 판단에 더 이상 쓰이지 않음. P200 예측은 계산/trace 기록되고 그룹 수요에 반영 |
| 5b | **(v2.9.1)** 바닥 최저 온도 `floorMinC` > 0, 바닥 기준 그룹 | `GROUP_FLOOR_MIN_HEAT` / `GROUP_FLOOR_MIN_CONTINUE` / `GROUP_FLOOR_MIN_REACHED` | 그룹 비가열 + leader 바닥 < floorMinC + 가중 실내 편차 < +0.3 °C 이면 시작, 예상 peak < floorMinC + 1.0 °C 동안 유지. 독립 방 무관 |
| 6 | 재량 반전 채터링 방지 | HOLD, `P000_ANTI_CHATTER` | 안전 차단이 아닐 때만. HEAT -> OFF < 300 s, OFF -> HEAT < 600 s 이면 직전 명령 유지 |

### 3.0 외부 조작 가동 유지 (v2.9.1, external boost)

- 배경(2026-11-25 field trace): 월패드/다른 HA 자동화로 설정온도를 24 °C 로 올리면 ITSS 가 약 1 분 안에 15 °C 로 되돌리고 0 min 가동 후 30 min 잔열 휴지를 시작했습니다.
- 감지 조건: `AUTO` 방, 직전 회차 관측 비가열, 현재 회차 관측 가열, ITSS 마지막 명령이 HEAT 아님. 오탐 방지: ITSS 의 OFF 가 적용되지 않아 직전 관측도 가열이면 외부로 보지 않고 OFF 재전송. ITSS 자신의 HEAT, `MANUAL` 방은 감지 제외.
- 동작: `externalBoostMin`(기본 15, 0 ~ 60, 0 = 기존 동작) 분 동안 명령 없이 가동 유지(외부 설정온도 유지) -> OFF 전송 + 잔열 휴지(가동 5 min 이상일 때만). boost 중 외부 OFF 면 재가열하지 않음.
- 안전: 과열 차단(실내 24 °C / 바닥 25 °C), 창문, 환기 보호, 센서 누락/stale, 바닥 기준 방(센서 방, 추정 방)의 상승률 기반 예상 peak 차단(`P000_FLOOR_PREDICTED_LIMIT`)은 적용. 잔열 휴지, 1회 최대 가동, 그룹/multi-zone 수요는 우회.
- 방3 · 방2 연동: 바닥을 공유하는 두 방(`AUTO`, 실측/추정 바닥) 중 한 방이라도 외부에서 켜면 **두 방이 같은 종료 시각**(`externalBoostMin`)으로 함께 가동합니다. 일반 그룹 경로를 사용하므로 leader(방3, 실측 바닥)가 바닥 안전(상승률 기반 예상 peak 차단, 과열 등)을 판단하고 follower 가 따라가며, 타이머/휴지는 동기화됩니다. boost 중 follower 의 warm skip 은 사용자 의도를 우선해 우회합니다. 켠 방은 외부 설정온도를 유지하고 ITSS 가 다른 방에 HEAT 를 보냅니다.
- 한 방을 외부에서 끄면 연동 boost 가 두 방 모두 취소되고(`EXTERNAL_BOOST_CANCELLED`), 종료 시 두 방 모두 OFF(`EXTERNAL_BOOST_END`) + 같은 잔열 휴지에 들어갑니다. 연동 boost 회차에서 최대 가동/휴지는 우회하지만 바닥 안전은 우회하지 않습니다. 취소 판정은 boost 중 실제 가열이 관측된 뒤에만 인정하며, 연동 방의 ITSS HEAT 가 아직 적용 전이면 취소 대신 HEAT 를 재전송합니다. 독립 방(거실/안방)은 단독 boost 이며 multi-zone 계획에서 계속 제외됩니다.
- 사유 코드: `EXTERNAL_BOOST`, `EXTERNAL_BOOST_END`, `EXTERNAL_BOOST_CANCELLED`.

### 3.0.1 ITSS 수동 HEAT 1회 요청화 (v2.9.1)

- 배경(2026-11-25 trace): 수동 HEAT 가 거실 약 154 min 연속 가동(독립 방이라 실내 과열 24 °C 만 정지 조건, 실내는 23.4 °C 까지만 상승), 방3 36.9 min 가동에 바닥 최고 27.1 °C(cap 25 °C).
- 신규 설정 `manualHeatMaxMin`(기본 15, 범위 5 ~ 60 min, UI "ITSS 수동 ON 1회 최대 (분, 경과 시 자동 복귀)"). 경과 시 OFF 전송, 방을 AUTO(controlMode AUTO, manualAction HOLD)로 복귀시키고 5 min 이상 가동했으면 잔열 휴지를 시작합니다. 사유 `MANUAL_HEAT_TIMEOUT`("수동 난방 설정 시간 경과(자동 중지 · 자동 제어 복귀)").
- 바닥 기준 방(방3 실측, 방2 추정)의 수동 HEAT 에도 AUTO 와 같은 상승률 기반 바닥 예측을 적용합니다. 시작 전 바닥 + 상승률 x 5 min >= 바닥 cap - 0.2 °C 이면 차단, 가동 중 가열 시작 바닥 + 상승률 x (가동 + poll) >= cap - 0.2 °C 이면 차단합니다.
- 수동 HEAT 중 바닥 한계(`P000_FLOOR_LIMIT_OFF`), 예상 한계(`P000_FLOOR_PREDICTED_LIMIT`), 과열(`P000_OVERHEAT_CUTOFF`)이 발생하면 수동 요청을 **종료하고 AUTO 로 복귀**합니다(이전에는 MANUAL 로 남아 바닥이 한계보다 1 °C 식으면 스스로 재가열).
- 추정 방(방2, floorOffsetC 음수)은 수동 모드에서도 실측 마스터 바닥보다 낮지 않은 바닥(공유 바닥)으로 판정합니다. 2026-11-25 22:27 방2 단독 재가열 사례를 수정합니다.
- 수동 HEAT 중 월패드/자동화가 조절기를 외부에서 끄면 24 °C 를 다시 지시하지 않고 수동 요청을 종료하며 AUTO 로 복귀합니다. 사유 `MANUAL_HEAT_CANCELLED`("수동 난방 중 외부에서 끔(자동 제어 복귀)").
- Telegram: ON 알림에 "수동 난방: HH:MM까지 최대 N분 후 자동 제어 복귀", 종료 알림에 "수동 난방 종료 → 자동 제어(AUTO) 복귀". `/api/v1/status` `liveByRoom.manualHeatUntil`, 방 카드 배지 "수동 가동 (HH:MM 자동 복귀)". 트레이스 diagnostics `manualEnded`.

### 3.0.2 바닥 최저 온도 (v2.9.1, floor comfort minimum)

- 설정 `floorMinC`(기본 0 = 사용 안 함, 범위 0 ~ 23 °C, UI "바닥 최저 온도 (°C, 0=사용 안 함 · 방3/방2 바닥 기준)"). 바닥 기준 그룹(방3/방2)에만 적용되며 독립 방(거실/안방)은 바닥이 없어 영향이 없습니다.
- 시작: 그룹이 가열 중이 아니고 leader 바닥 < `floorMinC` 이며 그룹 가중 실내 편차 < +0.3 °C(`FLOOR_MIN_AIR_MARGIN_C`, 실내 과열 방지)이면 그룹 HEAT(`GROUP_FLOOR_MIN_HEAT`).
- 유지: rate 모델 예상 peak(가열 시작 바닥 + 상승률 x 가동 min) < `floorMinC` + 1.0 °C(`FLOOR_MIN_HYST_C`)인 동안 `GROUP_FLOOR_MIN_CONTINUE`, 도달하면 두 방 OFF(`GROUP_FLOOR_MIN_REACHED`, 잔열 휴지 진입).
- 기존 안전(바닥 cap/예상 차단, 과열, 휴지, 기동 전 차단, 채터링 방지)은 모두 그대로 적용됩니다.
- 근거: 2026-11-25 09:48 ~ 19:56 약 10 h 동안 실내가 목표 이상이라 AUTO 난방이 없었고 바닥은 23.1 -> 19.7 °C 로 식었습니다. 시작값 권장 21 °C(사용자 조정).

### 3.0.3 (v2.9.1) 실내 센서 보정값 (`roomTempOffsetC`)

- 방 필드 `roomTempOffsetC`(-3.0 ~ +3.0 °C, 기본 0). DB 컬럼 `room_temp_offset_c`(자동 마이그레이션, 기존 방은 0).
- **제어용 실내 온도 = 센서 원시값 + 보정값.** 모든 제어/안전(실내 과열 24 °C 포함), 그룹 가중 평균, 기록/차트/KPI 에 적용됩니다. 센서 추적(보고 주기, 기울기, 환기 급락 감지)은 **원시값**을 유지하므로 보정값을 바꿔도 가짜 step 변화가 생기지 않습니다.
- API: `POST /api/v1/rooms`(`roomTempOffsetC` 선택, 재등록 시 생략하면 기존 값 유지), `PATCH /api/v1/rooms/{room}` `{"roomTempOffsetC": x}`(|x| > 3 이면 422). `/api/v1/status` `liveByRoom` 에 `roomTemp`(보정값), `roomTempRaw`, `roomTempOffsetC`. 트레이스 inputs 에 `roomTempRaw`/`roomTempOffsetC`, 회차 diagnostics 에 0 이 아닐 때 `roomTempOffsetC` 저장.
- UI: 방 카드 "센서 보정 -0.1/+0.1" 버튼과 "(원시 x)" 표시.
- **동시 비교 측정 도우미**: `POST /api/v1/calibration/room-sensors` body `{start, end}`(ISO KST), `reference`(`""` = 전체 센서 중앙값(합의), 방 이름 = 그 센서(현재 보정값 포함)), `referenceTempC`(선택, 기준 온도계 값). 구간 2 h 미만이면 422, 모르는 reference 방이면 404.
  - 방별 원시 median(원시 = 기록 roomTemp - 기록 보정값), 보고 수(서로 다른 보고 3개 미만이면 `FEW_REPORTS`), spread(p90 - p10, 1.0 °C 초과 시 경고), `suggestedOffsetC` = 기준 - 원시 median(±3.0 °C clip, 0.1 °C 단위). 적용은 UI [적용] 버튼(PATCH)으로 수동입니다.
  - 절차: ① 측정 중 시험 운전(Dry-Run) 또는 전체 방 수동 OFF 로 전환(센서가 방에 없으므로 제어가 개입하면 안 됨) ② 모든 방 센서를 한 곳(외풍/햇빛/열원 없음)에 모음 ③ 난방 OFF 상태로 4 h 이상(가능하면 밤새) 유지 — 방3/방2 센서는 약 55~60 min 마다 보고 ④ UI "실내 센서 보정 — 동시 비교 측정" 패널에 구간 입력 후 [적용].
  - 상수(`const.py`): `ROOM_TEMP_OFFSET_MAX_C`=3.0 °C, `CALIBRATION_MIN_SPAN_H`=2.0 h, `CALIBRATION_MIN_REPORTS`=3, `CALIBRATION_REF_SPREAD_WARN_C`=1.0 °C.

### 3.0.4 (v2.9.1) 쾌적 학습 (`comfort.py`, 제안 전용)

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

### 3.0.5 (v2.9.1) Gemini 일일 리포트 (자동 발송, 참고용)

- 설정(UI, `PATCH /api/v1/settings`): `dailyReportEnabled`(bool, 기본 true), `dailyReportHour`(KST 0 ~ 23 시, 기본 8, 범위 밖 422). UI "자동 발송", "발송 시각".
- 하루 1회, 현재 KST 시각이 `dailyReportHour` 이후 4 h 이내일 때 설정된 알림(HA notify / Telegram)으로 발송합니다(`DAILY_REPORT_SEND_WINDOW_H`=4 h, 재시작 후 늦은 발송 방지). 발송 전에 KV `daily_report_last` 에 날짜를 기록하므로 실패해도 반복 발송하지 않습니다. 백그라운드 task 로 실행되어 1 min 제어 루프를 막지 않습니다.
- 내용(결정적, 항상 발송): 기간/커버 시간, 보일러 가동/duty/단펄스/가스비, KPI(목표 ±0.15 °C 대역 비율, 최대 overshoot, 안전 개입 횟수), 방별 평균 실내/실효 목표/목표 - 0.3 °C 미만 시간 비율/난방 시간(min), 바닥/외기 범위, 사용자 난방 요청(최근 24 h 쾌적 신호), 쾌적 제안. 이어서 "Gemini 분석": 모델 `gemini-3.8-flash`(`generateContent` v1beta, `x-goog-api-key` 헤더), 한국어 plain text 8줄 이하(한 줄 요약, 숫자 포함 이상 징후 1~3개, 설정 제안 1~2개 — 로컬 쾌적 제안 평가). thought 파트는 제외. API key 없음/실패 시 요약만 발송(status `NO_API_KEY`/`ERROR`). 본문은 3500 자로 자릅니다(Telegram 4096 자 한도, `DAILY_REPORT_MAX_CHARS`=3500).
- 개인정보: Gemini 전송 전 방 이름을 `ROOM_A`, `ROOM_B`... 로 익명화하고 응답에서 원래 이름으로 되돌립니다.
- API: `POST /api/v1/report/daily?send=false|true` → `{kpis, report(Gemini 텍스트), status, text(전체 조합), context, sent?}`. UI 버튼 "일일 리포트 미리보기 (Gemini)", "리포트 알림 발송".
- 코드: `integrations/google_gemini_advisor.py` 에 `compute_room_daily_stats`, `format_daily_report_text`, `GeminiThermalAdvisor.generate_daily_report` 추가(기존 `generate_report` 유지). `stats_rows_since` 가 `floorTemp`/`floorSrc`/`outdoorTemp` 도 반환. 상수 `DAILY_REPORT_DEFAULT_HOUR`=8, `DAILY_REPORT_SEND_WINDOW_H`=4 h, `DAILY_REPORT_MAX_CHARS`=3500.
- 제어 설정은 절대 변경하지 않습니다.

### 3.1 P000 SENTINEL 내부 우선순위

| 순서 | 검사 | 코드 |
| :--- | :--- | :--- |
| 1 | 수동 override 플래그 (엔진이 항상 `False` 로 전달. 수동은 위 2 단계에서 별도 처리) | `P000_MANUAL_OVERRIDE` |
| 2 | 실내/바닥 값 누락 | `P000_SENSOR_MISSING` |
| 3 | sensor 경과 > sensor별 stale 임계 | `P000_SENSOR_STALE` |
| 4 | 과열 차단 (1 단계와 동일 기준) | `P000_OVERHEAT_CUTOFF` |
| 5 | 바닥 + (가열 중이면) 지연 보상 >= 상한 (`min(floorOffLimitC, 목표 연동 상한)`) | `P000_FLOOR_LIMIT_OFF` |
| 6 | **(v2.9.0)** 가열 중 예상 peak(가열 시작 바닥 + `floorRiseRateCPerMin` x 가동 분) >= 바닥 cap - 0.2 °C (`FLOOR_PEAK_MARGIN_C`). rate 없는 레거시 surge 모드는 바닥 + surge >= cap | `P000_FLOOR_PREDICTED_LIMIT` |
| 6e | **(v2.9.0)** 기동 전 가드(가동 중 가드와 통일): 바닥 + (최소 5 min 펄스 예상 상승) >= cap - margin 이면 시작 차단. 레거시 surge 모드는 바닥 + max(0.5, surge) >= cap. 기존 5c(cap - 0.5 °C) 규칙은 제거 | `P000_FLOOR_PREDICTED_LIMIT` |
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
- 1회 최대 가동 시간(v2.9.0): `min(외기 연동 최대 가동, (cap - 0.2 - 시작 바닥) / rate)`. 5 min 미만이면 펄스를 시작하지 않습니다. 가열 시작 바닥은 room timers 의 `heatingStartFloor` 에 저장합니다.
- 바닥 cap = `min(floorOffLimitC, 목표 연동 동적 상한)`. 과열 상수(실내 24.0 °C / 바닥 25.0 °C)는 변경 없음.
- 기존 `floorResidualRiseC` 는 계속 학습/표시되지만 rate 가 있으면 안전 판정에는 쓰지 않습니다.

### 3.1.1 바닥 상승률 모델 (v2.9.0, `thermal_lag_learner`)

- 배경(2026-11-25 00:01~08:12 trace): (A) 시작 게이트는 바닥 >= cap - 0.5 °C 만 차단했으나 가동 중 게이트는 바닥 + surge >= cap 에서 차단하여, [cap - surge, cap - 0.5) 구간에서 시작 후 1분 만에 차단됨(05:56, 06:40 방3, 각 1.1 min 펄스). (B) v2.8 추정 OFF 방이 마스터 바닥을 대리 안전값으로 썼으나 마스터 바닥은 타 방 난방에 반응하지 않음(01:56~04:22 거실/방2 4회 가열 동안 마스터 바닥 21.4 -> 20.3 °C 하락). 07:40 방3 바닥 25.7 °C 도달 시 4개 방 전체 과열 차단. (C) 잔열 상승을 펄스 길이와 무관한 단일 중앙값으로 학습하여 1분 펄스(+0.3~0.5 °C)가 1.2 °C 하한까지 끌어내렸고, 8분 펄스(실제 +2.2 °C, 07:46 peak 25.7 °C, OFF 후 약 20 min)를 과소 예측.
- 학습값 `floorRiseRateCPerMin` = (OFF 후 바닥 peak - 가열 시작 바닥) / 가동 분. 가동 3 min 이상(`FLOOR_RATE_MIN_RUN_MIN`)이며 peak 가 확정(peak 이후 5 min 이상 관측)된 펄스만 사용. 샘플 수는 `floorRateSamples`.
- 보수적 통계: 샘플 0 -> 기본 0.30 °C/min, 1~2 -> max(기본, 최대 샘플), 3 이상 -> 75 percentile. 0.05 ~ 0.80 °C/min 로 clamp.
- 기본 0.30 °C/min 출처: 2026-11-25 trace, 방3 단독 8 min +2.1 °C (0.26/min), 13 min +3.9 °C (0.30/min) 중 보수값.

### 3.3 바닥 기준 동시 제어 그룹 (v2.9.0)

| 구분 | 내용 |
| :--- | :--- |
| 구성 | 바닥 sensor 방(`MEASURED`) + 센서 없고 "바닥 기준 공유" ON 인 방(`ESTIMATED`, 마스터 + offset) 중 `AUTO` 인 방. Leader = 바닥 sensor 방. 예: 방3(leader) + 방2(follower) |
| 그룹 수요 | 방 weight 가중평균(투영 실내 - 실효 목표). 투영 실내 = P200 `projected_coast`(무가열 투영), 없으면 실내 + 학습 실내 잔열 |
| 판정 | idle: 평균 <= -0.15 °C 면 HEAT (`GROUP_DEMAND_HEAT`). 가열 중: 평균 >= 0 면 OFF (`GROUP_TARGET_REACHED`), 아니면 `GROUP_CONTINUE`. 그 외 `GROUP_IDLE`. 데이터 부족 시 `GROUP_NO_ROOM_DATA` |
| Leader | 공유 바닥에 대해 전체 바닥 안전(과열, 바닥 상한, rate 기반 예상 peak, 최대 가동, 휴지, 잔열 가드) 후 그룹 수요 적용. anti-chatter 는 leader 에 적용 |
| Follower | 방 기준 안전(실내 과열, 창문, 환기, 실내 sensor 누락/stale)만 수행 후 leader 의 action/reason 복사. 타이머(`heatingStartedAt`, `heatingStartFloor`, `cooldownUntil`)는 leader 와 동기화. leader 가 가열하지 않으면 follower 도 가열하지 않음 |
| 예외 | leader 판정 없음 또는 leader HEAT 전송 FAILED -> follower OFF (`GROUP_LEADER_UNAVAILABLE`). 그룹 가열 중 follower 실내 >= 실효 목표 + 0.15 °C 면 해당 방만 OFF (`GROUP_MEMBER_WARM_SKIP`) |
| 주의 | 바닥 sensor 방을 MANUAL 로 두면 AUTO 추정 방들이 첫 추정 방을 leader 로 그룹을 구성하며, 바닥 sensor 는 그 난방을 보지 못함. 장시간 사용 금지 |
| 독립 제어 | "바닥 기준 공유 OFF"(내부 `floorSrc=ESTIMATE_OFF`): 바닥값/안전 바닥 없음. 독립 방끼리만 하이브리드 multi-zone(`HYBRID_PRIMARY_HEAT`/`HYBRID_EARLY_CUTOFF`/`HYBRID_IDLE`/`HYBRID_COHEAT_BATCH`). 보호: 실내 과열 24 °C, 외기 연동 최대 가동, 잔열 휴지, 창문/환기. 마스터 바닥 과열/누락/미설정 무영향. 수동 HEAT 는 실내 sensor 만 필요. 예: 거실, 안방 |

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
| `rooms` | **(v2.9.1)** `room_temp_offset_c`(기본 0, 자동 마이그레이션), `room_name`(PK), `room_temp_entity`, `climate_entity`, `window_sensor_entity`, `has_floor_sensor`, `floor_estimate_enabled`(v2.8.0, 기본 1), `floor_offset_c`, `target_temp`, `control_mode`, `manual_action`, `weight`, `updated_at` |
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
| `comfort_events` | **(v2.9.1)** 쾌적 학습 신호(60 일, 최대 500 개) |
| `daily_report_last` | **(v2.9.1)** 일일 리포트 마지막 발송 일자(발송 전 기록) |

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
| `roomTemp`, `floorTemp`, `floorSensorSource` | 실내/바닥(실효값) 온도, `MEASURED`/`ESTIMATED`/`ESTIMATE_OFF`/`MISSING`/`UNCONFIGURED` (`ESTIMATE_OFF` = 독립 제어, `floorTemp=null`. v2.9.0 부터 `guardFloor` 대리값 없음) |
| `targetTemp` | 방 설정 목표 |
| `effectiveTargetTemp` | **(v2.7.0)** 외기/스케줄/프리셋 보정 후 실효 목표 |
| `outdoorTemp` | **(v2.7.0)** 회차 당시 외기 (RC 피팅 입력) |
| `predictedRoom30m`, `predictedFloor30m`, `predictorModel`, `residualLearningStatus` | P200 결과 (`MANUAL` 방은 `manual`) |
| `safetyReason`, `commandAction` | 판단 사유, `HEAT`/`OFF`/`HOLD` |
| `commandSetpoint`, `deviceModeEvent`, `combustionConfirmed` | 조절기 설정온도, 관측 모드, 가동 추정 |
| `dryRun` | **(v2.7.0)** 시험 운전 회차 여부 (통계/학습에서 제외 판단에 사용) |
| `dispatchResult` | **(v2.7.0)** `SENT`, `NO_CHANGE`, `DRY_RUN`, `FAILED`, `NONE` |
| `diagnostics` | **(v2.7.0)** `floorReportIntervalSec`, `floorStaleThresholdSec`, `floorLatencyC`, `roomReportIntervalSec`, `roomStaleThresholdSec`, `vRoomRate`, `vRoomSmooth`, `vFloor`, `drop5mRoom`, `trend10mRoom`, `trend10mFloor`, `dispatchCalls`, `antiChatterBlocked`, **(v2.9.0)** `groupRole`, `groupLeader`, 실패 시 `error` |

### API/알림 추가 사항 (v2.9.1)

- 설정 `externalBoostMin`(`PATCH /api/v1/settings`, 기본 15, 0 ~ 60 min), `manualHeatMaxMin`(기본 15, 5 ~ 60 min), `floorMinC`(기본 0 = 사용 안 함, 0 ~ 23 °C), `dailyReportEnabled`(기본 true), `dailyReportHour`(기본 8, 0 ~ 23 KST 시).
- 방: `roomTempOffsetC`(-3.0 ~ +3.0 °C, `POST/PATCH /api/v1/rooms`). `POST /api/v1/calibration/room-sensors`, `GET /api/v1/comfort`, `POST /api/v1/comfort/reset`, `POST /api/v1/report/daily?send=` (3.0.3 ~ 3.0.5).
- `/api/v1/status`: `liveByRoom.roomTemp`(보정값)/`roomTempRaw`/`roomTempOffsetC`, `comfort`. 트레이스 `inputs.roomTempRaw`/`roomTempOffsetC`.
- `/api/v1/status` `liveByRoom.manualHeatUntil`, 방 카드 배지 "수동 가동 (HH:MM 자동 복귀)".
- 신규 사유 코드(수동/바닥 최저): `MANUAL_HEAT_TIMEOUT`, `MANUAL_HEAT_CANCELLED`, `GROUP_FLOOR_MIN_HEAT`, `GROUP_FLOOR_MIN_CONTINUE`, `GROUP_FLOOR_MIN_REACHED`.
- Telegram: 수동 ON "수동 난방: HH:MM까지 최대 N분 후 자동 제어 복귀", 종료 "수동 난방 종료 → 자동 제어(AUTO) 복귀", 연동 boost 시작 "외부 조작 감지(방3 · 방2 연동): HH:MM까지 15분 유지 후 자동 중지".
- `/api/v1/status` `liveByRoom.externalBoostUntil`(boost 자동 중지 시각). 방 카드 배지 "외부 조작 가동 (HH:MM 자동 중지)".
- 신규 사유 코드: `EXTERNAL_BOOST`, `EXTERNAL_BOOST_END`, `EXTERNAL_BOOST_CANCELLED`.
- Telegram 시작 알림: "외부 조작 감지: HH:MM까지 15분 유지 후 자동 중지".

### API/알림 추가 사항 (v2.9.0)

- `/api/v1/status` `liveByRoom`: `groupRole`(LEADER/FOLLOWER/INDEPENDENT/null), `groupLeader`, `floorRiseRateCPerMin` 추가. 독립 방은 `guardFloorTemp=null`. `effectiveMaxRunMin` 은 rate 모델 기준.
- `/api/v1/status` `fittedRcParams`: UI "P200 RC 파라미터 보기" 가 사용. `null` 이면 `adaptive_rc` 는 코드 기본값(c_floor 1800, c_room 450 kJ/K, r_floor_room 2.2, r_room_ambient 12 K/kW, q_heat 1.8 kW) 사용.
- 방 토글 API 필드는 기존 `floorEstimateEnabled` 그대로이며 UI 표기만 "동시 제어(바닥 공유)" / "독립 제어".
- 신규 사유 코드: `GROUP_DEMAND_HEAT`, `GROUP_CONTINUE`, `GROUP_TARGET_REACHED`, `GROUP_IDLE`, `GROUP_NO_ROOM_DATA`, `GROUP_MEMBER_WARM_SKIP`, `GROUP_LEADER_UNAVAILABLE`.
- 알림: 독립 방 "바닥: 미사용(독립 제어)", follower "<floor>°C(추정 · 방3 기준 동시 제어)", 목표 "21.5°C (실효 21.64°C)", 그룹 방 "동시 제어: 방3 · 방2 (그룹 평균 편차 -0.45°C)" 줄 추가, 그룹 방은 "30분 예측 실내" 줄 생략.

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
| `limits` | `overheatRoomC`, `overheatFloorC`, `effFloorLimitC`, `learnedFloorSurgeC`, **(v2.9.0)** `floorCapC`, `floorRiseRateCPerMin`, `heatingStartFloorC`, `baseMaxRunMin`, `roomMaxRunMin`, `cooldownMin`, `pollIntervalMin` |
| `lagProfile` | `status`, `sampleCount`, `floorResidualRiseC`, `roomResidualRiseC`, **(v2.9.0)** `floorRiseRateCPerMin`, `floorRateSamples`, `recommendedCooldownMin`, `recommendedHorizonMin`, `recommendedRunTrimMin` |
| `timersBefore` / `timersAfter` | 판단 전: `runMin`, `cooldownRemainMin`, `ventilationRemainMin`, `lastCommand`, `secsSinceSwitch` / 판단 후: `heatingStartedAt`, `cooldownUntil`, `ventilationUntil`, `lastSwitchAt`, `underheatSince` |
| `p200` | `model`, `action`, `predRoom`, `predFloor`, `effTarget`, `learning`, `residualRiseC`, `diag` (없으면 `null`) |
| `multiZone` | `action`, `reason` (없으면 `null`) |
| `externalBoost` | **(v2.9.1)** `event`(`START`/`ACTIVE`/`END`/`CANCELLED`), `until` (`diagnostics.externalBoostGroup`: 방3/방2 연동 정보, `diagnostics.manualEnded`: 수동 요청 종료) |
| `group` | **(v2.9.0)** `role`, `leader`, `members`, `demand`, `demandReason`, `weightedDevC`, `leaderResult` |
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
| Gemini 일일 리포트 | 활성(v2.9.1: 매일 자동 발송 + UI 버튼 `POST /api/v1/report/daily`, 참고용, 3.0.5) |
| HA `notify`, `telegram_bot`(`chat_id`, 구형 `target` 폴백) | 활성 |
| `GoogleCalendarAdvisor`, `GeminiTTSAlert`, `format_cycle_for_sheets`, `CircadiaProtocol` | **라이브러리 전용. 제어 루프에 연결되지 않음** (P400 은 DB 스케줄 사용) |

---

## 다음 단계 (Next Steps)

1. 실운전 후 JSONL 트레이스의 `sentinel.reason` / `decision.reason` 분포를 집계하여 어떤 보호 로직이 주로 개입하는지 확인하십시오.
2. 라이브러리 전용 모듈을 활성화할 계획이 있다면 별도 이슈로 연결 지점(제어 루프/알림 흐름)을 정의하십시오.
3. `DELAYED` 상태는 스키마에만 있고 현재 코드에서 설정되지 않으므로, 사용하지 않을 경우 정리를 검토하십시오.
