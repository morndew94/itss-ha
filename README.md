# I.T.S.S. (Intelligent Thermal SCADA System) for Home Assistant

Home Assistant 환경에서 바닥 난방의 **0.3°C 정밀 제어(목표 온도 ±0.15°C)**, **5계층 프로토콜 기반 안전·제어·예측 분리**, **적응형 RC 열역학 모델 전환**, **정밀 시각 이력 DB 저장**을 **단일 커스텀 통합구성요소(Custom Integration)**로 제공하는 프로젝트입니다.

별도의 애드온(Docker 컨테이너)을 설치하거나 관리할 필요 없이, **HACS에서 통합구성요소 하나만 설치**하면 실시간 제어 루프, 로컬 SQLite 이력 DB, RC 파라미터 피팅까지 한 번에 동작합니다.

---

## 1. 아키텍처 개요 (ITSS Architecture v1.0)

I.T.S.S.는 통합구성요소 내부에서 책임 경계를 명확히 하기 위해 **5대 프로토콜 계층**으로 모듈화되어 있습니다.

| 프로토콜 | 명칭 | 모듈 경로 | 주요 책임 |
| :--- | :--- | :--- | :--- |
| **`ITSS-P000`** | **SENTINEL** | `protocols/p000_sentinel.py` | 센서 신뢰도 검증, 과열 차단(실내 >= 27°C / 바닥 >= 29°C), 일반 OFF(바닥 >= 28°C), 환기 급락 보호, 채터링(최소 가동/휴지 시간) 방지, 수동 조작 우선 |
| **`ITSS-P100`** | **CORE** | `protocols/p100_core.py` | 센서 계측(0.1°C), 온도조절기 0.5°C 단위 반올림 보정, 명령 전송 및 상태 재조회 확인, KST 정밀 시각 스키마 기록 및 로컬 SQLite DB 저장 |
| **`ITSS-P200`** | **THERM** | `protocols/p200_therm.py`, `protocols/rc_optimizer.py` | 예측 ON/OFF 제어. 기존 `v1.2` 선형외삽(`F30`, `Room30`)과 적응형 RC 열역학 모델(`C_f`, `C_r`, `R_fr`, `R_ra`)을 동일 인터페이스로 전환 및 파라미터 피팅 |
| **`ITSS-P300`** | **ATMOS** | `protocols/p300_atmos.py` | 외기온도·강수·풍속·일사량 기반 열손실 보정 (기상 미연동 시 시간대별 열손실 프로파일로 폴백) |
| **`ITSS-P400`** | **CIRCADIA** | `protocols/p400_circadia.py` | 수면·기상·생활패턴 및 방별/개인별 온열 쾌적성 목표 온도 보정 |

---

## 2. 저장소 구조

```text
itss-ha/
├── hacs.json                          # HACS 통합구성요소 메타데이터
├── custom_components/
│   └── itss/                          # Home Assistant 단일 커스텀 통합구성요소
│       ├── manifest.json
│       ├── __init__.py
│       ├── const.py
│       ├── config_flow.py             # UI 기반 방별 엔티티 매핑
│       ├── coordinator.py             # P000~P400 주기 제어 및 로컬 DB 저장
│       ├── sensor.py                  # HA 진단/예측 센서 노출
│       ├── strings.json
│       ├── translations/
│       │   ├── ko.json
│       │   └── en.json
│       ├── models/
│       │   └── schema.py              # KST 정밀 시각 및 회차/제어 이력 표준 스키마
│       ├── storage/
│       │   └── db.py                  # 로컬 SQLite 회차 이력 DB (config/itss_scada.db)
│       └── protocols/
│           ├── p000_sentinel.py       # 안전 인터락 및 과열/채터링 보호
│           ├── p100_core.py           # 0.5°C 반올림 처리 및 상반 명령 간격 계산
│           ├── p200_therm.py          # v1.2 선형외삽 & 적응형 RC 예측 모델 인터페이스
│           ├── rc_optimizer.py        # 2-노드 RC 파라미터 피팅 엔진
│           ├── p300_atmos.py          # 외기/시간대별 열손실 보정
│           └── p400_circadia.py       # 생활패턴 쾌적성 보정
├── docs/
│   └── ARCHITECTURE.md                # 상세 프로토콜 및 타임스탬프 규격서
├── tests/
│   └── test_protocols.py              # P000~P400, DB, RC 피팅 단위 테스트
└── .github/
    └── workflows/
        └── validate.yml               # GitHub Actions 자동 테스트 워크플로
```

---

## 3. 설치 방법 (HACS)

1. Home Assistant의 **HACS** > **통합구성요소** > 우측 상단 메뉴 > **사용자 지정 저장소(Custom repositories)**를 엽니다.
2. 저장소 URL(`https://github.com/morndew94/itss-ha`)을 입력하고 카테고리를 **Integration**으로 선택해 추가합니다.
3. **I.T.S.S. (Intelligent Thermal SCADA System)**를 다운로드한 뒤 Home Assistant를 재시작합니다.
4. **설정** > **기기 및 서비스** > **통합구성요소 추가**에서 `I.T.S.S.`를 검색하여 방별 엔티티(실내온도, 바닥온도, 온도조절기 등)를 매핑합니다.

---

## 4. 로컬 개발 및 단위 테스트

```bash
pip install pytest
pytest -v
```

---

## 5. 라이선스

MIT License (자세한 내용은 [LICENSE](LICENSE) 참조)
