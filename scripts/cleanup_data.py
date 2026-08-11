"""Telemetry retention cleanup for wevibe-bench.

Deletes entries directly under ``data/cells/`` and ``data/extract/`` whose mtime
is older than 7 days. ``data/`` is a TELEMETRY/RETENTION layer only, NEVER a
competing source of truth -- ``runs/`` (RC-5 manifest/status) stays
authoritative. This script never touches ``runs/``, ``data/README.md``, or
``data/``'s top-level structure. Dotfiles (e.g. ``.gitkeep``) under the retention
dirs are always preserved so the empty dirs remain tracked.

The "silent reaper is not a reaper" ethos applies: every removed entry is logged.

Env overrides:
    WEVIBE_BENCH_DATA_DIR    absolute path to the data dir (default: repo/data)
    WEVIBE_BENCH_SKIP_CLEANUP  set to ``1`` to skip cleanup at the run entrypoint
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

_RETENTION_DAYS = 7
_LOG = logging.getLogger("cleanup_data")


def resolve_data_dir() -> Path:
    """Data dir: WEVIBE_BENCH_DATA_DIR env override, else repo root ``data/``."""
    override = os.environ.get("WEVIBE_BENCH_DATA_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "data"


def _retention_cutoff() -> float:
    """Epoch seconds older than which an entry is considered expired (7 days)."""
    return time.time() - (_RETENTION_DAYS * 24 * 3600)


def cleanup_entries(data_dir: Path | None = None, dry_run: bool = False) -> int:
    """Delete entries under ``cells/`` and ``extract/`` older than 7 days.

    Creates the subdirs if absent; never raises on missing dirs. Preserves
    dotfiles (``.gitkeep``) and anything not directly under ``cells/``/``extract/``
    (top-level structure like ``README.md`` is untouched). Robust to any entry
    name -- expiry is decided purely by mtime, never by parsing the name.
    Returns the number of entries actually removed (0 in dry-run).
    """
    data_dir = data_dir or resolve_data_dir()
    cutoff = _retention_cutoff()
    removed = 0
    for sub in ("cells", "extract"):
        sub_dir = data_dir / sub
        sub_dir.mkdir(parents=True, exist_ok=True)
        for entry in sorted(sub_dir.iterdir()):
            if entry.name.startswith("."):
                _LOG.info("cleanup_data preserve dotfile %s", entry)
                continue
            try:
                stale = entry.stat().st_mtime < cutoff
            except OSError:
                _LOG.warning("cleanup_data cannot stat %s; skipping", entry)
                continue
            if stale:
                if dry_run:
                    _LOG.info("cleanup_data would remove %s (dry-run)", entry)
                else:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                    _LOG.info("cleanup_data removed %s", entry)
                    removed += 1
    return removed


def run_cleanup(data_dir: Path | None = None, dry_run: bool = False) -> int:
    """Entrypoint for the run handler: resolves the data dir and cleans it."""
    return cleanup_entries(data_dir=data_dir, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retain wevibe-bench telemetry (7-day window)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be removed without deleting anything",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    data_dir = resolve_data_dir()
    _LOG.info("cleanup_data data_dir=%s dry_run=%s", data_dir, args.dry_run)
    removed = run_cleanup(data_dir=data_dir, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    _LOG.info("cleanup_data %s %d entries", verb, removed)
    return 0


if __name__ == "__main__":
    sys.exit(main())