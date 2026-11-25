# I.T.S.S. Thermal SCADA 애드온 사용 설명서 (v2.9.0)

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
- 알림: HA `notify`, Telegram(`chat_id`). Gemini 일일 진단 리포트(버튼, API key 필요)

> Google Calendar 예열, Gemini TTS 음성 경보, Google Sheets 내보내기는 코드에 라이브러리로만 존재하며 **현재 동작하지 않습니다.**

## 2.1 v2.9.0 변경 사항

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
| 사유가 `P000_OVERHEAT_CUTOFF` 로 반복 | 실내 24.0 °C / 바닥 25.0 °C 도달 여부, 트레이스 `limits` 확인 |
| 접속 403 | Ingress(사이드바)로 접속하십시오 |
| `/health` 가 `degraded` | 로그의 "제어 회차 실패" 항목 확인 |
