"""Coordinator adapter for the OpenCode consumer recall gate file seam.

This module intentionally does *only* seam I/O:
- reads `wevibe-plugin-queue.json` (recalled candidates)
- writes `wevibe-plugin-decisions.json` (stored consumer decisions)
- writes `wevibe-tui-active.json` (TUI heartbeat)
- reads served-memory store for reconciliation checks.

HARD RULE: this adapter MUST NOT inject memory into prompts, MUST NOT write
AGENTS/context files, MUST NOT call `/v1/serves|denials|reports`, and MUST NOT
reimplement receipt crypto. The plugin owns all fate side effects.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .consumer_decision import (
    CONSUMER_DECISION_SCHEMA_VERSION,
    VALID_FATES,
    ConsumerCandidateDecision,
    ConsumerDecisionManifest,
    resolve_fate,
    validate_correlation,
    validate_replay,
    validate_schema,
)

QUEUE_FILENAME = "wevibe-plugin-queue.json"
DECISIONS_FILENAME = "wevibe-plugin-decisions.json"
HEARTBEAT_FILENAME = "wevibe-tui-active.json"

# HARD RULE enforcement: this module is only allowed to write decision +
# heartbeat seam files. Any attempt to write other files (AGENTS/context/etc.)
# is a correctness violation.
_ALLOWED_WRITE_FILENAMES = frozenset({DECISIONS_FILENAME, HEARTBEAT_FILENAME})


def _require_non_empty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _read_json_file(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default

    raw = path.read_text(encoding="utf-8")
    if raw.strip() == "":
        return default

    return json.loads(raw)


def _assert_allowed_write(path: Path) -> None:
    assert (
        path.name in _ALLOWED_WRITE_FILENAMES
    ), "consumer_gate may only write wevibe-plugin-decisions.json and wevibe-tui-active.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    _assert_allowed_write(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    serialized = f"{json.dumps(payload, indent=2)}\n"

    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _normalize_cid_iterable(values: Iterable[Any], *, field_name: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be an iterable of candidate CIDs")

    try:
        items = list(values)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be an iterable of candidate CIDs") from exc

    seen: set[str] = set()
    ordered: list[str] = []
    for index, raw in enumerate(items):
        cid = _require_non_empty_string(raw, field_name=f"{field_name}[{index}]")
        if cid in seen:
            continue
        seen.add(cid)
        ordered.append(cid)

    return ordered


def _normalize_stored_decision(value: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    memory_id = _require_non_empty_string(
        value.get("memoryID"),
        field_name=f"stored_decisions[{index}].memoryID",
    )
    action = _require_non_empty_string(
        value.get("action"),
        field_name=f"stored_decisions[{index}].action",
    )
    if action not in VALID_FATES:
        raise ValueError(f"stored_decisions[{index}].action must be one of {sorted(VALID_FATES)}")

    reason = value.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        raise ValueError(f"stored_decisions[{index}].reason must be a string")

    note = value.get("note", "")
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise ValueError(f"stored_decisions[{index}].note must be a string")

    timestamp = value.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError(f"stored_decisions[{index}].timestamp must be an integer")

    return {
        "memoryID": memory_id,
        "action": action,
        "reason": reason,
        "note": note,
        "timestamp": timestamp,
    }


def _decision_signature(stored: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(stored.get("memoryID", "")),
        str(stored.get("action", "")),
        str(stored.get("reason", "")),
        str(stored.get("note", "")),
    )


def _record_has_session(record: Any, *, session_id: str) -> bool:
    if not isinstance(record, Mapping):
        return False

    raw_session_ids = record.get("session_ids")
    if not isinstance(raw_session_ids, list):
        return False

    for raw_session_id in raw_session_ids:
        if isinstance(raw_session_id, str) and raw_session_id == session_id:
            return True

    return False


def default_plugin_state_dir(scoped_wevibe_dir: Path | None = None) -> Path:
    """Return the plugin state-dir location (`scopedWeVibeDir/state`).

    Plugin default root is `~/.wevibe` when no scoped root is supplied.
    """

    scoped_root = scoped_wevibe_dir if scoped_wevibe_dir is not None else (Path.home() / ".wevibe")
    return Path(scoped_root) / "state"


@dataclass(frozen=True)
class ConsumerGateOutcome:
    run_id: str
    session_id: str
    coordinator_trace: str
    accept_count: int
    deny_count: int
    block_count: int
    report_count: int
    decisions: list[tuple[str, str]]
    decisions_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "coordinator_trace": self.coordinator_trace,
            "accept_count": self.accept_count,
            "deny_count": self.deny_count,
            "block_count": self.block_count,
            "report_count": self.report_count,
            "decisions": [[cid, fate] for cid, fate in self.decisions],
            "decisions_path": self.decisions_path,
        }


@dataclass(frozen=True)
class ServedStoreReconcile:
    accepted_confirmed: list[str]
    missing_accepted: list[str]
    nonaccept_leaked: list[str]
    served_store_present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_confirmed": list(self.accepted_confirmed),
            "missing_accepted": list(self.missing_accepted),
            "nonaccept_leaked": list(self.nonaccept_leaked),
            "served_store_present": self.served_store_present,
        }


class ConsumerGateCoordinator:
    """Coordinates consumer-gate manifest application over plugin seam files."""

    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        if not isinstance(state_dir, Path):
            raise TypeError("state_dir must be a pathlib.Path")
        if not callable(clock):
            raise TypeError("clock must be callable")

        self._state_dir = state_dir
        self._clock = clock
        self._queue_path = self._state_dir / QUEUE_FILENAME
        self._decisions_path = self._state_dir / DECISIONS_FILENAME
        self._heartbeat_path = self._state_dir / HEARTBEAT_FILENAME

    def _now_ms(self) -> int:
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("clock() must return a numeric unix timestamp in seconds")
        return int(now * 1000)

    def read_recalled_candidates(self) -> list[dict[str, Any]]:
        payload = _read_json_file(self._queue_path, default=[])
        if not isinstance(payload, list):
            raise ValueError("wevibe-plugin-queue.json must contain a JSON array")

        recalled: list[dict[str, Any]] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, Mapping):
                raise ValueError(f"queue[{index}] must be a JSON object")
            recalled.append(dict(entry))
        return recalled

    def _recalled_cids(self, recalled_candidates: list[dict[str, Any]]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []

        for index, entry in enumerate(recalled_candidates):
            cid = _require_non_empty_string(
                entry.get("cid") if "cid" in entry else entry.get("id"),
                field_name=f"queue[{index}].cid",
            )

            if "id" in entry and entry.get("id") is not None:
                queue_id = _require_non_empty_string(
                    entry.get("id"),
                    field_name=f"queue[{index}].id",
                )
                if queue_id != cid:
                    raise ValueError(
                        f"queue[{index}] id/cid mismatch: id={queue_id!r}, cid={cid!r}"
                    )

            if cid in seen:
                continue
            seen.add(cid)
            ordered.append(cid)

        return ordered

    def _read_stored_decisions(self) -> list[dict[str, Any]]:
        payload = _read_json_file(self._decisions_path, default=[])
        if not isinstance(payload, list):
            raise ValueError("wevibe-plugin-decisions.json must contain a JSON array")

        normalized: list[dict[str, Any]] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, Mapping):
                raise ValueError(f"stored_decisions[{index}] must be a JSON object")
            normalized.append(_normalize_stored_decision(entry, index=index))

        return normalized

    def _stored_manifest(
        self,
        stored_decisions: list[dict[str, Any]],
        *,
        run_id: str,
        session_id: str,
        policy_id: str,
        default_fate: str,
        coordinator_trace: str,
    ) -> ConsumerDecisionManifest:
        decisions = tuple(
            ConsumerCandidateDecision(
                run_id=run_id,
                session_id=session_id,
                candidate_cid=entry["memoryID"],
                fate=entry["action"],
                coordinator_trace=coordinator_trace,
                reason=entry["reason"],
                note=entry["note"],
            )
            for entry in stored_decisions
        )

        return ConsumerDecisionManifest(
            schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
            run_id=run_id,
            policy_id=policy_id,
            default_fate=default_fate,
            decisions=decisions,
            coordinator_trace=coordinator_trace,
        )

    def write_heartbeat(self) -> None:
        _atomic_write_json(self._heartbeat_path, {"ts": self._now_ms()})

    def apply_manifest(
        self,
        manifest: ConsumerDecisionManifest,
        *,
        run_id: str,
        session_id: str,
    ) -> ConsumerGateOutcome:
        normalized_run_id = _require_non_empty_string(run_id, field_name="run_id")
        normalized_session_id = _require_non_empty_string(session_id, field_name="session_id")

        validate_schema(manifest)

        recalled_candidates = self.read_recalled_candidates()
        recalled_cids = self._recalled_cids(recalled_candidates)

        validate_correlation(
            manifest,
            run_id=normalized_run_id,
            session_id=normalized_session_id,
            recalled_cids=recalled_cids,
        )

        stored_prior = self._read_stored_decisions()
        if stored_prior:
            prior_manifest = self._stored_manifest(
                stored_prior,
                run_id=normalized_run_id,
                session_id=normalized_session_id,
                policy_id=manifest.policy_id,
                default_fate=manifest.default_fate,
                coordinator_trace=manifest.coordinator_trace,
            )
            validate_replay(prior_manifest, manifest)

        prior_by_cid = {entry["memoryID"]: entry for entry in stored_prior}
        now_ms = self._now_ms()

        stored_decisions: list[dict[str, Any]] = []
        decided_pairs: list[tuple[str, str]] = []
        accept_count = 0
        deny_count = 0
        block_count = 0
        report_count = 0

        for cid in recalled_cids:
            resolved = resolve_fate(manifest, session_id=normalized_session_id, candidate_cid=cid)
            stored = resolved.to_stored_decision(now_ms)

            prior = prior_by_cid.get(cid)
            if prior is not None and _decision_signature(prior) == _decision_signature(stored):
                stored["timestamp"] = prior["timestamp"]

            fate = resolved.fate
            if fate == "accept":
                accept_count += 1
            elif fate == "deny":
                deny_count += 1
            elif fate == "block":
                block_count += 1
            elif fate == "report":
                report_count += 1
            else:
                raise ValueError(f"unsupported fate {fate!r}")

            stored_decisions.append(stored)
            decided_pairs.append((cid, fate))

        _atomic_write_json(self._decisions_path, stored_decisions)
        self.write_heartbeat()

        return ConsumerGateOutcome(
            run_id=normalized_run_id,
            session_id=normalized_session_id,
            coordinator_trace=manifest.coordinator_trace,
            accept_count=accept_count,
            deny_count=deny_count,
            block_count=block_count,
            report_count=report_count,
            decisions=decided_pairs,
            decisions_path=str(self._decisions_path),
        )

    def served_store_reconcile(
        self,
        served_store_path: Path,
        *,
        session_id: str,
        accepted_cids: Iterable[Any],
        denied_cids: Iterable[Any],
        blocked_cids: Iterable[Any],
        reported_cids: Iterable[Any],
    ) -> ServedStoreReconcile:
        normalized_session_id = _require_non_empty_string(session_id, field_name="session_id")
        accepted = _normalize_cid_iterable(accepted_cids, field_name="accepted_cids")
        denied = _normalize_cid_iterable(denied_cids, field_name="denied_cids")
        blocked = _normalize_cid_iterable(blocked_cids, field_name="blocked_cids")
        reported = _normalize_cid_iterable(reported_cids, field_name="reported_cids")

        served_store_present = served_store_path.exists()
        memories: Mapping[str, Any]
        if served_store_present:
            payload = _read_json_file(served_store_path, default={})
            if not isinstance(payload, Mapping):
                raise ValueError("served store must be a JSON object")

            raw_memories = payload.get("memories", {})
            if not isinstance(raw_memories, Mapping):
                raise ValueError("served store memories must be a JSON object")
            memories = raw_memories
        else:
            memories = {}

        accepted_confirmed: list[str] = []
        missing_accepted: list[str] = []

        for cid in accepted:
            if _record_has_session(memories.get(cid), session_id=normalized_session_id):
                accepted_confirmed.append(cid)
            else:
                missing_accepted.append(cid)

        nonaccept_union: list[str] = []
        seen_nonaccept: set[str] = set()
        for cid in denied + blocked + reported:
            if cid in seen_nonaccept:
                continue
            seen_nonaccept.add(cid)
            nonaccept_union.append(cid)

        nonaccept_leaked: list[str] = []
        for cid in nonaccept_union:
            if _record_has_session(memories.get(cid), session_id=normalized_session_id):
                nonaccept_leaked.append(cid)

        return ServedStoreReconcile(
            accepted_confirmed=accepted_confirmed,
            missing_accepted=missing_accepted,
            nonaccept_leaked=nonaccept_leaked,
            served_store_present=served_store_present,
        )


__all__ = [
    "ConsumerGateCoordinator",
    "ConsumerGateOutcome",
    "ServedStoreReconcile",
    "default_plugin_state_dir",
]
