#!/usr/bin/env sh
set -eu

export ITSS_OPTIONS_PATH="${ITSS_OPTIONS_PATH:-/data/options.json}"
export ITSS_DB_PATH="${ITSS_DB_PATH:-/data/itss_scada.db}"
exec uvicorn app.main:app --host 0.0.0.0 --port 8099

