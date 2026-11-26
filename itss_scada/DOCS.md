# I.T.S.S. Thermal SCADA 애드온 사용 설명서 (v2.9.1)

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
- P400 시간대별 목표 offset 스케줄(UI 편집), RC 피팅 및 슬레이브 floor offset **제안**(자동 적용 안 함, 방 테이블 버튼으로 적용)
- **바닥 기준 동시 제어 / 독립 제어 (v2.9.0)**: 방 테이블 바닥 칸의 `동시 제어(바닥 공유)` / `독립 제어` 토글, offset `±0.1 °C` 버튼. 독립 제어 방은 바닥값과 바닥 안전 판정이 없으며(v2.8 마스터 대리 판정 제거) 실내 기준 보호만 받습니다. 자세한 내용은 아래 "v2.9.0 변경 사항" 참조.
- **외부 조작 가동 유지 (v2.9.1)**: 월패드/다른 자동화로 켠 난방을 `externalBoostMin`(기본 15 min) 동안 유지 후 자동 중지합니다. 아래 "v2.9.1 변경 사항" 참조.
- **실내 센서 보정값 / 쾌적 학습 / Gemini 일일 리포트 (v2.9.1)**: 방별 센서 보정, 월패드 조작 기반 목표/바닥 최저 온도 **제안**, 매일 자동 발송되는 Gemini 일일 리포트(참고용). 아래 "v2.9.1 변경 사항" 참조.
- 알림: HA `notify`, Telegram(`chat_id`). Gemini 일일 리포트(자동 발송 + 버튼 미리보기/발송, API key 없으면 요약만)

> Google Calendar 예열, Gemini TTS 음성 경보, Google Sheets 내보내기는 코드에 라이브러리로만 존재하며 **현재 동작하지 않습니다.**

## 2.0 v2.9.1 변경 사항 (외부 조작 가동 유지 · 수동 HEAT 안전 · 바닥 최저 온도 · 센서 보정 · 쾌적 학습 · 일일 리포트)

**문제 (2026-11-25 field trace)**: 월패드 또는 다른 HA 자동화로 설정온도를 24 °C 로 올려도 ITSS 가 약 1 분 안에 15 °C 로 되돌렸고, 0 min 가동 후에도 30 min 잔열 휴지를 시작했습니다. 사용자가 수동으로 난방을 켤 수 없었습니다.

**동작**

- 감지: `AUTO` 방에서 직전 회차 관측이 비가열, 현재 회차 관측이 가열, ITSS 마지막 명령이 HEAT 가 아니면 외부 수동 ON 으로 판단합니다.
- 유지: `externalBoostMin` 분(기본 15, 범위 0 ~ 60, 0 = 기존 동작: 즉시 되돌림) 동안 **명령을 보내지 않고** 가동을 유지합니다(외부 설정온도는 그대로). 종료 시 OFF 를 보내고 정상 잔열 휴지에 들어갑니다(가동이 5 min 이상일 때만 휴지).
- 외부 OFF: boost 중 사용자가 외부에서 끄면 다시 켜지 않습니다 (`EXTERNAL_BOOST_CANCELLED`).
- 안전은 계속 적용: 실내 24 °C / 바닥 25 °C 과열 차단, 창문, 환기 보호, 센서 누락/stale, 바닥 기준 방(방3 센서 방, 방2 추정 방)의 상승률 기반 예상 peak 차단(`P000_FLOOR_PREDICTED_LIMIT`). 잔열 휴지, 1회 최대 가동, 그룹/multi-zone 수요는 boost 중 우회합니다.
- 방3 · 방2 연동: 바닥을 공유하는 두 방(`AUTO`, 실측/추정 바닥) 중 한 방이라도 외부에서 켜면 **두 방이 같은 종료 시각**(`externalBoostMin`)으로 함께 가동합니다. 일반 그룹 경로를 사용하므로 leader(방3, 실측 바닥)가 바닥 안전(상승률 기반 예상 peak 차단, 과열 등)을 판단하고 follower 가 따라가며, 타이머/휴지는 동기화됩니다. boost 중 follower 의 warm skip 은 사용자 의도를 우선해 우회합니다. 켠 방은 외부 설정온도를 유지하고 ITSS 가 다른 방에 HEAT 를 보냅니다.
- 한 방을 외부에서 끄면 연동 boost 가 두 방 모두 취소되고(`EXTERNAL_BOOST_CANCELLED`), 종료 시 두 방 모두 OFF(`EXTERNAL_BOOST_END`) + 같은 잔열 휴지에 들어갑니다. 연동 boost 회차에서 최대 가동/휴지는 우회하지만 바닥 안전은 우회하지 않습니다. 취소 판정은 boost 중 실제 가열이 관측된 뒤에만 인정하며, 연동 방의 ITSS HEAT 가 아직 적용 전이면 취소 대신 HEAT 를 재전송합니다. 독립 방(거실/안방)은 단독 boost 이며 multi-zone 계획에서 계속 제외됩니다.
- 오탐 방지: ITSS 의 OFF 가 조절기에 적용되지 않은 경우(직전 관측도 가열)는 외부로 보지 않고 OFF 를 재전송합니다. ITSS 자신의 HEAT 와 ITSS `MANUAL` 방은 감지 대상이 아닙니다.

**UI / API / 알림**

- 설정 `externalBoostMin` (`PATCH /api/v1/settings`), UI 항목 "월패드·외부 자동화로 켜면 유지 (분, 0=즉시 되돌림)".
- `/api/v1/status` `liveByRoom.externalBoostUntil`, 방 카드 배지 "외부 조작 가동 (HH:MM 자동 중지)".
- 트레이스 최상위 `externalBoost`{`event`: START/ACTIVE/END/CANCELLED, `until`}, diagnostics `externalBoostGroup`.
- 신규 사유 코드: `EXTERNAL_BOOST`(외부 조작(월패드/다른 자동화)으로 켜짐 감지, 설정 시간 가동 후 자동 중지), `EXTERNAL_BOOST_END`(가동 시간 종료, 자동 중지 및 잔열 휴지 진입), `EXTERNAL_BOOST_CANCELLED`(외부에서 끔 또는 조절기 자체 설정온도 도달).
- Telegram 시작 알림에 "외부 조작 감지: HH:MM까지 15분 유지 후 자동 중지" 줄 추가(연동 방: "외부 조작 감지(방3 · 방2 연동): HH:MM까지 15분 유지 후 자동 중지").

### ITSS 수동 HEAT 안전 강화 (1회 요청화)

**근거 (2026-11-25 trace): 수동 HEAT 가 거실 약 154 min 연속 가동(독립 방이라 실내 과열 24 °C 만 정지 조건, 실내는 23.4 °C 까지만 상승), 방3 36.9 min 가동에 바닥 최고 27.1 °C(cap 25 °C).**

- 신규 설정 `manualHeatMaxMin`(기본 15, 범위 5 ~ 60 min, UI "ITSS 수동 ON 1회 최대 (분, 경과 시 자동 복귀)"). 경과 시 OFF 전송, 방을 AUTO(controlMode AUTO, manualAction HOLD)로 복귀시키고 5 min 이상 가동했으면 잔열 휴지를 시작합니다. 사유 `MANUAL_HEAT_TIMEOUT`("수동 난방 설정 시간 경과(자동 중지 · 자동 제어 복귀)").
- 바닥 기준 방(방3 실측, 방2 추정)의 수동 HEAT 에도 AUTO 와 같은 상승률 기반 바닥 예측을 적용합니다. 시작 전 바닥 + 상승률 x 5 min >= 바닥 cap - 0.2 °C 이면 차단, 가동 중 가열 시작 바닥 + 상승률 x (가동 + poll) >= cap - 0.2 °C 이면 차단합니다.
- 수동 HEAT 중 바닥 한계(`P000_FLOOR_LIMIT_OFF`), 예상 한계(`P000_FLOOR_PREDICTED_LIMIT`), 과열(`P000_OVERHEAT_CUTOFF`)이 발생하면 수동 요청을 **종료하고 AUTO 로 복귀**합니다(이전에는 MANUAL 로 남아 바닥이 한계보다 1 °C 식으면 스스로 재가열).
- 추정 방(방2, floorOffsetC 음수)은 수동 모드에서도 실측 마스터 바닥보다 낮지 않은 바닥(공유 바닥)으로 판정합니다. 2026-11-25 22:27 방2 단독 재가열 사례를 수정합니다.
- 수동 HEAT 중 월패드/자동화가 조절기를 외부에서 끄면 24 °C 를 다시 지시하지 않고 수동 요청을 종료하며 AUTO 로 복귀합니다. 사유 `MANUAL_HEAT_CANCELLED`("수동 난방 중 외부에서 끔(자동 제어 복귀)").
- Telegram: ON 알림에 "수동 난방: HH:MM까지 최대 N분 후 자동 제어 복귀", 종료 알림에 "수동 난방 종료 → 자동 제어(AUTO) 복귀". `/api/v1/status` `liveByRoom.manualHeatUntil`, 방 카드 배지 "수동 가동 (HH:MM 자동 복귀)". 트레이스 diagnostics `manualEnded`.

### 바닥 최저 온도 (floor comfort minimum)

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

## 2.1 v2.9.0 변경 사항 (이력)

2026-11-25 00:01~08:12 trace 분석 기반. 바닥 sensor(DS18B20, 바닥 표면)는 방3에만 있고, 방2은 인접, 거실/안방은 바닥 기준이 없습니다.

**수정한 문제**

| 구분 | 문제 | 수정 |
| :--- | :--- | :--- |
| A | 시작 게이트는 바닥 >= cap - 0.5 °C 만 차단, 가동 중 게이트는 바닥 + surge >= cap 에서 차단 -> 그 사이 구간에서 시작 1분 후 차단 (05:56, 06:40 방3, 각 1.1 min 펄스) | 기동 전 가드 `6e`: 바닥 + 최소 5 min 펄스 예상 상승 >= cap - margin 이면 `P000_FLOOR_PREDICTED_LIMIT` 로 시작 차단. 기존 `cap - 0.5` 규칙 제거. 레거시 surge 모드는 바닥 + max(0.5, surge) >= cap |
| B | v2.8 추정 OFF 방이 마스터(방3) 바닥을 안전 대리값으로 사용. 마스터 바닥은 타 방 난방에 반응 안 함 (01:56~04:22 거실/방2 4회 가열 동안 21.4 -> 20.3 °C 하락), 07:40 방3 25.7 °C 도달 시 4개 방 전체 과열 차단 | 바닥 기준 동시 제어 그룹 + 독립 제어로 재설계 (대리 판정 제거) |
| C | 잔열 상승을 단일 중앙값으로 학습 -> 1분 펄스(+0.3~0.5 °C)가 1.2 °C 하한까지 끌어내려 8분 펄스(실제 +2.2 °C, 07:46 peak 25.7 °C, OFF 후 약 20 min)를 과소 예측 | 가동 1분당 바닥 상승률 모델 |

**바닥 상승률 모델 (`floorRiseRateCPerMin`)**

- 학습: (OFF 후 바닥 peak - 가열 시작 바닥) / 가동 분. 3 min 이상 펄스 중 peak 가 확정된(peak 이후 5 min 이상 관측) 것만 사용. `floorRateSamples` 로 샘플 수 확인.
- 보수적 통계: 샘플 0 -> 기본 0.30 °C/min, 1~2 -> max(기본, 최대 샘플), 3 이상 -> 75 percentile. 0.05 ~ 0.80 °C/min 로 clamp.
- 기본 0.30 °C/min 은 2026-11-25 trace 의 방3 단독 8 min +2.1 °C (0.26/min), 13 min +3.9 °C (0.30/min) 에서 정한 보수값입니다.
- 예상 peak = 가열 시작 바닥 + rate x 가동 분. >= 바닥 cap - 0.2 °C 이면 차단. 바닥 cap = `min(floor_off_limit_c, 목표 연동 동적 상한)`.
- 1회 최대 가동 = min(외기 연동 최대 가동, (cap - 0.2 - 시작 바닥) / rate). 5 min 미만이면 펄스를 시작하지 않습니다.
- 기존 `floorResidualRiseC` 는 계속 학습/표시되지만 rate 가 있으면 안전 판정에는 쓰이지 않습니다.

**바닥 기준 동시 제어 그룹**

- 구성: 바닥 sensor 방(leader) + "바닥 기준 공유" ON 인 센서 없는 방(follower, 마스터 + offset) 중 `AUTO` 인 방. 예: 방3(leader) + 방2(follower).
- 그룹 수요: 방 weight 가중평균(투영 실내 - 실효 목표). 투영 실내는 P200 `projected_coast`, 없으면 실내 + 학습 잔열. idle 에서 평균 <= -0.15 °C 면 HEAT, 가열 중 평균 >= 0 면 OFF.
- leader 는 공유 바닥에 대해 전체 바닥 안전을 수행하고, follower 는 방 기준 안전(실내 과열, 창문, 환기, 센서 누락/stale)만 수행한 뒤 leader 의 action/reason 을 따릅니다. leader 가 가열하지 않으면 follower 도 가열하지 않습니다.
- leader 판정이 없거나 HEAT 전송이 FAILED 면 follower OFF (`GROUP_LEADER_UNAVAILABLE`). 그룹 가열 중 follower 실내가 실효 목표 + 0.15 °C 이상이면 그 방만 OFF (`GROUP_MEMBER_WARM_SKIP`).
- 주의: 바닥 sensor 방을 MANUAL 로 두면 AUTO 추정 방들이 첫 추정 방을 leader 로 그룹을 구성하는데, 바닥 sensor 는 그 난방을 보지 못합니다. 장시간 이렇게 두지 마십시오.

**독립 제어** ("바닥 기준 공유 OFF")

- 바닥값과 안전 바닥이 없습니다. 독립 방끼리 기존 hybrid multi-zone(`HYBRID_PRIMARY_HEAT`/`HYBRID_EARLY_CUTOFF`/`HYBRID_IDLE`/`HYBRID_COHEAT_BATCH`)으로 제어합니다.
- 보호: 실내 과열 24 °C, 외기 연동 최대 가동, 잔열 휴지, 창문/환기. 마스터 바닥 과열/센서 누락/미설정의 영향을 받지 않으며, 수동 HEAT 도 실내 sensor 만 있으면 됩니다.
- 방별 P200 단독 판단(`P200_PREDICTIVE_ON/OFF`)은 AUTO 에서 더 이상 쓰이지 않습니다. P200 예측은 계산/trace 기록되고 그룹 수요(`projected_coast`)에 반영됩니다.

**권장 설정 (이 집)**

| 방 | 설정 |
| :--- | :--- |
| 방3 | 바닥 sensor 방 (leader) |
| 방2 | 바닥 기준 공유 ON (offset 은 보정값) |
| 거실, 안방 | 독립 제어 |

**UI / API / 알림**

- UI: 토글 "동시 제어(바닥 공유)" / "독립 제어"(API 필드 `floorEstimateEnabled` 동일), 등록 폼 "바닥 기준 공유 (센서 없는 방)", lag 표 "가동 1분당 상승률", 버튼 **"P200 RC 파라미터 보기"** (`fittedRcParams`, 현재 predictorModel, thermalLagProfiles 표시. `fittedRcParams` 가 null 이면 `adaptive_rc` 는 코드 기본값 c_floor 1800, c_room 450 kJ/K, r_floor_room 2.2, r_room_ambient 12 K/kW, q_heat 1.8 kW 사용).
- `/api/v1/status` `liveByRoom`: `groupRole`(LEADER/FOLLOWER/INDEPENDENT/null), `groupLeader`, `floorRiseRateCPerMin` 추가. 독립 방은 `guardFloorTemp` = null. `effectiveMaxRunMin` 은 rate 모델 기준.
- 트레이스: `group`{role, leader, members, demand, demandReason, weightedDevC, leaderResult}, `limits.floorCapC`/`floorRiseRateCPerMin`/`heatingStartFloorC`, `lagProfile.floorRiseRateCPerMin`/`floorRateSamples`, `diagnostics.groupRole`/`groupLeader`.
- 신규 사유 코드: `GROUP_DEMAND_HEAT`, `GROUP_CONTINUE`, `GROUP_TARGET_REACHED`, `GROUP_IDLE`, `GROUP_NO_ROOM_DATA`, `GROUP_MEMBER_WARM_SKIP`, `GROUP_LEADER_UNAVAILABLE`.
- 알림: "바닥: None°C" 수정. 독립 방 "바닥: 미사용(독립 제어)", follower "<floor>°C(추정 · 방3 기준 동시 제어)", 목표 "21.5°C (실효 21.64°C)", 그룹 방 "동시 제어: 방3 · 방2 (그룹 평균 편차 -0.45°C)" 줄 추가, 그룹 방은 "30분 예측 실내" 줄 생략.
- 과열 상수는 변경 없음 (실내 24.0 °C / 바닥 25.0 °C).

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
| 실내 >= 24.0 °C 또는 바닥 >= 25.0 °C | 즉시 OFF (`P000_OVERHEAT_CUTOFF`, `AUTO`/`MANUAL` 공통) |
| `floor_off_limit_c` > 25.0 °C (기본 26.5 °C) | 과열 차단이 먼저 작동 (`/health` warnings 및 UI 에 경고) |
| 센서 누락 / stale | 강제 OFF. 수동 ON 도 차단 |
| 기동 전/가동 중 예상 바닥 peak >= 바닥 cap - 0.2 °C (v2.9.0, 상승률 모델) | 시작 차단 / 즉시 OFF (`P000_FLOOR_PREDICTED_LIMIT`) |
| 수동 ON 중 바닥 한계/예상 한계/과열 (v2.9.1) | 수동 요청 종료 + AUTO 복귀. 수동 1회 최대 `manualHeatMaxMin`(기본 15 min) 경과 시에도 자동 복귀 |
| 외부 조작(월패드/다른 자동화)으로 켜짐 감지 (v2.9.1) | `externalBoostMin` 동안 유지 후 OFF + 잔열 휴지. 과열/창문/환기/센서/바닥 예상 peak 안전은 유지. 방3/방2 는 두 방 연동 |
| 실내 센서 보정값 (v2.9.1) | 제어/안전 실내 온도 = 원시 + `roomTempOffsetC`(±3.0 °C). 실내 과열 24 °C 도 보정값 기준 |
| 바닥 최저 온도 `floorMinC` (v2.9.1, 0=사용 안 함) | 방3/방2 그룹 바닥이 최저 미만이면 가열(실내 편차 < +0.3 °C 일 때), 예상 peak >= 최저 + 1.0 °C 면 종료 |
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

주요 키: `inputs`, `dynamics`, `limits`, `lagProfile`, `timersBefore`, `p200`, `multiZone`, `externalBoost`(v2.9.1), `sentinel`, `decision`, `command`, `timersAfter`, `elapsedMs`.

## 6. 문제 해결

| 증상 | 확인 |
| :--- | :--- |
| 난방이 안 켜지고 사유가 `P000_FLOOR_UNCONFIGURED` | `master_floor_entity` 설정 |
| LIVE 전환이 409 로 거부됨 | 마스터 바닥 sensor 설정 필요 |
| 설정 저장이 422 | `heat_off_setpoint <= heat_on_setpoint - 3.0 °C` 확인 |
| 사유가 `P000_OVERHEAT_CUTOFF` 로 반복 | 실내 24.0 °C / 바닥 25.0 °C 도달 여부, 트레이스 `limits` 확인 |
| 접속 403 | Ingress(사이드바)로 접속하십시오 |
| `/health` 가 `degraded` | 로그의 "제어 회차 실패" 항목 확인 |
