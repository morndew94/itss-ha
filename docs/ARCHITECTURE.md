# I.T.S.S. 아키텍처 및 데이터 규격서 (v1.0)

## 1. 단일 통합구성요소 기반 제어 파이프라인

I.T.S.S.는 별도 애드온 없이 `custom_components/itss` 하나로 실시간 제어, 예측 연산, 로컬 SQLite 이력 저장(`config/itss_scada.db`)을 모두 처리합니다.

매 제어 회차(Cycle)마다 다음 순서로 프로토콜을 실행합니다.

1. **`P100 CORE` 계측 수집**: 대상 방의 실내온도(0.1°C), 바닥온도(0.1°C), 습도, 온도조절기 현재 상태 및 연소 상태를 조회합니다.
2. **`P400 CIRCADIA` 목표 보정**: 생활패턴(수면/기상/외출)에 따른 목표 온도 오프셋을 적용합니다.
3. **`P300 ATMOS` 열손실 보정**: 외기/기상 데이터(미연동 시 시간대별 보정 프로파일)를 산출합니다.
4. **`P000 SENTINEL` 안전 인터락 검증**:
   - 센서 유효성/신선도 검사
   - 수동 조작 우선 여부 확인
   - 과열 차단(실내 >= 27.0°C 또는 바닥 >= 29.0°C) 및 일반 OFF 상한(바닥 >= 28.0°C)
   - 환기 급락 보호(단시간 급격한 실내 온도 하강 감지 시 난방 보류)
   - 채터링 방지(최소 가동 시간 / 최소 휴지 시간 미충족 시 상태 유지)
5. **`P200 THERM` 열역학 예측 제어**:
   - `v1.2` 선형외삽 모델(`F30`, `Room30`) 또는 적응형 RC 모델(`C_f`, `C_r`, `R_fr`, `R_ra`) 중 설정된 엔진으로 ON/OFF 판단을 수행합니다.
   - 잔열 학습 상태(`READY`, `INSUFFICIENT_SAMPLES`, `TRANSIENT_SKIP`)를 명시적으로 기록합니다.
6. **`P100 CORE` 명령 전송, 재조회 확인 및 로컬 DB 저장**:
   - 온도조절기 0.5°C 단위 반올림 특성을 반영해 목표 설정 온도를 계산합니다.
   - 명령 전송 시각(`controlIssuedAt`) -> 확인 시각(`controlConfirmedAt`) -> 상태 재조회 관측 시각(`observedAt`) -> 저장 시각(`savedAt`)을 순차 기록합니다.
   - HA 실행 스레드 풀(`async_add_executor_job`)을 통해 로컬 SQLite DB(`storage/db.py`)에 비동기 안전 저장합니다.

---

## 2. KST 정밀 시각 및 이력 스키마 (`CycleRecord`)

모니터링 이력의 `미확인` / `null` 누락을 방지하기 위해 모든 시각은 ISO-8601 KST(`+09:00`) 문자열로 분리 저장합니다.

| 필드명 | 설명 |
| :--- | :--- |
| `cycleId` | 회차 고유 식별자 (예: `20261120-1755-living_room`) |
| `scheduledAt` | 예정 회차 예약 시각 (KST) |
| `executionStartedAt` | 실제 제어 루프 실행 시작 시각 (KST) |
| `sensorMeasuredAtRoom` | 실내 온도 센서 최종 갱신 시각 (KST) |
| `sensorMeasuredAtFloor` | 바닥 온도 센서 최종 갱신 시각 (KST) |
| `controlIssuedAt` | 기기 제어 명령 전송 시각 (KST) |
| `controlConfirmedAt` | 명령 수락/반영 확인 시각 (KST) |
| `observedAt` | 명령 후 상태 재조회 관측 시각 (KST) |
| `savedAt` | 로컬 SQLite DB 저장 시각 (KST) |
| `executionStatus` | `EXECUTED`, `DELAYED`, `SKIPPED_SAFETY`, `FAILED` 등 |
| `opposingCommandIntervalSec` | 직전 상반 명령(ON<->OFF)과의 경과 시간(초) |
| `commandAction` | 전송한 제어 명령 (`HEAT`, `OFF`, `HOLD`) |
| `deviceModeEvent` | 기기에서 관측된 실제 운전 모드 |
| `combustionConfirmed` | 실제 보일러 연소/밸브 개방 확인 여부 (`true`/`false`/`null`) |

---

## 3. P200 예측 모델 추상화 (v1.2 -> RC 전환)

`ThermalPredictor` 추상 인터페이스는 동일한 `ThermalInput`을 받아 `ThermalPrediction`을 반환합니다.

- **`LinearExtrapolationV12`**:
  - `F30 = floor_temp + v_floor * 30`
  - `Room30 = room_temp + v_room * 30`
- **`AdaptiveRCModel`**:
  - 2-노드 RC 열회로망(바닥 노드 `T_f`, 실내 노드 `T_r`, 외기 노드 `T_a`)의 이산 시간 차분 방정식으로 30분(또는 N분) 궤적을 시뮬레이션합니다.
  - `dT_f/dt = (Q_heat - (T_f - T_r) / R_fr) / C_f`
  - `dT_r/dt = ((T_f - T_r) / R_fr - (T_r - T_a) / R_ra) / C_r`
  - 파라미터(`C_f`, `C_r`, `R_fr`, `R_ra`, `Q_heat`)는 통합구성요소 내부의 `protocols/rc_optimizer.py`가 실측 이력으로부터 최적화합니다.
