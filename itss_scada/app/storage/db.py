"""SQLite persistence layer for I.T.S.S. rooms configuration and cycle audit logs."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from ..models.schema import now_kst_iso


class ScadaDatabase:
    """Stores per-room control settings (target temp, manual mode) and KST cycle logs."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rooms (
                    room_name TEXT PRIMARY KEY,
                    room_temp_entity TEXT NOT NULL,
                    climate_entity TEXT NOT NULL,
                    has_floor_sensor INTEGER NOT NULL DEFAULT 0,
                    floor_offset_c REAL NOT NULL DEFAULT 0.0,
                    target_temp REAL NOT NULL DEFAULT 23.0,
                    control_mode TEXT NOT NULL DEFAULT 'AUTO',
                    manual_action TEXT NOT NULL DEFAULT 'HOLD',
                    weight REAL NOT NULL DEFAULT 1.0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cycles (
                    cycle_id TEXT PRIMARY KEY,
                    room_name TEXT NOT NULL,
                    scheduled_at TEXT,
                    execution_started_at TEXT,
                    control_issued_at TEXT,
                    control_confirmed_at TEXT,
                    observed_at TEXT,
                    saved_at TEXT,
                    execution_status TEXT,
                    command_action TEXT,
                    safety_reason TEXT,
                    room_temp REAL,
                    floor_temp REAL,
                    target_temp REAL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cycles_room_time "
                "ON cycles (room_name, execution_started_at DESC)"
            )

    def upsert_room(self, room: dict[str, Any]) -> dict[str, Any]:
        """Insert or update a room configuration."""
        now = now_kst_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rooms (
                    room_name, room_temp_entity, climate_entity,
                    has_floor_sensor, floor_offset_c, target_temp,
                    control_mode, manual_action, weight, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_name) DO UPDATE SET
                    room_temp_entity=excluded.room_temp_entity,
                    climate_entity=excluded.climate_entity,
                    has_floor_sensor=excluded.has_floor_sensor,
                    floor_offset_c=excluded.floor_offset_c,
                    target_temp=excluded.target_temp,
                    control_mode=excluded.control_mode,
                    manual_action=excluded.manual_action,
                    weight=excluded.weight,
                    updated_at=excluded.updated_at
                """,
                (
                    room["roomName"],
                    room["roomTempEntity"],
                    room["climateEntity"],
                    1 if room.get("hasFloorSensor") else 0,
                    float(room.get("floorOffsetC", 0.0)),
                    round(float(room.get("targetTemp", 23.0)), 1),
                    str(room.get("controlMode", "AUTO")).upper(),
                    str(room.get("manualAction", "HOLD")).upper(),
                    float(room.get("weight", 1.0)),
                    now,
                ),
            )
        return self.get_room(room["roomName"]) or {}

    def update_room_control(
        self,
        room_name: str,
        *,
        target_temp: float | None = None,
        control_mode: str | None = None,
        manual_action: str | None = None,
        floor_offset_c: float | None = None,
    ) -> dict[str, Any] | None:
        """Partially update room target temperature or manual/auto mode."""
        existing = self.get_room(room_name)
        if not existing:
            return None
        if target_temp is not None:
            existing["targetTemp"] = round(float(target_temp), 1)
        if control_mode is not None:
            existing["controlMode"] = control_mode.upper()
        if manual_action is not None:
            existing["manualAction"] = manual_action.upper()
        if floor_offset_c is not None:
            existing["floorOffsetC"] = round(float(floor_offset_c), 2)
        return self.upsert_room(existing)

    def delete_room(self, room_name: str) -> bool:
        """Delete a room configuration by name."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM rooms WHERE room_name = ?", (room_name,))
            return cur.rowcount > 0

    def get_room(self, room_name: str) -> dict[str, Any] | None:
        """Return one room config dict or None."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rooms WHERE room_name = ?", (room_name,)).fetchone()
        return self._row_to_room(row) if row else None

    def list_rooms(self) -> list[dict[str, Any]]:
        """List all configured rooms ordered with master floor sensor room first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rooms ORDER BY has_floor_sensor DESC, room_name ASC"
            ).fetchall()
        return [self._row_to_room(r) for r in rows]

    @staticmethod
    def _row_to_room(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "roomName": row["room_name"],
            "roomTempEntity": row["room_temp_entity"],
            "climateEntity": row["climate_entity"],
            "hasFloorSensor": bool(row["has_floor_sensor"]),
            "floorOffsetC": row["floor_offset_c"],
            "targetTemp": row["target_temp"],
            "controlMode": row["control_mode"],
            "manualAction": row["manual_action"],
            "weight": row["weight"],
            "updatedAt": row["updated_at"],
        }

    def insert_cycle(self, payload: dict[str, Any]) -> None:
        """Insert or replace a single cycle audit record."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cycles (
                    cycle_id, room_name, scheduled_at, execution_started_at,
                    control_issued_at, control_confirmed_at, observed_at, saved_at,
                    execution_status, command_action, safety_reason,
                    room_temp, floor_temp, target_temp, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["cycleId"],
                    payload["roomName"],
                    payload.get("scheduledAt"),
                    payload.get("executionStartedAt"),
                    payload.get("controlIssuedAt"),
                    payload.get("controlConfirmedAt"),
                    payload.get("observedAt"),
                    payload.get("savedAt"),
                    payload.get("executionStatus"),
                    payload.get("commandAction"),
                    payload.get("safetyReason"),
                    payload.get("roomTemp"),
                    payload.get("floorTemp"),
                    payload.get("targetTemp"),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def list_cycles(self, room_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Query recent cycle records ordered by execution time descending."""
        with self._connect() as conn:
            if room_name:
                rows = conn.execute(
                    "SELECT payload_json FROM cycles WHERE room_name = ? "
                    "ORDER BY execution_started_at DESC LIMIT ?",
                    (room_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM cycles "
                    "ORDER BY execution_started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]
