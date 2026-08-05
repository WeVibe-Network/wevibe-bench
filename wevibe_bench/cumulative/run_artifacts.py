"""Write-once run manifest, append-only status stream, and scorecard builder.

This module publishes two immutable-by-design per-run artifacts alongside the
MUTABLE cumulative manifest, and a scorecard builder that reads ONLY those two
artifacts. It never touches the mutable ``CumulativeManifest`` at
``runs/cumulative/manifest.json``.

Artifacts
---------
* RunManifest — a WRITE-ONCE, immutable (frozen dataclass) identity record for
  one run. ``write_run_manifest`` refuses to overwrite an existing file
  (``FileExistsError``) so a run's identity can never drift after the first
  write.
* StatusStream — an APPEND-ONLY JSON-lines stream of per-attempt status
  records. Each line is an independently-parseable compact JSON object. A run
  that dies halfway leaves a short but VALID stream: every intact line is
  parseable, and unparseable trailing fragments are skipped on read.

Status record schema (a plain ``dict`` passed to ``StatusStream.append``)
-------------------------------------------------------------------------
Each record describes ONE attempt/cell of a scored session. Keys:

- ``type``: "attempt"
- ``schema_version``: 1
- ``sequence_index``: int — which roster cell this attempt belongs to
- ``memory_mode``: str ("on"/"off")
- ``org_id``: str
- ``served_model``: dict | None — API-reported served model, shape
  ``{"model": <requested>, "upstream_model": <served>|None}`` or None
- ``verdict``: str
- ``termination_reason``: str
- ``attempts_to_green``: int | str | None
- ``progress``: dict — the cumulative ``ProgressVector.to_dict()`` as of this
  record; for the terminal attempt of a cell this equals the cell's final
  progress.

Token accounting (injected-memory-block kept SEPARATE from work tokens):
- ``work_input_tokens``, ``work_output_tokens``, ``work_total_tokens`` (int)
- ``injected_block_est_tokens`` (int | None; None when mode off)

Injection observability (null BY CONTRACT when mode off — that null is
correct, not a defect):
- ``injected_count``, ``injected_block_chars``,
  ``injected_block_est_tokens``, ``consumer_injected_count`` (all int | None)

Extraction-attempt observability:
- ``extraction_state`` in {"never_invoked","invoked_cut_off",
  "invoked_completed","unknown"}
- ``extraction_candidate_count`` int | None.
  The absent-flag-must-NOT-read-as-pass principle: a missing value is never
  defaulted to a pass; an explicit state is always set.

Terminal outcome:
- ``terminal_outcome``: bool | None
- ``terminal_reason``: str
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .convergence import build_convergence_trend

RUN_ARTIFACTS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RunManifest:
    """Write-once identity record for a single run.

    ``served_model`` is the API-reported upstream model, never a configured
    name. All fields are informational identity; none are mutated after the
    first write.
    """

    schema_version: int = RUN_ARTIFACTS_SCHEMA_VERSION
    run_id: str = ""
    created_at: str = ""
    served_model: str | None = None
    requested_model: str | None = None
    memory_mode: str = ""
    org_id: str = ""
    source_commit: str | None = None
    worker_image_fingerprint: dict | str | None = None
    seed: int | None = None
    template_hash: str | None = None
    roster_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "run_id": self.run_id,
            "created_at": self.created_at,
            "served_model": self.served_model,
            "requested_model": self.requested_model,
            "memory_mode": self.memory_mode,
            "org_id": self.org_id,
            "source_commit": self.source_commit,
            "worker_image_fingerprint": self.worker_image_fingerprint,
            "seed": self.seed,
            "template_hash": self.template_hash,
            "roster_fingerprint": self.roster_fingerprint,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RunManifest:
        if not isinstance(d, Mapping):
            raise ValueError("run manifest must decode to a JSON object")
        return cls(
            schema_version=int(d.get("schema_version", RUN_ARTIFACTS_SCHEMA_VERSION)),
            run_id=str(d.get("run_id", "")),
            created_at=str(d.get("created_at", "")),
            served_model=d.get("served_model"),
            requested_model=d.get("requested_model"),
            memory_mode=str(d.get("memory_mode", "")),
            org_id=str(d.get("org_id", "")),
            source_commit=d.get("source_commit"),
            worker_image_fingerprint=d.get("worker_image_fingerprint"),
            seed=d.get("seed"),
            template_hash=d.get("template_hash"),
            roster_fingerprint=d.get("roster_fingerprint"),
        )


def write_run_manifest(
    path: str | os.PathLike[str],
    manifest: RunManifest,
) -> RunManifest:
    """Write a run manifest exactly once.

    Raises ``FileExistsError`` if ``path`` already exists. Writes canonical
    JSON (``sort_keys=True, separators=(",",":")``) plus a trailing newline via
    a temp file + ``os.replace`` for atomicity. Never overwrites.
    """
    manifest_path = os.fspath(path)
    if os.path.exists(manifest_path):
        raise FileExistsError(
            f"run manifest already exists; write-once invariant violated: {manifest_path}"
        )
    parent = os.path.dirname(manifest_path) or "."
    os.makedirs(parent, exist_ok=True)

    rendered = json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(manifest_path)}.tmp-",
        dir=parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, manifest_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return manifest


def load_run_manifest(path: str | os.PathLike[str]) -> RunManifest:
    """Read + validate a run manifest. Returns the parsed RunManifest."""
    manifest_path = os.fspath(path)
    with open(manifest_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError(f"run manifest at {manifest_path} must decode to an object")
    return RunManifest.from_dict(payload)


class StatusStream:
    """Append-only JSON-lines stream of per-attempt status records.

    Invariants:
    - Never truncates, rewrites prior lines, rewinds, or compacts.
    - ``append`` opens the file in append mode each call and writes one compact
      JSON line plus a newline, then flushes and fsyncs the handle.
    - ``records`` reads all parsed records in order, skipping unparseable lines
      (a run that dies halfway leaves a short-but-valid stream).
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = os.fspath(path)
        parent = os.path.dirname(self._path) or "."
        os.makedirs(parent, exist_ok=True)

    @property
    def path(self) -> str:
        return self._path

    def append(self, record: dict) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def records(self) -> list[dict]:
        parsed: list[dict] = []
        if not os.path.exists(self._path):
            return parsed
        with open(self._path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, Mapping):
                    parsed.append(dict(decoded))
        return parsed


def default_run_manifest_path(manifest_path: str | os.PathLike[str]) -> str:
    """Sibling path of the mutable manifest for the write-once run manifest."""
    path = Path(os.fspath(manifest_path))
    return str(path.with_name(f"{path.stem}.run-manifest.json"))


def default_status_stream_path(manifest_path: str | os.PathLike[str]) -> str:
    """Sibling path of the mutable manifest for the append-only status stream."""
    path = Path(os.fspath(manifest_path))
    return str(path.with_name(f"{path.stem}.status.jsonl"))


class _ScoredSession:
    """Lightweight record adapter consumed by ``build_convergence_trend``."""

    __slots__ = ("sequence_index", "session_fp", "session_id", "progress")

    def __init__(
        self,
        *,
        sequence_index: int,
        session_fp: str,
        session_id: str | None,
        progress: dict[str, Any],
    ) -> None:
        self.sequence_index = sequence_index
        self.session_fp = session_fp
        self.session_id = session_id
        self.progress = progress


def build_scorecard(
    manifest_path: str | os.PathLike[str],
    *,
    stream_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Build a scorecard reading ONLY the two published run artifacts.

    The ``manifest_path`` argument is the path of the MUTABLE cumulative
    manifest; the scorecard locates the run-manifest and status-stream as its
    siblings and does NOT read the mutable manifest itself.

    Returns a dict with ``schema_version``, ``manifest`` (the RunManifest
    identity dict), ``convergence`` (the derived trend dict), and the counts of
    parsed stream records and scored sessions.
    """
    run_manifest_path = default_run_manifest_path(manifest_path)
    resolved_stream_path = (
        default_status_stream_path(manifest_path)
        if stream_path is None
        else os.fspath(stream_path)
    )

    run_manifest = load_run_manifest(run_manifest_path)
    stream = StatusStream(resolved_stream_path)
    records = stream.records()

    # Group records by sequence_index; take the LAST record per cell that has a
    # non-None progress dict (the terminal attempt's final progress).
    best_by_cell: dict[int, dict[str, Any]] = {}
    for record in records:
        if record.get("type") != "attempt":
            continue
        progress = record.get("progress")
        if not isinstance(progress, Mapping):
            continue
        seq = record.get("sequence_index")
        if seq is None:
            continue
        best_by_cell[int(seq)] = record

    scored_sessions = [
        _ScoredSession(
            sequence_index=int(record["sequence_index"]),
            session_fp=str(record.get("session_fp") or ""),
            session_id=record.get("session_id"),
            progress=dict(record["progress"]),
        )
        for record in best_by_cell.values()
    ]

    convergence = build_convergence_trend(scored_sessions).to_dict()

    return {
        "schema_version": RUN_ARTIFACTS_SCHEMA_VERSION,
        "manifest": run_manifest.to_dict(),
        "convergence": convergence,
        "stream_records": len(records),
        "scored_sessions": len(scored_sessions),
    }


__all__ = [
    "RUN_ARTIFACTS_SCHEMA_VERSION",
    "RunManifest",
    "StatusStream",
    "build_scorecard",
    "default_run_manifest_path",
    "default_status_stream_path",
    "load_run_manifest",
    "write_run_manifest",
]