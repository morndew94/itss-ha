"""SQLite persistence layer for rooms, P400 schedules, settings, and KST cycle logs."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from ..models.schema import now_kst_iso

DEFAULT_SCHEDULES = [
    {"slotId": "dawn", "label": "기상 예열", "startHour": 5, "endHour": 7, "offsetC": 0.1, "enabled": True},
    {"slotId": "day", "label": "주간 활동", "startHour": 7, "endHour": 18, "offsetC": 0.0, "enabled": True},
    {"slotId": "evening", "label": "저녁 귀가", "startHour": 18, "endHour": 23, "offsetC": 0.1, "enabled": True},
    {"slotId": "sleep", "label": "취침 절전", "startHour": 23, "endHour": 5, "offsetC": -0.3, "enabled": True},
]


class ScadaDatabase:
    """Stores per-room config, P400 schedules, runtime KV state, and KST cycle logs."""

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
                    window_sensor_entity TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS schedules (
                    slot_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    offset_c REAL NOT NULL DEFAULT 0.0,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
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
            # Safe schema migration for existing DBs upgrading to v2.5.0
            cols = {r[1] for r in conn.execute("PRAGMA table_info(rooms)").fetchall()}
            if "window_sensor_entity" not in cols:
                conn.execute("ALTER TABLE rooms ADD COLUMN window_sensor_entity TEXT NOT NULL DEFAULT ''")
            # Initialize default P400 schedule slots once
            cnt = conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
            if cnt == 0:
                for s in DEFAULT_SCHEDULES:
                    conn.execute(
                        "INSERT INTO schedules (slot_id, label, start_hour, end_hour, offset_c, enabled) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (s["slotId"], s["label"], s["startHour"], s["endHour"], s["offsetC"], 1),
                    )

    # --- KV store ---
    def set_kv(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store (key, value_json) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM kv_store WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    # --- Schedules (P400 CIRCADIA) ---
    def list_schedules(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM schedules ORDER BY start_hour ASC").fetchall()
        return [
            {
                "slotId": r["slot_id"],
                "label": r["label"],
                "startHour": r["start_hour"],
                "endHour": r["end_hour"],
                "offsetC": r["offset_c"],
                "enabled": bool(r["enabled"]),
            }
            for r in rows
        ]

    def replace_schedules(self, slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.execute("DELETE FROM schedules")
            for s in slots:
                conn.execute(
                    "INSERT INTO schedules (slot_id, label, start_hour, end_hour, offset_c, enabled) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        s["slotId"],
                        s["label"],
                        int(s["startHour"]),
                        int(s["endHour"]),
                        round(float(s["offsetC"]), 2),
                        1 if s.get("enabled", True) else 0,
                    ),
                )
        return self.list_schedules()

    def active_schedule_offset(self, hour: int) -> tuple[float, str | None]:
        """Return (offset_c, slot_label) matching the current KST hour."""
        for s in self.list_schedules():
            if not s["enabled"]:
                continue
            sh, eh = s["startHour"], s["endHour"]
            in_slot = (sh <= hour < eh) if sh < eh else (hour >= sh or hour < eh)
            if in_slot:
                return float(s["offsetC"]), s["label"]
        return 0.0, None

    # --- Rooms ---
    def upsert_room(self, room: dict[str, Any]) -> dict[str, Any]:
        now = now_kst_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rooms (
                    room_name, room_temp_entity, climate_entity, window_sensor_entity,
                    has_floor_sensor, floor_offset_c, target_temp,
                    control_mode, manual_action, weight, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_name) DO UPDATE SET
                    room_temp_entity=excluded.room_temp_entity,
                    climate_entity=excluded.climate_entity,
                    window_sensor_entity=excluded.window_sensor_entity,
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
                    str(room.get("windowSensorEntity", "") or "").strip(),
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
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM rooms WHERE room_name = ?", (room_name,))
            return cur.rowcount > 0

    def get_room(self, room_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rooms WHERE room_name = ?", (room_name,)).fetchone()
        return self._row_to_room(row) if row else None

    def list_rooms(self) -> list[dict[str, Any]]:
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
            "windowSensorEntity": row["window_sensor_entity"] if "window_sensor_entity" in row.keys() else "",
            "hasFloorSensor": bool(row["has_floor_sensor"]),
            "floorOffsetC": row["floor_offset_c"],
            "targetTemp": row["target_temp"],
            "controlMode": row["control_mode"],
            "manualAction": row["manual_action"],
            "weight": row["weight"],
            "updatedAt": row["updated_at"],
        }

    # --- Cycles ---
    def insert_cycle(self, payload: dict[str, Any]) -> None:
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
