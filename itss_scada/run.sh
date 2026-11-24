#!/usr/bin/with-contenv sh
set -eu

# S6 container_environment 에 SUPERVISOR_TOKEN 이 있으면 환경변수로 내보냄 (폴백)
if [ -z "${SUPERVISOR_TOKEN:-}" ] && [ -f /run/s6/container_environment/SUPERVISOR_TOKEN ]; then
  SUPERVISOR_TOKEN="$(cat /run/s6/container_environment/SUPERVISOR_TOKEN)"
  export SUPERVISOR_TOKEN
fi

export ITSS_OPTIONS_PATH="${ITSS_OPTIONS_PATH:-/data/options.json}"
export ITSS_DB_PATH="${ITSS_DB_PATH:-/data/itss_scada.db}"
echo "[ITSS] Booting I.T.S.S. Thermal SCADA v${ITSS_BUILD_VERSION:-unknown} ($(python3 --version 2>&1))"
# Ingress(172.30.32.2) 전용 접속 - 앱 내부 IP 필터가 그 외 접속을 403으로 차단
# 모듈 import 시 부수효과가 없도록 app factory 방식으로 기동
exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8099 --no-server-header
