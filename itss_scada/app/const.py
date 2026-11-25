"""Constants for the I.T.S.S. Home Assistant integration."""

from datetime import timedelta, timezone

DOMAIN = "itss"
VERSION = "2.9.0"

# Korea Standard Time (UTC+9)
KST = timezone(timedelta(hours=9))

# Config entry keys
CONF_ROOM_NAME = "room_name"
CONF_ROOM_TEMP_ENTITY = "room_temp_entity"
CONF_FLOOR_TEMP_ENTITY = "floor_temp_entity"
CONF_HUMIDITY_ENTITY = "humidity_entity"
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_VALVE_ENTITY = "valve_entity"
CONF_OUTDOOR_TEMP_ENTITY = "outdoor_temp_entity"
CONF_TARGET_TEMP = "target_temp"
CONF_PREDICTOR_MODEL = "predictor_model"
CONF_UPDATE_INTERVAL_SEC = "update_interval_sec"
CONF_GEMINI_API_KEY = "gemini_api_key"
CONF_WEATHER_LOCATION = "weather_location"
CONF_ZONE_STRATEGY = "zone_strategy"
CONF_HAS_FLOOR_SENSOR = "has_floor_sensor"
CONF_FLOOR_OFFSET_C = "floor_offset_c"
CONF_DRY_RUN = "dry_run"

# Multi-zone control strategies
STRATEGY_HYBRID = "hybrid"
STRATEGY_SIMULTANEOUS = "simultaneous"

# P100 post-command state re-read delay
DEFAULT_RECHECK_DELAY_SEC = 2.0

# Predictor model identifiers (ITSS-P200)
MODEL_LINEAR_V12 = "linear_v12"
MODEL_ADAPTIVE_RC = "adaptive_rc"

# Default control & safety parameters
DEFAULT_TARGET_TEMP = 23.0
DEFAULT_CONTROL_BAND_HALF = 0.15  # 0.3 degC total band (+/- 0.15 degC)
DEFAULT_UPDATE_INTERVAL_SEC = 300  # 5 minutes
DEFAULT_DB_FILENAME = "itss_scada.db"

# ITSS-P000 SENTINEL default safety limits
DEFAULT_OVERHEAT_ROOM_C = 24.0
DEFAULT_OVERHEAT_FLOOR_C = 25.0
DEFAULT_GENERAL_OFF_FLOOR_C = 25.0
DEFAULT_VENTILATION_DROP_RATE = -0.15  # degC per minute
DEFAULT_MIN_ON_SEC = 300   # 최소 가동 유지 5분 (예측 OFF 채터링 방지, 안전 OFF는 예외)
DEFAULT_MIN_OFF_SEC = 600  # 최소 휴지 유지 10분
DEFAULT_MAX_SENSOR_AGE_SEC = 7200  # 보고 주기 미학습 시 기본 stale 임계 (2시간)

# 센서 보고 주기 기반 적응형 stale 판정 (sensor_tracker)
SENSOR_STALE_MIN_SEC = 600         # 적응형 stale 임계 하한 10분
SENSOR_STALE_FACTOR = 2.0          # 최근 최대 보고 간격 x 2.0
SENSOR_MIN_INTERVALS_FOR_STALE = 6  # 보고 간격 6개 이상 관측 후 적응형 임계 사용
SENSOR_ADAPT_MIN_SPAN_SEC = 6 * 3600  # 최소 6시간 관측 후에만 임계 축소 (이벤트형 센서 heartbeat 공백 학습)
SENSOR_LONG_GAP_SEC = 120.0        # 24시간 최대 공백 이력에 저장할 최소 간격

# 1회 최소 가동 펄스 (밸브 구동 시간 고려, 이보다 짧은 펄스는 만들지 않음)
MIN_PULSE_MIN = 5.0

# v2.9.0 바닥 상승률 모델: 가동 L분 -> OFF 후 바닥 피크 ≈ 가동 시작 시 바닥 + rate x L
# 기본값 0.30°C/min: 2026-11-25 트레이스 방3 단독 8분 +2.1°C(0.26), 13분 +3.9°C(0.30) 중 보수값
DEFAULT_FLOOR_RISE_RATE_C_PER_MIN = 0.30
FLOOR_RISE_RATE_MIN_C_PER_MIN = 0.05   # 학습값 하한
FLOOR_RISE_RATE_MAX_C_PER_MIN = 0.80   # 학습값 상한
FLOOR_RATE_MIN_RUN_MIN = 3.0           # 이보다 짧은 펄스는 상승률 학습에서 제외 (공급수 잔열이 지배)
FLOOR_PEAK_MARGIN_C = 0.2              # 예측 피크가 (바닥 상한 - 여유) 이상이면 시작 금지/차단

# HA 서비스 재전송 (상태가 이미 목표와 같으면 생략, 단 이 주기마다 1회 재동기화)
DISPATCH_RESEND_MIN = 30.0

# 난방 부족(목표 미달 지속) 경보
UNDERHEAT_MARGIN_C = 0.3
UNDERHEAT_ALERT_MIN = 90.0

# 제어 루프 연속 실패 경보 횟수
LOOP_FAILURE_ALERT_COUNT = 3

# HA Ingress 프록시(Supervisor) IP + 로컬 개발용 loopback
DEFAULT_ALLOWED_CLIENT_IPS = ("172.30.32.2", "127.0.0.1", "::1")

# ITSS-P100 CORE thermostat step
THERMOSTAT_SETPOINT_STEP = 0.5
SENSOR_RESOLUTION = 0.1
HEAT_SETPOINT_MAX_BOOST_C = 3.0  # 조절기 현재온도 기준 동적 ON 설정온도 최대 상향폭

