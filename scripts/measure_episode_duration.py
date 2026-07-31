#!/usr/bin/env python3
"""Measure the empirical episode-duration ceiling for T4 rho.

Schema note: serve_events does NOT carry episode_ref or session_id today. The
measurement therefore uses the weaker available linkage:
outcome_events.session_id -> session_served_memories.session_id, then
session_served_memories.memory_cid -> serve_events.memory_content_hash. This is
not a cryptographic episode join; it is the best timestamp proxy available from
the current pre-MVP schema and is reported as such in the CLI output.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import psycopg

from wevibe_bench.spend_key import _read_dotenv, _resolve_dotenv_path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
SERVER_DOTENV = WORKSPACE_ROOT / "wevibe-server" / ".env"
DEFAULT_HUB_DSN = "postgresql://wevibe:wevibe_dev@127.0.0.1:5433/wevibe_hub?sslmode=disable"
TARGET_RHO = 0.80

EPISODE_DURATION_QUERY = """
WITH paired_episodes AS (
    SELECT
        oe.episode_ref,
        MIN(se.created_at) AS first_serve_at,
        MAX(oe.created_at) AS last_outcome_at
    FROM outcome_events oe
    JOIN session_served_memories ssm
      ON ssm.org_id = oe.org_id
     AND ssm.session_id = oe.session_id
     AND ssm.memory_cid = oe.memory_content_hash
    JOIN serve_events se
      ON se.org_id = ssm.org_id
     AND se.memory_content_hash = ssm.memory_cid
     AND se.event_type = 'serve'
    WHERE oe.episode_ref IS NOT NULL
      AND oe.session_id IS NOT NULL
    GROUP BY oe.episode_ref
)
SELECT
    pe.episode_ref,
    EXTRACT(EPOCH FROM (pe.last_outcome_at - pe.first_serve_at)) / 60.0 AS duration_minutes
FROM paired_episodes pe
WHERE pe.first_serve_at IS NOT NULL
  AND pe.last_outcome_at IS NOT NULL
  AND pe.last_outcome_at >= pe.first_serve_at
ORDER BY pe.episode_ref;
""".strip()

UNPAIRED_SERVES_QUERY = """
SELECT COUNT(*)
FROM serve_events se
LEFT JOIN outcome_events oe
  ON oe.org_id = se.org_id
 AND oe.memory_content_hash = se.memory_content_hash
WHERE se.event_type = 'serve'
  AND oe.id IS NULL;
""".strip()


@dataclass(frozen=True)
class DurationStats:
    n: int
    min_minutes: float | None
    p50_minutes: float | None
    p90_minutes: float | None
    p95_minutes: float | None
    max_minutes: float | None
    within_window_count: int
    within_window_fraction: float | None
    verdict: str


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    if q < 0 or q > 100:
        raise ValueError("percentile q must be between 0 and 100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (q / 100.0)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = pos - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def rho_verdict(within_window_fraction: float | None, *, n: int, target: float = TARGET_RHO) -> str:
    if n <= 0 or within_window_fraction is None:
        return "UNDETERMINED-INSUFFICIENT-DATA"
    if within_window_fraction >= target:
        return "ACHIEVABLE"
    return "NOT ACHIEVABLE"


def compute_duration_stats(durations_minutes: Iterable[float], window_minutes: float) -> DurationStats:
    durations = [float(d) for d in durations_minutes]
    if window_minutes <= 0:
        raise ValueError("window_minutes must be positive")
    n = len(durations)
    if n == 0:
        return DurationStats(
            n=0,
            min_minutes=None,
            p50_minutes=None,
            p90_minutes=None,
            p95_minutes=None,
            max_minutes=None,
            within_window_count=0,
            within_window_fraction=None,
            verdict="UNDETERMINED-INSUFFICIENT-DATA",
        )
    within = sum(1 for d in durations if d <= window_minutes)
    fraction = within / n
    return DurationStats(
        n=n,
        min_minutes=min(durations),
        p50_minutes=percentile(durations, 50),
        p90_minutes=percentile(durations, 90),
        p95_minutes=percentile(durations, 95),
        max_minutes=max(durations),
        within_window_count=within,
        within_window_fraction=fraction,
        verdict=rho_verdict(fraction, n=n),
    )


def redact_dsn(dsn: str) -> str:
    parts = urlsplit(dsn)
    if not parts.password:
        return dsn
    username = parts.username or ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:REDACTED@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_hub_dsn(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    env_map = os.environ if env is None else env
    for key in ("WEVIBE_BENCH_HUB_DB_DSN", "WEVIBE_HUB_DATABASE_URL", "DATABASE_URL"):
        value = str(env_map.get(key, "")).strip()
        if value:
            return value, f"env:{key}"

    bench_dotenv = _resolve_dotenv_path(env=env_map, dotenv_path=None)
    for dotenv_path in (bench_dotenv, SERVER_DOTENV):
        dot = _read_dotenv(dotenv_path, env=env_map)
        for key in ("WEVIBE_BENCH_HUB_DB_DSN", "WEVIBE_HUB_DATABASE_URL", "DATABASE_URL"):
            value = dot.get(key, "").strip()
            if value:
                return value, f"dotenv:{dotenv_path}:{key}"

    return DEFAULT_HUB_DSN, "docker-compose default wevibe-server:127.0.0.1:5433/wevibe_hub"


def setup_logger() -> tuple[logging.Logger, Path]:
    runs_dir = REPO_ROOT / "runs"
    runs_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = runs_dir / f"{stamp}-episode-duration-rho-ceiling.log"
    logger = logging.getLogger("episode_duration")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger, log_path


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def run(window_minutes: float) -> int:
    logger, log_path = setup_logger()
    dsn, dsn_source = resolve_hub_dsn()
    redacted = redact_dsn(dsn)
    logger.info("start window_minutes=%s dsn_source=%s dsn=%s", window_minutes, dsn_source, redacted)
    logger.info("episode_duration_query=%s", EPISODE_DURATION_QUERY)
    logger.info("unpaired_serves_query=%s", UNPAIRED_SERVES_QUERY)
    print(f"logfile={log_path}")
    print(f"dsn_source={dsn_source} dsn={redacted}")
    print("linkage=WEAK: serve_events has no episode_ref/session_id; joined via outcome_events.session_id -> session_served_memories -> serve_events memory hash")
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(EPISODE_DURATION_QUERY)
                rows = cur.fetchall()
                cur.execute(UNPAIRED_SERVES_QUERY)
                unpaired_serves = int(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001 - log full unexpected DB failures for R-37.
        logger.error("outcome=error reason=db_unreachable error=%s traceback=%s", exc, traceback.format_exc())
        print(f"VERDICT UNDETERMINED-INSUFFICIENT-DATA n=0 reason=db_unreachable error={exc}")
        return 2

    durations = [float(row[1]) for row in rows]
    stats = compute_duration_stats(durations, window_minutes)
    logger.info("row_counts paired_episodes=%s unpaired_serves=%s", stats.n, unpaired_serves)
    logger.info("outcome n=%s min=%s p50=%s p90=%s p95=%s max=%s within=%s fraction=%s verdict=%s", stats.n, fmt(stats.min_minutes), fmt(stats.p50_minutes), fmt(stats.p90_minutes), fmt(stats.p95_minutes), fmt(stats.max_minutes), stats.within_window_count, fmt(stats.within_window_fraction), stats.verdict)

    print(f"episodes_n={stats.n}")
    print(f"duration_minutes min={fmt(stats.min_minutes)} p50={fmt(stats.p50_minutes)} p90={fmt(stats.p90_minutes)} p95={fmt(stats.p95_minutes)} max={fmt(stats.max_minutes)}")
    print(f"within_window window_minutes={window_minutes:g} count={stats.within_window_count} fraction={fmt(stats.within_window_fraction)}")
    print(f"unpaired_serves={unpaired_serves}")
    print(f"implied_rho_ceiling={fmt(stats.within_window_fraction)}")
    if stats.n == 0:
        print("VERDICT UNDETERMINED-INSUFFICIENT-DATA n=0 reason=no_paired_episode_rows")
        return 1
    print(f"VERDICT {stats.verdict} n={stats.n} target=T4 rho>=0.80")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure episode duration distribution and implied T4 rho ceiling.")
    parser.add_argument("--window-minutes", type=float, default=1440.0, help="Pending serve window in minutes; default 1440 (24h).")
    args = parser.parse_args(argv)
    return run(args.window_minutes)


if __name__ == "__main__":
    raise SystemExit(main())
