"""SQLite persistence layer for I.T.S.S. cycle audit logs inside Home Assistant."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any


class ScadaDatabase:
    """Stores cycle audit payloads with indexed KST timestamps."""

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

