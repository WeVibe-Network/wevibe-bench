"""Lifecycle logging helpers and trace/fingerprint utilities."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path


def new_trace_id() -> str:
    """Return a lifecycle trace id for cross-service operation correlation."""

    return f"lc-{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def fp(data: bytes | str) -> str:
    """Return the first 8 hex chars of sha256(data)."""

    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:8]


class _UtcIsoFormatter(logging.Formatter):
    """Formatter that emits UTC ISO timestamps for one-line op logs."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_logger(name: str, runs_dir: str) -> logging.Logger:
    """Create a logger writing to stderr + a timestamped run logfile.

    The logfile path is exposed as ``logger.logfile_path``.
    """

    run_root = Path(os.path.expanduser(runs_dir))
    run_root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    logfile_path = run_root / f"{ts}-{name}.log"

    logger_name = f"wevibe_bench.lifecycle.{name}.{ts}.{secrets.token_hex(2)}"
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = _UtcIsoFormatter("%(asctime)s %(levelname)s %(message)s")

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(fmt)

    file_handler = logging.FileHandler(logfile_path, encoding="utf-8")
    file_handler.setFormatter(fmt)

    logger.addHandler(stderr_handler)
    logger.addHandler(file_handler)
    logger.logfile_path = str(logfile_path)  # type: ignore[attr-defined]
    return logger
