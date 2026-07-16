"""Resolve gold expected_slugs to seed-checkpoint CIDs for one run."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from wevibe_bench import recall_gold


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolve_repo_path(raw_path: str, *, repo_root: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def _is_under_runs(path: Path, *, repo_root: Path) -> bool:
    runs_root = (repo_root / "runs").resolve()
    target = path.resolve()
    try:
        target.relative_to(runs_root)
        return True
    except ValueError:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gold",
        default="recall/gold/go-concurrency-v1.gold.jsonl",
        help="Path to gold jsonl (default: recall/gold/go-concurrency-v1.gold.jsonl from repo root)",
    )
    parser.add_argument(
        "--corpus",
        default="recall/corpus/go-concurrency-v1.json",
        help="Path to corpus json (default: recall/corpus/go-concurrency-v1.json from repo root)",
    )
    parser.add_argument("--checkpoint", required=True, help="Seed checkpoint JSON path")
    parser.add_argument("--out", required=True, help="Run-scoped output path under runs/")
    parser.add_argument("--run-id", default=_default_run_id(), help="Run identifier (default: UTC timestamp)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = _repo_root()

    gold_path = _resolve_repo_path(args.gold, repo_root=repo_root)
    corpus_path = _resolve_repo_path(args.corpus, repo_root=repo_root)
    checkpoint_path = _resolve_repo_path(args.checkpoint, repo_root=repo_root)
    out_path = _resolve_repo_path(args.out, repo_root=repo_root)

    if not _is_under_runs(out_path, repo_root=repo_root):
        print(
            f"--out must be run-scoped under {(repo_root / 'runs').resolve()} (refusing committed path)",
            file=sys.stderr,
        )
        return 1

    try:
        recall_gold.resolve_from_files(
            gold_path,
            corpus_path,
            checkpoint_path,
            run_id=args.run_id,
            out_path=out_path,
        )
    except (recall_gold.GoldError, recall_gold.ResolveError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
