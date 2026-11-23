# I.T.S.S. SCADA Server 애드온 사용 설명서

## 기능
- 통합구성요소(`custom_components/itss`)가 전송하는 회차별 KST 정밀 시각 및 제어 이력을 SQLite DB에 저장합니다.
- `미확인` 또는 `null` 필드를 감지하여 모니터링 품질 지표를 제공합니다.
- 축적된 실측 온도·난방 이력을 바탕으로 방별 적응형 RC 파라미터(`C_f`, `C_r`, `R_fr`, `R_ra`, `Q_heat`) 피팅 API를 제공합니다.

## 주요 API 엔드포인트
- `GET /health`: 서버 상태 확인
- `POST /api/v1/cycles`: 회차 제어/관측 이력 저장
- `GET /api/v1/cycles`: 최근 회차 이력 조회 (`?room_name=...&limit=50`)
- `POST /api/v1/rc/fit`: 방별 실측 시계열을 입력받아 RC 파라미터 추정

