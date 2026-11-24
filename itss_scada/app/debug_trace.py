"""실운전 분석용 로깅/트레이스 유틸리티.

- 텍스트 로그: 'itss' 로거 -> stdout(애드온 로그 탭) + 회전 파일(<data>/logs/itss.log, 2MB x 5개)
- JSONL 트레이스: 회차·방별 판단 근거 1줄 = 1 JSON 객체 (<data>/trace/trace-YYYYMMDD.jsonl)
  pandas.read_json(path, lines=True) 로 바로 분석 가능. 보존 기간 지난 파일은 자동 삭제.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import re
import sys
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Any

from .const import KST

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_FILE_MAX_BYTES = 2 * 1024 * 1024
LOG_FILE_BACKUPS = 5
TRACE_DATE_PATTERN = re.compile(r"^\d{8}$")

_LOGGER = logging.getLogger("itss.trace")


def configure_logging(level_name: str | None, log_dir: str | None = None) -> str | None:
    """'itss' 로거 구성. 반환값: 파일 로그 경로 (파일 로그 미사용/실패 시 None)."""
    level = getattr(logging, str(level_name or "info").upper(), logging.INFO)
    logger = logging.getLogger("itss")
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter(LOG_FORMAT)

    if not any(getattr(h, "_itss_stdout", False) for h in logger.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh._itss_stdout = True  # type: ignore[attr-defined]
        logger.addHandler(sh)

    if not log_dir:
        return None
    log_path = os.path.join(log_dir, "itss.log")
    # 파일 핸들러는 하나만 유지 (경로가 바뀌면 이전 핸들러를 닫고 교체)
    for h in list(logger.handlers):
        existing = getattr(h, "_itss_file", None)
        if existing == os.path.abspath(log_path):
            return log_path
        if existing:
            logger.removeHandler(h)
            h.close()
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(log_path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8")
        fh.setFormatter(fmt)
        fh._itss_file = os.path.abspath(log_path)  # type: ignore[attr-defined]
        logger.addHandler(fh)
        return log_path
    except OSError as err:
        logger.warning("파일 로그 생성 실패 (%s): %s", log_path, err)
        return None


def tail_log(log_path: str | None, lines: int = 300) -> list[str]:
    """텍스트 로그 마지막 N줄."""
    if not log_path or not os.path.isfile(log_path):
        return []
    with open(log_path, encoding="utf-8", errors="replace") as f:
        return [ln.rstrip("\n") for ln in deque(f, maxlen=max(1, lines))]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    return str(obj)


class TraceWriter:
    """회차·방별 판단 근거를 일자별 JSONL 파일로 기록."""

    def __init__(self, trace_dir: str | None, *, enabled: bool = True, retention_days: int = 7) -> None:
        self.trace_dir = trace_dir
        self.enabled = bool(enabled and trace_dir)
        self.retention_days = max(1, int(retention_days))
        self.last_error: str | None = None
        if self.enabled:
            try:
                os.makedirs(trace_dir, exist_ok=True)  # type: ignore[arg-type]
            except OSError as err:
                self.enabled = False
                self.last_error = str(err)
                _LOGGER.warning("트레이스 디렉터리 생성 실패 (%s): %s", trace_dir, err)

    def path_for(self, day: str) -> str | None:
        """day: YYYYMMDD. 경로 조작 방지를 위해 형식 검증."""
        if not self.trace_dir or not TRACE_DATE_PATTERN.match(day):
            return None
        return os.path.join(self.trace_dir, f"trace-{day}.jsonl")

    def write(self, record: dict[str, Any], now: datetime | None = None) -> None:
        if not self.enabled:
            return
        now = now or datetime.now(tz=KST)
        path = self.path_for(now.strftime("%Y%m%d"))
        if path is None:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
        except OSError as err:
            # 트레이스 실패가 제어를 멈추면 안 됨: 1회만 경고
            if self.last_error != str(err):
                _LOGGER.warning("트레이스 기록 실패 (%s): %s", path, err)
            self.last_error = str(err)

    def list_files(self) -> list[dict[str, Any]]:
        if not self.trace_dir or not os.path.isdir(self.trace_dir):
            return []
        out = []
        for name in sorted(os.listdir(self.trace_dir)):
            m = re.match(r"^trace-(\d{8})\.jsonl(\.gz)?$", name)
            if not m:
                continue
            full = os.path.join(self.trace_dir, name)
            out.append({
                "date": m.group(1),
                "file": name,
                "compressed": bool(m.group(2)),
                "sizeBytes": os.path.getsize(full),
            })
        return out

    def read(self, day: str, *, room_name: str | None = None, limit: int | None = None) -> list[str] | None:
        """해당 일자 JSONL 줄 목록 (room_name 필터, 마지막 limit 줄). 압축(.gz) 파일도 지원. 없으면 None."""
        path = self.path_for(day)
        if path is None:
            return None
        if os.path.isfile(path):
            opener = open(path, encoding="utf-8", errors="replace")
        elif os.path.isfile(path + ".gz"):
            opener = gzip.open(path + ".gz", "rt", encoding="utf-8", errors="replace")
        else:
            return None
        lines: deque[str] | list[str] = deque(maxlen=limit) if limit else []
        with opener as f:
            for ln in f:
                ln = ln.rstrip("\n")
                if not ln:
                    continue
                if room_name is not None:
                    try:
                        if json.loads(ln).get("room") != room_name:
                            continue
                    except ValueError:
                        continue
                lines.append(ln)
        return list(lines)

    def prune(self, now: datetime | None = None) -> int:
        """보존 기간이 지난 트레이스 파일 삭제 + 지난 일자 파일 gzip 압축."""
        if not self.trace_dir or not os.path.isdir(self.trace_dir):
            return 0
        now = now or datetime.now(tz=KST)
        today = now.strftime("%Y%m%d")
        cutoff = (now - timedelta(days=self.retention_days)).strftime("%Y%m%d")
        removed = 0
        for item in self.list_files():
            full = os.path.join(self.trace_dir, item["file"])
            if item["date"] < cutoff:
                try:
                    os.remove(full)
                    removed += 1
                except OSError:
                    pass
            elif item["date"] < today and not item["compressed"]:
                # 지난 일자 파일은 압축 보관 (JSONL 은 통상 수 배~10배 압축)
                try:
                    with open(full, "rb") as src, gzip.open(full + ".gz", "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.remove(full)
                except OSError as err:
                    _LOGGER.warning("트레이스 압축 실패 (%s): %s", full, err)
        if removed:
            _LOGGER.info("트레이스 보존 정책 적용: %d일 이전 파일 %d개 삭제", self.retention_days, removed)
        return removed
