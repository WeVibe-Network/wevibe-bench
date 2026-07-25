"""Transport-only consumer bridge daemon over the plugin file seam.

HARD RULE (non-negotiable): this daemon only validates + transports an external
coordinator-authored consumer-decision manifest into the vendored OpenCode
plugin seam. It NEVER injects memory into prompts, NEVER writes AGENTS/context
files, NEVER calls `/v1/serves|denials|reports`, NEVER performs receipt crypto,
and NEVER chooses/defaults/infers a fate itself.

All decision side effects are owned by the plugin coordinator path
(`ConsumerGateCoordinator.apply_manifest`). If a manifest is missing, stale,
invalid, or correlation-mismatched, the bridge emits NO decision.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

from ._validation import _require_non_empty_string
from .bridge_state import (
    BridgeDaemonState,
    DeliveredDecision,
    WorkerLease,
    already_delivered_cids,
    atomic_write_state,
    compute_manifest_digest,
    mark_acked,
    record_delivery,
    resume_or_create_state,
)
from .consumer_decision import (
    ConflictError as ConsumerConflictError,
    ConsumerDecisionManifest,
    validate_correlation,
    validate_schema,
)
from .consumer_gate import ConsumerGateCoordinator

MANIFEST_SUFFIX = ".consumer-manifest.json"
DEFAULT_POLL_INTERVAL_MS = 1_000
DEFAULT_HEARTBEAT_CADENCE_MS = 5_000
DEFAULT_LEASE_TTL_MS = 120_000


def _require_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def scope_key(run_id: str, session_id: str) -> str:
    return f"{run_id}::{session_id}"


def manifest_inbox_name(run_id: str, session_fp: str) -> str:
    return f"{run_id}__{session_fp}{MANIFEST_SUFFIX}"


class ConsumerBridge:
    def __init__(
        self,
        *,
        coordinator: ConsumerGateCoordinator,
        state_path: Path,
        manifest_inbox: Path,
        served_store_path: Path,
        run_id: str,
        session_id: str,
        session_fp: str,
        container_name: str | None = None,
        clock: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
        lease_ttl_ms: int = DEFAULT_LEASE_TTL_MS,
        heartbeat_cadence_ms: int = DEFAULT_HEARTBEAT_CADENCE_MS,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
    ) -> None:
        if not isinstance(coordinator, ConsumerGateCoordinator):
            raise TypeError("coordinator must be a ConsumerGateCoordinator")
        if not isinstance(state_path, Path):
            raise TypeError("state_path must be a pathlib.Path")
        if not isinstance(manifest_inbox, Path):
            raise TypeError("manifest_inbox must be a pathlib.Path")
        if not isinstance(served_store_path, Path):
            raise TypeError("served_store_path must be a pathlib.Path")
        if not callable(clock):
            raise TypeError("clock must be callable")

        self.coordinator = coordinator
        self.state_path = state_path
        self.manifest_inbox = manifest_inbox
        self.served_store_path = served_store_path
        self.run_id = _require_non_empty_string(run_id, field_name="run_id")
        self.session_id = _require_non_empty_string(session_id, field_name="session_id")
        self.session_fp = _require_non_empty_string(session_fp, field_name="session_fp")
        self.container_name = _require_non_empty_string(
            container_name if container_name is not None else f"consumer-bridge-{os.getpid()}",
            field_name="container_name",
        )
        self.clock = clock
        self._logger = logger if logger is not None else logging.getLogger("consumer_bridge")
        self.lease_ttl_ms = _require_positive_int(lease_ttl_ms, field_name="lease_ttl_ms")
        self.heartbeat_cadence_ms = _require_positive_int(
            heartbeat_cadence_ms,
            field_name="heartbeat_cadence_ms",
        )
        self.poll_interval_ms = _require_positive_int(poll_interval_ms, field_name="poll_interval_ms")

        self._scope_key = scope_key(self.run_id, self.session_id)
        self._last_manifest_reason = "no_manifest"
        self._cycle = 0

        self.state: BridgeDaemonState = resume_or_create_state(
            self.state_path,
            run_id=self.run_id,
            session_id=self.session_id,
            session_fp=self.session_fp,
            container_name=self.container_name,
        )

        started_at_ms = self._now_ms()
        self.state.lease = WorkerLease(
            pid=os.getpid(),
            started_at_ms=started_at_ms,
            ttl_ms=self.lease_ttl_ms,
            expires_at_ms=started_at_ms + self.lease_ttl_ms,
        )
        self.state.resume_marker = "active"
        atomic_write_state(self.state_path, self.state)

    def _now_ms(self) -> int:
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("clock() must return a unix timestamp in seconds")
        return int(now * 1000)

    def _lease_remaining_ms(self, now_ms: int) -> int:
        lease = self.state.lease
        if lease is None:
            return 0
        return max(0, lease.expires_at_ms - now_ms)

    def _is_lease_expired(self, now_ms: int) -> bool:
        lease = self.state.lease
        if lease is None:
            return True
        return lease.is_expired(now_ms)

    def refresh_heartbeat(self) -> None:
        self.coordinator.write_heartbeat()
        now_ms = self._now_ms()
        self.state.heartbeat_last_ts_ms = now_ms
        if self.state.lease is not None:
            # started_at_ms tracks the most recent renewal so from_dict's
            # invariant remains true: started_at_ms + ttl_ms == expires_at_ms.
            self.state.lease = dataclasses.replace(
                self.state.lease,
                started_at_ms=now_ms,
                expires_at_ms=now_ms + self.lease_ttl_ms,
            )
            self._logger.debug(
                "LEASE_RENEWED scope=%s expires_at_ms=%s ttl_ms=%s",
                self._scope_key,
                self.state.lease.expires_at_ms,
                self.state.lease.ttl_ms,
            )
        self.state.resume_marker = "active"
        atomic_write_state(self.state_path, self.state)

    def _manifest_path(self) -> Path:
        return self.manifest_inbox / manifest_inbox_name(self.run_id, self.session_fp)

    def _load_coordinator_manifest(self) -> ConsumerDecisionManifest | None:
        manifest_path = self._manifest_path()
        if not manifest_path.exists():
            self._last_manifest_reason = "no_manifest"
            return None

        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("manifest payload must be a JSON object")
            manifest = ConsumerDecisionManifest.from_dict(payload)
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
            self._last_manifest_reason = "invalid_manifest"
            self._logger.warning(
                "consumer_bridge manifest rejected scope=%s reason=invalid_manifest error=%s",
                self._scope_key,
                exc,
            )
            return None

        self._last_manifest_reason = "ok"
        return manifest

    def _read_pending_decision_cids(self) -> set[str] | None:
        decisions_path = getattr(self.coordinator, "_decisions_path", None)
        if not isinstance(decisions_path, Path):
            return None
        if not decisions_path.exists():
            return set()

        try:
            payload = json.loads(decisions_path.read_text(encoding="utf-8") or "[]")
            if not isinstance(payload, list):
                raise ValueError("decisions seam payload must be a JSON array")

            cids: set[str] = set()
            for index, entry in enumerate(payload):
                if not isinstance(entry, Mapping):
                    raise ValueError(f"decisions[{index}] must be a JSON object")
                cid = _require_non_empty_string(entry.get("memoryID"), field_name="memoryID")
                cids.add(cid)
            return cids
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
            self._logger.error(
                "consumer_bridge failed decisions re-read scope=%s error=%s",
                self._scope_key,
                exc,
            )
            return None

    def _record_fate_buckets(self, record: Any) -> dict[str, list[str]]:
        buckets: dict[str, list[str]] = {"accept": [], "deny": [], "block": [], "report": []}
        if record is None:
            return buckets

        for decision in record.delivered:
            buckets[decision.fate].append(decision.candidate_cid)
        return buckets

    def _observe_ack_and_outcomes(self) -> dict[str, Any]:
        record = self.state.consumed_manifests.get(self._scope_key)
        if record is None:
            return {
                "fate_counts": {"accept": 0, "deny": 0, "block": 0, "report": 0},
                "delivered_cids": [],
                "ack_summary": {
                    "pending": 0,
                    "drained": 0,
                    "newly_drained": [],
                    "drained_cids": [],
                },
                "reconcile": {
                    "served_store_present": self.served_store_path.exists(),
                    "served_confirmed": [],
                    "missing_accepted": [],
                    "nonaccept_leaked": [],
                },
                "side_effect_timeouts": [],
            }

        pending_file_cids = self._read_pending_decision_cids()
        newly_drained: list[str] = []
        if pending_file_cids is not None:
            for delivered in record.delivered:
                if delivered.ack_status == "drained":
                    continue
                if delivered.candidate_cid in pending_file_cids:
                    continue
                mark_acked(self.state, self._scope_key, delivered.candidate_cid, "drained")
                newly_drained.append(delivered.candidate_cid)

            if newly_drained:
                record = self.state.consumed_manifests[self._scope_key]

        fate_buckets = self._record_fate_buckets(record)
        accepted_cids = _unique(list(fate_buckets["accept"]))
        denied_cids = _unique(list(fate_buckets["deny"]))
        blocked_cids = _unique(list(fate_buckets["block"]))
        reported_cids = _unique(list(fate_buckets["report"]))

        reconcile_summary = {
            "served_store_present": self.served_store_path.exists(),
            "served_confirmed": [],
            "missing_accepted": [],
            "nonaccept_leaked": [],
        }

        try:
            reconcile = self.coordinator.served_store_reconcile(
                self.served_store_path,
                session_id=self.session_id,
                accepted_cids=accepted_cids,
                denied_cids=denied_cids,
                blocked_cids=blocked_cids,
                reported_cids=reported_cids,
            )
            reconcile_summary = {
                "served_store_present": reconcile.served_store_present,
                "served_confirmed": list(reconcile.accepted_confirmed),
                "missing_accepted": list(reconcile.missing_accepted),
                "nonaccept_leaked": list(reconcile.nonaccept_leaked),
            }
        except ValueError as exc:
            self._logger.error(
                "consumer_bridge reconcile failed scope=%s error=%s",
                self._scope_key,
                exc,
            )

        self.state.plugin_outcome_refs["served_confirmed"] = list(reconcile_summary["served_confirmed"])
        self.state.plugin_outcome_refs["missing_accepted"] = list(reconcile_summary["missing_accepted"])
        self.state.plugin_outcome_refs["nonaccept_leaked"] = list(reconcile_summary["nonaccept_leaked"])
        self.state.plugin_outcome_refs["denied"] = denied_cids
        self.state.plugin_outcome_refs["reported"] = reported_cids
        self.state.plugin_outcome_refs["blocked"] = blocked_cids
        self.state.plugin_outcome_refs["side_effect_timeouts"] = list(
            reconcile_summary["missing_accepted"]
        )

        self.state.resume_marker = "active"
        atomic_write_state(self.state_path, self.state)

        drained_cids = [d.candidate_cid for d in record.delivered if d.ack_status == "drained"]
        pending_cids = [d.candidate_cid for d in record.delivered if d.ack_status != "drained"]
        fate_counts = {fate: len(cids) for fate, cids in fate_buckets.items()}

        return {
            "fate_counts": fate_counts,
            "delivered_cids": [decision.candidate_cid for decision in record.delivered],
            "ack_summary": {
                "pending": len(pending_cids),
                "drained": len(drained_cids),
                "newly_drained": newly_drained,
                "drained_cids": drained_cids,
            },
            "reconcile": reconcile_summary,
            "side_effect_timeouts": list(reconcile_summary["missing_accepted"]),
        }

    def _log_progress(self, status: Mapping[str, Any]) -> None:
        self._cycle += 1
        fate_counts = status.get("fate_counts", {})
        ack_summary = status.get("ack_summary", {})
        self._logger.info(
            "PROGRESS cycle=%s recalled=%s decision_emitted=%s accept=%s deny=%s block=%s report=%s acked=%s lease_remaining_ms=%s reason=%s",
            self._cycle,
            status.get("recalled", 0),
            status.get("decision_emitted", False),
            fate_counts.get("accept", 0),
            fate_counts.get("deny", 0),
            fate_counts.get("block", 0),
            fate_counts.get("report", 0),
            ack_summary.get("drained", 0),
            status.get("lease_remaining_ms", 0),
            status.get("reason", ""),
        )

    def poll_once(self) -> dict[str, Any]:
        now_ms = self._now_ms()

        if self.state.resume_marker == "stopped":
            status = {
                "decision_emitted": False,
                "reason": "stopped",
                "lease_expired": True,
                "lease_remaining_ms": self._lease_remaining_ms(now_ms),
                "recalled": 0,
                "fate_counts": {"accept": 0, "deny": 0, "block": 0, "report": 0},
                "delivered_cids": [],
                "ack_summary": {
                    "pending": 0,
                    "drained": 0,
                    "newly_drained": [],
                    "drained_cids": [],
                },
                "reconcile": {
                    "served_store_present": self.served_store_path.exists(),
                    "served_confirmed": [],
                    "missing_accepted": [],
                    "nonaccept_leaked": [],
                },
                "side_effect_timeouts": [],
            }
            self._log_progress(status)
            return status

        if self._is_lease_expired(now_ms):
            self.state.resume_marker = "stopped"
            atomic_write_state(self.state_path, self.state)
            status = {
                "decision_emitted": False,
                "reason": "lease_expired",
                "lease_expired": True,
                "lease_remaining_ms": 0,
                "recalled": 0,
                "fate_counts": {"accept": 0, "deny": 0, "block": 0, "report": 0},
                "delivered_cids": [],
                "ack_summary": {
                    "pending": 0,
                    "drained": 0,
                    "newly_drained": [],
                    "drained_cids": [],
                },
                "reconcile": {
                    "served_store_present": self.served_store_path.exists(),
                    "served_confirmed": [],
                    "missing_accepted": [],
                    "nonaccept_leaked": [],
                },
                "side_effect_timeouts": [],
            }
            self._log_progress(status)
            return status

        self.refresh_heartbeat()

        recalled = self.coordinator.read_recalled_candidates()
        recalled_cids = self.coordinator._recalled_cids(recalled)

        manifest = self._load_coordinator_manifest()
        if manifest is None:
            reason = self._last_manifest_reason
            status = {
                "decision_emitted": False,
                "reason": reason,
                "lease_expired": False,
                "lease_remaining_ms": self._lease_remaining_ms(self._now_ms()),
                "recalled": len(recalled_cids),
                "fate_counts": {"accept": 0, "deny": 0, "block": 0, "report": 0},
                "delivered_cids": [],
                "ack_summary": {
                    "pending": 0,
                    "drained": 0,
                    "newly_drained": [],
                    "drained_cids": [],
                },
                "reconcile": {
                    "served_store_present": self.served_store_path.exists(),
                    "served_confirmed": [],
                    "missing_accepted": [],
                    "nonaccept_leaked": [],
                },
                "side_effect_timeouts": [],
            }
            self._log_progress(status)
            return status

        if not recalled_cids:
            observation = self._observe_ack_and_outcomes()
            status = {
                "decision_emitted": False,
                "reason": "no_recalled_candidates",
                "lease_expired": False,
                "lease_remaining_ms": self._lease_remaining_ms(self._now_ms()),
                "recalled": 0,
                **observation,
            }
            self._log_progress(status)
            return status

        try:
            validate_schema(manifest)
            validate_correlation(
                manifest,
                run_id=self.run_id,
                session_id=self.session_id,
                recalled_cids=recalled_cids,
            )
        except ValueError as exc:
            self._logger.exception(
                "consumer_bridge correlation rejected scope=%s error=%s",
                self._scope_key,
                exc,
            )
            status = {
                "decision_emitted": False,
                "reason": "correlation_rejected",
                "error": str(exc),
                "lease_expired": False,
                "lease_remaining_ms": self._lease_remaining_ms(self._now_ms()),
                "recalled": len(recalled_cids),
                "fate_counts": {"accept": 0, "deny": 0, "block": 0, "report": 0},
                "delivered_cids": [],
                "ack_summary": {
                    "pending": 0,
                    "drained": 0,
                    "newly_drained": [],
                    "drained_cids": [],
                },
                "reconcile": {
                    "served_store_present": self.served_store_path.exists(),
                    "served_confirmed": [],
                    "missing_accepted": [],
                    "nonaccept_leaked": [],
                },
                "side_effect_timeouts": [],
            }
            self._log_progress(status)
            return status

        digest = compute_manifest_digest(manifest.to_dict())
        existing = self.state.consumed_manifests.get(self._scope_key)
        if existing is not None and existing.manifest_digest == digest:
            observation = self._observe_ack_and_outcomes()
            status = {
                "decision_emitted": False,
                "reason": "already_delivered",
                "manifest_digest": digest,
                "already_delivered_cids": sorted(already_delivered_cids(self.state, self._scope_key)),
                "lease_expired": False,
                "lease_remaining_ms": self._lease_remaining_ms(self._now_ms()),
                "recalled": len(recalled_cids),
                **observation,
            }
            self._log_progress(status)
            return status

        try:
            outcome = self.coordinator.apply_manifest(
                manifest,
                run_id=self.run_id,
                session_id=self.session_id,
            )
        except ConsumerConflictError as exc:
            self._logger.exception(
                "consumer_bridge replay conflict from coordinator scope=%s error=%s",
                self._scope_key,
                exc,
            )
            observation = self._observe_ack_and_outcomes()
            status = {
                "decision_emitted": False,
                "reason": "replay_conflict",
                "error": str(exc),
                "lease_expired": False,
                "lease_remaining_ms": self._lease_remaining_ms(self._now_ms()),
                "recalled": len(recalled_cids),
                **observation,
            }
            self._log_progress(status)
            return status

        delivered_at_ms = self._now_ms()
        delivered = [
            DeliveredDecision(
                candidate_cid=cid,
                fate=fate,
                delivered_at_ms=delivered_at_ms,
                ack_status="pending",
                outcome_ref=None,
            )
            for cid, fate in outcome.decisions
        ]

        try:
            record_delivery(
                self.state,
                self._scope_key,
                digest,
                manifest.coordinator_trace,
                delivered,
                delivered_at_ms,
            )
        except ConsumerConflictError as exc:
            self._logger.exception(
                "consumer_bridge replay conflict during state record scope=%s error=%s",
                self._scope_key,
                exc,
            )
            observation = self._observe_ack_and_outcomes()
            status = {
                "decision_emitted": False,
                "reason": "replay_conflict",
                "error": str(exc),
                "lease_expired": False,
                "lease_remaining_ms": self._lease_remaining_ms(self._now_ms()),
                "recalled": len(recalled_cids),
                **observation,
            }
            self._log_progress(status)
            return status

        self.state.resume_marker = "active"
        atomic_write_state(self.state_path, self.state)

        observation = self._observe_ack_and_outcomes()
        status = {
            "decision_emitted": True,
            "reason": "delivered",
            "manifest_digest": digest,
            "lease_expired": False,
            "lease_remaining_ms": self._lease_remaining_ms(self._now_ms()),
            "recalled": len(recalled_cids),
            "delivered_cids": [cid for cid, _ in outcome.decisions],
            "fate_counts": {
                "accept": outcome.accept_count,
                "deny": outcome.deny_count,
                "block": outcome.block_count,
                "report": outcome.report_count,
            },
            "ack_summary": observation["ack_summary"],
            "reconcile": observation["reconcile"],
            "side_effect_timeouts": observation["side_effect_timeouts"],
        }
        self._log_progress(status)
        return status

    def run_loop(self, *, stop_event: threading.Event, max_cycles: int | None = None) -> None:
        if not isinstance(stop_event, threading.Event):
            raise TypeError("stop_event must be a threading.Event")
        if max_cycles is not None:
            _require_positive_int(max_cycles, field_name="max_cycles")

        cycles = 0
        sleep_ms = min(self.poll_interval_ms, self.heartbeat_cadence_ms)
        self._logger.info(
            "consumer_bridge daemon started scope=%s lease_watchdog=renew-on-heartbeat ttl_ms=%s",
            self._scope_key,
            self.lease_ttl_ms,
        )

        while not stop_event.is_set():
            if max_cycles is not None and cycles >= max_cycles:
                break

            status = self.poll_once()
            cycles += 1

            if status.get("reason") == "lease_expired":
                break
            if max_cycles is not None and cycles >= max_cycles:
                break

            if sleep_ms > 0 and stop_event.wait(sleep_ms / 1000.0):
                break

        self.stop()

    def stop(self) -> None:
        self.state.resume_marker = "stopped"
        atomic_write_state(self.state_path, self.state)


__all__ = [
    "MANIFEST_SUFFIX",
    "DEFAULT_POLL_INTERVAL_MS",
    "DEFAULT_HEARTBEAT_CADENCE_MS",
    "DEFAULT_LEASE_TTL_MS",
    "scope_key",
    "manifest_inbox_name",
    "ConsumerBridge",
]
