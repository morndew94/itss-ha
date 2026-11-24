#!/usr/bin/with-contenv sh
set -eu

# Fallback: export SUPERVISOR_TOKEN from S6 container_environment if present
if [ -z "${SUPERVISOR_TOKEN:-}" ] && [ -f /run/s6/container_environment/SUPERVISOR_TOKEN ]; then
  SUPERVISOR_TOKEN="$(cat /run/s6/container_environment/SUPERVISOR_TOKEN)"
  export SUPERVISOR_TOKEN
fi

export ITSS_OPTIONS_PATH="${ITSS_OPTIONS_PATH:-/data/options.json}"
export ITSS_DB_PATH="${ITSS_DB_PATH:-/data/itss_scada.db}"
echo "[ITSS] Booting I.T.S.S. Thermal SCADA v${ITSS_BUILD_VERSION:-unknown} ($(python3 --version 2>&1))"
exec uvicorn app.main:app --host 0.0.0.0 --port 8099
