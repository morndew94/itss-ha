"""Constants for the I.T.S.S. Home Assistant integration."""

from datetime import timedelta, timezone

DOMAIN = "itss"
VERSION = "1.0.0"

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
DEFAULT_OVERHEAT_ROOM_C = 27.0
DEFAULT_OVERHEAT_FLOOR_C = 29.0
DEFAULT_GENERAL_OFF_FLOOR_C = 28.0
DEFAULT_VENTILATION_DROP_RATE = -0.15  # degC per minute
DEFAULT_MIN_ON_SEC = 600   # 10 minutes anti-chattering
DEFAULT_MIN_OFF_SEC = 600  # 10 minutes anti-chattering
DEFAULT_MAX_SENSOR_AGE_SEC = 7200  # 2 hours staleness threshold for Zigbee sensors

# ITSS-P100 CORE thermostat step
THERMOSTAT_SETPOINT_STEP = 0.5
SENSOR_RESOLUTION = 0.1

