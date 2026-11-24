"""SQLite persistence layer for rooms, P400 schedules, settings, and KST cycle logs."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from typing import Any, Iterator

from ..models.schema import now_kst_iso

DEFAULT_SCHEDULES = [
    {"slotId": "dawn", "label": "기상 예열", "startHour": 5, "endHour": 7, "offsetC": 0.1, "enabled": True},
    {"slotId": "day", "label": "주간 활동", "startHour": 7, "endHour": 18, "offsetC": 0.0, "enabled": True},
    {"slotId": "evening", "label": "저녁 귀가", "startHour": 18, "endHour": 23, "offsetC": 0.1, "enabled": True},
    {"slotId": "sleep", "label": "취침 절전", "startHour": 23, "endHour": 5, "offsetC": -0.3, "enabled": True},
]

# v2.7.0 통계/학습 쿼리용 컬럼 (payload_json 파싱 없이 24시간 집계)
_CYCLE_EXTRA_COLUMNS = {
    "combustion_confirmed": "INTEGER",
    "dry_run": "INTEGER",
    "effective_target": "REAL",
    "outdoor_temp": "REAL",
    "floor_sensor_source": "TEXT",
}


class ScadaDatabase:
    """Stores per-room config, P400 schedules, runtime KV state, and KST cycle logs."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """트랜잭션 커밋/롤백 후 연결을 명시적으로 닫는다."""
        with closing(sqlite3.connect(self.db_path, timeout=10.0)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            # 제어 루프와 웹 요청의 동시 읽기/쓰기를 위해 WAL 모드 사용
            conn.execute("PRAGMA journal_mode=WAL")
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
            # 방 필터 없는 시간순 조회/보존기간 삭제용 인덱스
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cycles_time ON cycles (execution_started_at)")

            # 기존 DB 안전 마이그레이션
            cols = {r[1] for r in conn.execute("PRAGMA table_info(rooms)").fetchall()}
            if "window_sensor_entity" not in cols:
                conn.execute("ALTER TABLE rooms ADD COLUMN window_sensor_entity TEXT NOT NULL DEFAULT ''")
            cycle_cols = {r[1] for r in conn.execute("PRAGMA table_info(cycles)").fetchall()}
            for col, col_type in _CYCLE_EXTRA_COLUMNS.items():
                if col not in cycle_cols:
                    conn.execute(f"ALTER TABLE cycles ADD COLUMN {col} {col_type}")

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
        comb = payload.get("combustionConfirmed")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cycles (
                    cycle_id, room_name, scheduled_at, execution_started_at,
                    control_issued_at, control_confirmed_at, observed_at, saved_at,
                    execution_status, command_action, safety_reason,
                    room_temp, floor_temp, target_temp, payload_json,
                    combustion_confirmed, dry_run, effective_target, outdoor_temp, floor_sensor_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    None if comb is None else (1 if comb else 0),
                    None if payload.get("dryRun") is None else (1 if payload.get("dryRun") else 0),
                    payload.get("effectiveTargetTemp"),
                    payload.get("outdoorTemp"),
                    payload.get("floorSensorSource"),
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

    def list_cycles_since(self, since_iso: str, room_name: str | None = None) -> list[dict[str, Any]]:
        """since_iso 이후 전체 payload (오래된 순). 학습/피팅용."""
        with self._connect() as conn:
            if room_name:
                rows = conn.execute(
                    "SELECT payload_json FROM cycles WHERE room_name = ? AND execution_started_at >= ? "
                    "ORDER BY execution_started_at ASC",
                    (room_name, since_iso),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM cycles WHERE execution_started_at >= ? "
                    "ORDER BY execution_started_at ASC",
                    (since_iso,),
                ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def stats_rows_since(self, since_iso: str) -> list[dict[str, Any]]:
        """KPI/보일러 통계용 경량 행 (JSON 파싱 없음, 오래된 순)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT room_name, execution_started_at, saved_at, command_action, safety_reason,
                       room_temp, target_temp, effective_target, combustion_confirmed, dry_run
                FROM cycles WHERE execution_started_at >= ?
                ORDER BY execution_started_at ASC
                """,
                (since_iso,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            comb = r["combustion_confirmed"]
            dry = r["dry_run"]
            out.append({
                "roomName": r["room_name"],
                "executionStartedAt": r["execution_started_at"],
                "savedAt": r["saved_at"],
                "commandAction": r["command_action"],
                "safetyReason": r["safety_reason"],
                "roomTemp": r["room_temp"],
                "targetTemp": r["target_temp"],
                "effectiveTargetTemp": r["effective_target"],
                "combustionConfirmed": None if comb is None else bool(comb),
                "dryRun": None if dry is None else bool(dry),
            })
        return out

    def chart_rows_since(self, room_name: str, since_iso: str) -> list[dict[str, Any]]:
        """차트용 경량 시계열 (오래된 순)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT execution_started_at, room_temp, floor_temp, target_temp, effective_target,
                       command_action, combustion_confirmed
                FROM cycles WHERE room_name = ? AND execution_started_at >= ?
                ORDER BY execution_started_at ASC
                """,
                (room_name, since_iso),
            ).fetchall()
        return [
            {
                "t": r["execution_started_at"],
                "room": r["room_temp"],
                "floor": r["floor_temp"],
                "target": r["effective_target"] if r["effective_target"] is not None else r["target_temp"],
                "heat": (r["command_action"] == "HEAT") or (r["combustion_confirmed"] == 1),
            }
            for r in rows
        ]

    def latest_cycle_by_room(self, room_names: list[str]) -> dict[str, dict[str, Any]]:
        """방별 최신 회차 1건 (인덱스 사용)."""
        out: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            for name in room_names:
                row = conn.execute(
                    "SELECT payload_json FROM cycles WHERE room_name = ? "
                    "ORDER BY execution_started_at DESC LIMIT 1",
                    (name,),
                ).fetchone()
                if row:
                    out[name] = json.loads(row["payload_json"])
        return out

    def prune_cycles(self, before_iso: str) -> int:
        """보존 기간이 지난 회차 이력 삭제. 삭제 건수 반환."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM cycles WHERE execution_started_at < ?", (before_iso,))
            return int(cur.rowcount or 0)

    def count_cycles(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0])
