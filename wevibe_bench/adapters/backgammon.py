"""Backgammon worker-runner adapter for the benchmark harness.

This adapter drives a single backgammon cell end-to-end:
- seed a fresh worktree from scaffold
- run either a mock worker (golden/scaffold copy) or headless opencode
- evaluate with the backgammon gate report runner
- apply budget-bounded rounds of *problems-only* feedback in the same session
"""

from __future__ import annotations

import collections
from contextlib import nullcontext
from dataclasses import dataclass
import datetime as _dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid

from wevibe_bench.adapters._memory_format import _format_memory
from wevibe_bench.adapters.cheat_detector import (
    build_oracle_markers,
    scan_events_for_oracle_access,
)
from wevibe_bench.config import WORKER_MODEL_REGISTRY
from wevibe_bench.contention import ContentionCovariates
from .docker_worker import (
    DockerCell,
    DockerCellConfig,
    ImageFingerprint,
    WORKER_IMAGE,
    docker_available,
    worker_image_fingerprint,
)
from wevibe_bench.backends.base import NeedCard, RecalledMemory
from wevibe_bench.runner import AgentRunner, TaskOutcome
from wevibe_bench.serve_client import (
    LOOP_GUARD_SIGNATURES,
    REASON_STREAM_FINALIZE_TIMEOUT,
    TERMINAL_GUARD_ABORT,
    ServeClient,
    ServeClientError,
    classify_transport_anomaly,
    founder_attach_command,
    set_read_retry_observer,
)


_LOG = logging.getLogger(__name__)


# WO-77: the first pass is a sequence of chunk prompts (tasks/backgammon/prompts/
# chunk-NN.md). Each chunk instructs the worker to end with this marker; the
# harness advances to the next chunk only when the marker appears in the
# messages produced by THAT chunk (watermark-windowed scan — the worker emits
# the marker and its WEVIBE_DISCOVERY block in either order, sometimes across
# separate assistant messages). A missing marker is a stall, not a verdict:
# WO-NUDGE-INF-1 (Walter 2026-08-11) makes the marker nudge UNBOUNDED — the
# harness re-nudges with the chunking reminder until the marker lands, and the
# attempt is never failed for having needed nudges. Nudge turns are excluded
# from scoring turns; their tokens stay metered. Between chunks the worker
# self-compacts (self_compact tool); the harness watches for the compaction
# evidence and fires a backstop summarize when none materializes.
CHUNK_MARKER = "CHUNK FINISHED"

# Harness-declared verification/test commands for the backgammon task.
# Gate runner = `node report.mjs` (tasks/backgammon/gates/). Worker-invoked
# test commands are observed via bash tool_use events. test_invocations counts
# bash tool_use events whose command contains any declared string.
DECLARED_TEST_COMMANDS: tuple[str, ...] = (
    "node report.mjs",
    "npx vitest",
    "npx playwright",
    "npm test",
    "npm run test",
    "vitest",
    "playwright test",
)

# Source: published provider pricing cards (USD per 1M tokens), including:
# - https://www.orcarouter.ai/api/pricing
#   (pricing_version c58e194db3f6a20e7d41b8c9e2f05a17, fetched 2026-07-24T12:45Z;
#   input USD/Mtok = model_ratio × $2 × group_ratio(=1), output = input × completion_ratio)
# - https://openrouter.ai/anthropic/claude-opus-4.8 (snapshot used in bench guard reports)
# - https://opencode.ai/docs/zen-models (Zen free/free row for big-pickle)
# Walter-pinned: keep the free/free big-pickle row at truthful zero pricing.
_MODEL_PRICING_USD_PER_1M: dict[str, dict[str, float]] = {
    "z-ai/glm-5.2": {
        "input": 1.4,
        "output": 4.4,
        "cache_read": 0.26,
        "cache_write": 1.4,  # OrcaRouter has no cache-write field; use input rate.
    },
    "kimi/kimi-k3": {
        # OrcaRouter pricing_version c58e194db3f6a20e7d41b8c9e2f05a17
        # fetched 2026-07-27 (model_ratio=1.5, completion_ratio=5, cache_ratio=0.1).
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.0,  # OrcaRouter has no cache-write field; use input rate.
    },
    "kimi/kimi-k2.7-code": {
        "input": 0.95,
        "output": 4.0,
        "cache_read": 0.19,
        "cache_write": 0.95,  # OrcaRouter has no cache-write field; use input rate.
    },
    "tencent/hy3": {
        "input": 0.18,
        "output": 0.59,
        "cache_read": 0.059,
        "cache_write": 0.18,  # OrcaRouter has no cache-write field; use input rate.
    },
    "anthropic/claude-opus-4.8": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
    },
    "opencode/big-pickle": {
        "input": 0.0,
        "output": 0.0,
    }
}

_RESERVATION_SAFETY_FACTOR = 1.10
_HARNESS_LIMIT_REASONS = {"run_timeout", "max_steps_per_attempt", "token_cap"}
_PROXY_CHECKPOINT_ENV = "WEVIBE_BENCH_PROXY_CHECKPOINT"
_REASONING_EFFORT_ENV = "WEVIBE_BENCH_REASONING_EFFORT"
# WO-NUDGE-INF-1 (Walter 2026-08-11): nudges are UNBOUNDED. A zero-tool turn is
# a stall, not a verdict — the harness re-nudges for as long as the turn keeps
# coming back tool-less. The only exit from the resume loop is a turn that
# actually used tools (or a missing session id, which is a transport failure,
# not a model outcome). Nudge turns are excluded from scoring turns; their
# tokens stay metered.
ZERO_TOOL_RESUME_NUDGE = "Continue. Edit files with tools — do not explain."
_FILE_WRITE_TOOL_NAMES = frozenset({"write", "edit", "apply_patch", "multi_edit"})

# WO-HOLD-UI-1: opt-in post-cell observation window. When WEVIBE_BENCH_HOLD_UI=1,
# the cell's stack (container + worktree) is NOT torn down at benchmark end; the
# artifact's UI server is booted host-side from the bind-mounted worktree on
# :8002 — the exact bytes the model wrote, the same boot the gates perform
# (tasks/backgammon/gates/lib/harness.ts). The port MUST equal PORT there.
# Release is operator-explicit: `touch <run_dir>/RELEASE_HOLD`. Teardown then
# proceeds through the normal unconditional path (RC-6 is preserved — the hold
# sits INSIDE the cell context, so every abort/interrupt still tears down).
_HOLD_UI_ENV = "WEVIBE_BENCH_HOLD_UI"
_HOLD_UI_PORT = 8002
_HOLD_UI_RELEASE_FILE = "RELEASE_HOLD"
_HOLD_UI_STATE_FILE = "hold-ui.json"
_HOLD_UI_SERVER_LOG = "hold-ui-server.log"
_HOLD_UI_HEALTH_TIMEOUT_S = 15.0
_HOLD_UI_POLL_S = 2.0
_HOLD_UI_HEARTBEAT_S = 30.0

# WO-LOOPREC-1: when the relay's StreamLoopGuard kills a serve-driven turn, the
# harness re-drives the phase with an anti-repetition nudge instead of counting
# the looped turn as completed work (2026-08-10 live cell: a loop kill on the
# repair leg metered as a turn, no recovery, gates ran on an unrepaired
# worktree). WO-FINALIZE-REC-1 (Walter 2026-08-10): the relay's 30s
# stream-finalize watchdog kill gets the same recovery, with a resume-style
# nudge (the turn was cut off, not looping).
# WO-NUDGE-INF-1 (Walter 2026-08-11): the recovery is UNBOUNDED — no budget, no
# exhaustion kill. Stalls, loops, and oversized generations are NORMAL agentic
# behaviour under measurement; the harness keeps nudging with the chunking
# reminder for as long as the kill keeps repeating, and a nudged phase is never
# voided for having been nudged. Nudge turns are excluded from scoring turns
# (see scoring_turns in _run_opencode_serve) so recovery cannot inflate the
# measurement; their tokens stay fully metered — real burn is never hidden.
# Neither nudge restates the original prompt — for a loop kill, the same prompt
# into the same context is the loop's fuel.
# Applies to every serve-driven phase (chunked building leg AND repair leg
# alike — RC-4: no mode branch). The proxy guard itself is never reconfigured.
# The chunking reminder (Walter 2026-08-10): the finalize kills that day were
# oversized single generations (one 32000-token write; ~4900-token writes) —
# the model tried to emit whole files in one call. Both nudges carry the
# write-in-chunks directive so a re-driven turn retries at safe granularity
# (~150 lines ≈ 1.5K output tokens; every observed sub-1K-token generation
# finalized cleanly, the killed ones were ~4.9K+).
_WRITE_CHUNKING_DIRECTIVE = (
    "Remember to write in chunks of at most ~150 lines per tool call — build "
    "large files up in ~150-line chunks across several write/edit calls, never "
    "one giant call."
)
_LOOP_RECOVERY_NUDGE = (
    "Transport notice: your previous response was cut off by a loop detector "
    "(repeated content). Do NOT repeat or restate anything already written. "
    "Continue the task from exactly where it stopped — pick up the next "
    "unfinished step and keep moving. "
    + _WRITE_CHUNKING_DIRECTIVE
    + " No preamble, no recap."
)
_FINALIZE_RECOVERY_NUDGE = (
    "Transport notice: the end of your previous response was lost to a "
    "transport failure after the model finished generating. Continue the task "
    "from exactly where it stopped — do not restart steps that already "
    "completed. "
    + _WRITE_CHUNKING_DIRECTIVE
    + " No preamble, no recap."
)

# Turn-terminal taxonomy (WO-TRUNC-1). A turn is one model generation step,
# delimited by step_start/step_finish on the worker's JSON event stream.
# step_finish reasons that mean "the provider stream ended without a finish
# reason" — the turn's content and usage frame were lost in transit.
TRUNCATED_STEP_FINISH_REASONS = frozenset({"unknown", "stream-incomplete"})
# step_finish reasons that close a turn normally and need no anomaly record.
_NORMAL_STEP_FINISH_REASONS = frozenset({"stop", "tool-calls", "tool_calls", "length"})
# Substring signatures in `error`-event payloads that classify a turn aborted
# by transport/guard rather than by the model. Guard trips and upstream drops
# both surface to the client as terminal error events (see the loopguard
# diagnostic report); the open step they interrupt never gets a step_finish.
# The loop-guard shapes (live + legacy) live in serve_client.LOOP_GUARD_SIGNATURES.
# The relay finalize-watchdog shape leads this list so the stdout event path
# names it exactly as the serve path does (RC-4 taxonomy parity).
_TRANSPORT_ERROR_SIGNATURES = (
    ("stream_finalize_timeout", "did not finalize"),
    ("stream_incomplete", "stream incomplete"),
    ("idle_timeout", "idle timeout"),
    ("provider_error", "provider returned error"),
    ("unexpected_server_error", "unexpected server error"),
    ("corrupted_thought_signature", "corrupted thought signature"),
)
# Anomaly terminal classes recorded on turn_terminal records.
TURN_TERMINAL_TRUNCATED = "truncated_no_signal"
TURN_TERMINAL_GUARD_ABORT = "guard_abort"
TURN_TERMINAL_TRANSPORT_ERROR = "transport_error"
TURN_TERMINAL_STREAM_DIED_OPEN = "stream_died_open"
TURN_TERMINAL_UNCLASSIFIED_FINISH = "unclassified_finish"
# D-SERVE-MESSAGE-500: the transcript read failed past every transient retry,
# so the harness lost its window onto a session that may still be alive. The
# phase carries no trustworthy measurement — an instrument failure, never a
# capability FAIL (RUNBOOK rule 5.10).
TURN_TERMINAL_OBSERVATION_LOST = "observation_lost"
REASON_OBSERVATION_LOST = "transcript_read_failed_past_retries"
# WO-WATCH-1E evidence file name, written next to the cell's events file under
# the run dir (``<worktree>.events.jsonl`` -> ``<worktree>.parent/...``). Lazily
# created: only a real truncation/transport anomaly ever opens it.
TRUNCATION_EVIDENCE_FILENAME = "truncation-evidence.jsonl"


def _iso_utc(epoch_ms: int) -> str:
    """Format an epoch-ms timestamp as an RFC3339 UTC string (evidence window)."""
    return _dt.datetime.fromtimestamp(float(epoch_ms) / 1000.0, tz=_dt.timezone.utc).isoformat()


def _build_truncation_evidence(
    *,
    attempt_id: str | None,
    run_label: str,
    phase: str,
    terminal: str,
    reason: str,
    ts_start_epoch_ms: int | None,
    ts_end_epoch_ms: int,
    wall_seconds: float | None,
    session_id: Any,
    received_bytes: int | None,
    received_lines: int | None,
    last_event_type: Any,
    last_event_ts: Any,
    finish_reason: Any,
    output_tokens_received: int,
    input_tokens_received: int,
    reasoning_tokens_received: int,
    truncations_seen: int,
) -> dict[str, Any]:
    """Build one WO-WATCH-1E truncation/transport evidence record (pure).

    Captures, at the moment a truncation/transport-error is detected, a
    correlation-ready snapshot that a human or future step matches against the
    local proxy's own ``runs/{YYYY-MM-DD}.jsonl`` log by ``ts`` within the
    recorded ``ts_window_utc``. The harness cannot see the proxy's internal
    trace id at capture time, so it records a timestamp window + attempt id +
    session id (READ-ONLY against the proxy — never reads the proxy log, and
    the proxy itself is never touched).
    """
    ts_start = int(ts_start_epoch_ms) if ts_start_epoch_ms is not None else None
    ts_end = int(ts_end_epoch_ms)
    sess = str(session_id) if isinstance(session_id, str) else None
    attempt = str(attempt_id) if attempt_id else None
    return {
        "attempt_id": attempt,
        "run_label": str(run_label),
        "phase": str(phase),
        "terminal": str(terminal),
        "reason": str(reason),
        "ts_start_epoch_ms": ts_start,
        "ts_end_epoch_ms": ts_end,
        "wall_seconds": float(wall_seconds) if wall_seconds is not None else None,
        "session_id": sess,
        "received_bytes": received_bytes,
        "received_lines": received_lines,
        "last_event_type": last_event_type if last_event_type is not None else None,
        "last_event_ts": last_event_ts,
        "finish_reason": finish_reason,
        "output_tokens_received": int(output_tokens_received or 0),
        "input_tokens_received": int(input_tokens_received or 0),
        "reasoning_tokens_received": int(reasoning_tokens_received or 0),
        "truncations_seen": int(truncations_seen or 0),
        "correlation": {
            "proxy_log_dir": "runs",
            "ts_window_utc": [
                _iso_utc(ts_start) if ts_start is not None else None,
                _iso_utc(ts_end),
            ],
            "match_key": f"{run_label}|{attempt or 'none'}|{sess or 'none'}",
        },
    }


# Canonical budget-bounded attempt ceiling.
# Fixture evidence (runs/backgammon/*.scorecard.json):
# - stage7 kimi-k2.7 cells at cap=2.4 spent 1.37-1.69 over 3 attempts
#   (~$0.46-$0.56 per attempt, so ~4 attempts inside a $2.4 cap).
# - stage7 opus-4.8 at cap=11 spent 6.17 over 3 attempts
#   (~$2.06 per attempt, so ~6 attempts inside an ~$12 cap).
# 8 is a bounded safety margin above both observed envelopes.
DEFAULT_ATTEMPT_HARD_CEILING = 8

# Canonical per-attempt step cap (runaway-loop guard, NOT a budget instrument).
# Budget enforcement is the accrued usage.cost kill plus the proxy's hard-cap
# reservation; this cap exists only to stop a fast runaway tool-call loop.
# Evidence for 100: the healthy 15-07 un-clamped baseline used 77 turns across a
# full run (~25-40 per attempt; 19b initial attempt = 37), while the clamp-era
# value of 40 killed smoke 19c at turn 41 mid-work, UNGRADED. 100 = baseline +
# margin. Programmatic `max_steps_per_attempt=None` still means "no cap"; the
# CLI driver defaults to this constant.
DEFAULT_MAX_STEPS_PER_ATTEMPT = 100

# Canonical per-attempt wall-clock timeout (guard, NOT a scoring signal).
# Evidence for 5400: smoke 19d observed ~3060s wall on a healthy 68-turn Opus
# PASS. Stage-4 at the old 1800s default killed converging near-pass runs
# (kimi-k2.7-code: 52 turns with 26/29 gates green; mimo-v2.5-pro: 35 turns).
# int4/fp8 pins run slower than Opus, so the canonical default carries ~1.75x
# headroom over the slowest healthy observed wall (3060 * 1.75 ~= 5355 -> 5400).
DEFAULT_RUN_TIMEOUT_S = 5400

# Canonical gate-oracle wall-clock timeout (harness guard, NOT a scoring signal).
# The gate is graded work, not model work: a healthy grade is fast. Measured
# 2026-08-12 on the backgammon task: 45s and 113s for two clean attempts, and
# 1918s for one starved by an orphaned gate tree competing for CPU. 3600s is
# ~1.9x the worst OBSERVED (already-pathological) wall and ~32x the healthy
# baseline, so it can only fire on a genuine hang, never on a slow-but-working
# grade. A gate that exceeds it fails its attempt WITH evidence (the streamed
# log is already on disk) instead of hanging the campaign indefinitely, which is
# what happened before this existed.
#
# NOT the same threshold as the board's stall ALARM: the alarm is a visual
# signal that must fire early (minutes) so an operator can look; this is a
# destructive kill that must fire late. Alarm << timeout, by construction.
DEFAULT_GATE_TIMEOUT_S = 3600


class GateTimeoutError(RuntimeError):
    """The gate oracle exceeded its wall-clock limit and was killed.

    Distinct from a gate FAIL: the model's work was never graded, so this is a
    harness/instrument failure and must never be scored as a capability FAIL
    (RUNBOOK rule 5.10 reasoning). It carries the partial log path so the stall
    is diagnosable from the artifact rather than from a live process.
    """


def _worktree_has_injection_record(worktree: Path) -> bool:
    return (Path(worktree) / ".wevibe" / "org.json").is_file()


def _scan_cell_delivery(worktree: Path) -> str | None:
    plugin_log = worktree / ".wevibe" / "logs" / "wevibe-plugin-errors.log"
    try:
        payload = plugin_log.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    matches = re.findall(r"\[inject\] injected count=(\d+)", payload)
    if not matches:
        return None
    if any(int(count) >= 1 for count in matches):
        return "YES"
    return "NO"


def _scan_injected_block_chars(worktree: Path) -> int | None:
    plugin_log = worktree / ".wevibe" / "logs" / "wevibe-plugin-errors.log"
    try:
        payload = plugin_log.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    block_matches = re.findall(r"\[inject\] injected[^\n]*\bblock_chars=(\d+)", payload)
    if block_matches:
        return sum(int(chars) for chars in block_matches)

    legacy_matches = re.findall(r"\[inject\] injected[^\n]*\bchars=(\d+)", payload)
    if legacy_matches:
        return sum(int(chars) for chars in legacy_matches)

    return None


@dataclass(frozen=True)
class RecallFunnelScan:
    recall_fired_total: int = 0
    recall_returned_total: int = 0
    recall_returned_count_sum: int = 0
    no_keywords_count: int = 0
    injected_count: int = 0
    served_attempted: int = 0
    served_failed: int = 0
    served_confirmed: int = 0


def _scan_recall_funnel(worktree: Path) -> RecallFunnelScan | None:
    plugin_log = worktree / ".wevibe" / "logs" / "wevibe-plugin-errors.log"
    try:
        payload = plugin_log.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    fired_matches = re.findall(r"\brecall_fired\s+trigger=repeat_failure\b", payload)

    returned_matches = re.findall(
        r"\brecall_returned\s+status=\S+\s+count=(\d+)\s+reason_code=(\S+)\s+dur_ms=\d+\s+error=",
        payload,
    )
    recall_returned_count_sum = sum(int(count) for count, _reason_code in returned_matches)
    no_keywords_count = sum(1 for _count, reason_code in returned_matches if reason_code == "no_keywords")

    injected_matches = re.findall(r"\[inject\]\s+injected\s+count=(\d+)", payload)
    injected_count = sum(int(count) for count in injected_matches)

    served_attempted = len(re.findall(r"\[serve\]\s+upsert\s+cid=", payload))
    served_failed = len(re.findall(r"\[serve\]\s+receipt\s+failed\b", payload))

    return RecallFunnelScan(
        recall_fired_total=len(fired_matches),
        recall_returned_total=len(returned_matches),
        recall_returned_count_sum=recall_returned_count_sum,
        no_keywords_count=no_keywords_count,
        injected_count=injected_count,
        served_attempted=served_attempted,
        served_failed=served_failed,
        served_confirmed=served_attempted - served_failed,
    )


def _export_cell_telemetry(worktree: Path, run_label: str) -> Path | None:
    """Copy the plugin's observable recall surface host-side before teardown.

    The plugin writes its funnel snapshot and error log INSIDE the cell worktree
    under ``.wevibe/``, which is destroyed at ``docker rm``. This copies both
    host-side into ``data/cells/<unix_ts>-<run_label>/`` so the recall telemetry
    survives the container.

    FAIL-OPEN by contract: telemetry export must never fail a cell. Any error is
    logged and swallowed, and the function returns None. ``data/`` is a
    telemetry/retention layer only -- ``runs/`` (RC-5) stays authoritative, and
    this never writes there.
    """
    sources = {
        "funnel-snapshot.json": worktree / ".wevibe" / "state" / "funnel-snapshot.json",
        "plugin-errors.log": worktree / ".wevibe" / "logs" / "wevibe-plugin-errors.log",
    }
    present = {name: path for name, path in sources.items() if path.is_file()}
    if not present:
        return None

    try:
        override = os.environ.get("WEVIBE_BENCH_DATA_DIR", "").strip()
        data_dir = Path(override) if override else Path(__file__).resolve().parents[2] / "data"
        dest = data_dir / "cells" / f"{int(time.time())}-{run_label}"
        dest.mkdir(parents=True, exist_ok=True)
        for name, path in present.items():
            shutil.copy2(path, dest / name)
        return dest
    except (OSError, shutil.Error) as exc:
        _LOG.warning("telemetry export failed for run_label=%s: %s", run_label, exc)
        return None


def _scan_funnel_snapshot(worktree: Path) -> dict[str, dict[str, int | None]] | None:
    """Read the plugin's per-session funnel counters from funnel-snapshot.json.

    The plugin writes this file into its state dir (``{worktree}/.wevibe/state``)
    periodically and on ``session.idle``. Content is a flat JSON object mapping
    sessionId -> counter dict (all numeric; ``gate_decision_ms`` is int|null).

    Mirrors the tolerant style of ``_scan_recall_funnel``: an absent or
    unreadable/corrupt file yields None (never a raise); a file that exists but
    carries no sessions yields ``{}``.
    """
    snapshot_path = worktree / ".wevibe" / "state" / "funnel-snapshot.json"
    try:
        payload = snapshot_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None

    sessions: dict[str, dict[str, int | None]] = {}
    for session_id, counters in parsed.items():
        if not isinstance(counters, dict):
            continue
        sessions[str(session_id)] = dict(counters)
    return sessions


@dataclass(frozen=True)
class _OpencodeRunStats:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    turns: int
    session_id: str | None
    killed_reason: str | None
    exit_code: int | None
    cost_usd: float
    budget_stop_detected: bool = False
    budget_stop_signature: str | None = None
    truncations: int = 0
    zero_tool_turns: int = 0
    terminal_zero_tool_turn: bool = False
    zero_tool_resumes: int = 0
    zero_tool_turn_honest_fail: bool = False
    resume_count: int = 0
    # WO-TRUNC-1: per-anomalous-turn terminal records (truncated/guard/transport
    # endings and their retries). Normal stop/tool-calls/length turns stay
    # aggregate-only. Each dict is a turn_terminal payload ready for the
    # append-only status stream.
    turn_anomalies: tuple[dict[str, Any], ...] = ()
    # WO-77 chunked first pass: one record per chunk (index, delta tokens,
    # marker seen, nudged). Empty for single-prompt phases.
    chunk_reports: tuple[dict[str, Any], ...] = ()
    # Turns whose usage frame never survived the stream drop: their true
    # upstream token burn is unmetered client-side (never synthesized), but
    # their measured wall-clock is real cost and lands here.
    unmetered_turns: int = 0
    unmetered_turn_wall_s: float = 0.0
    # WO-LOOPREC-1/FINALIZE-REC-1: recovery nudges this invocation fired after
    # relay loop-guard kills or finalize-watchdog kills (serve path only; see
    # _LOOP_RECOVERY_NUDGE/_FINALIZE_RECOVERY_NUDGE). WO-NUDGE-INF-1: unbounded.
    recovery_nudges: int = 0
    # WO-TURNACCT-1 (Walter 2026-08-10): guard-killed turns NEVER count toward
    # scoring turns. ``turns`` already excludes them; their count is carried
    # here so the exclusion is reported, never silent. Tokens stay metered.
    guard_aborted_turns: int = 0
    # WO-NUDGE-INF-1 (Walter 2026-08-11): finalize-killed turns are excluded
    # from scoring turns on the same grounds and counted here for the same
    # reason — the exclusion is reported, never silent. Tokens stay metered.
    finalize_timeout_turns: int = 0
    # D-SERVE-MESSAGE-500: phases whose transcript read failed past every
    # transient retry. Non-zero means the cell was measured blind and must be
    # gated VOID-INSTRUMENT rather than scored.
    observation_lost_turns: int = 0


@dataclass(frozen=True)
class _ProxyBudgetSnapshot:
    hard_cap_usd: float
    accrued_actual_usd: float
    accrued_derived_usd: float
    committed_unproven_usd: float
    remaining_usd: float
    checkpoint_path: str


@dataclass
class BackgammonCellResult:
    verdict: str
    attempts_to_green: int | str
    termination_reason: str
    conformed: bool
    input_tokens: int
    output_tokens: int
    turns: int
    wall_seconds: float
    delivery: str
    failed_gates: list[str]
    problems_final: list[dict[str, Any]]
    attempt_reports: list[dict[str, Any]]
    worktree: str
    session_id: str | None
    memory_mode: str
    model: str
    wall_cost_usd: float = 0.0
    cheated: bool = False
    cheat_detail: str = ""
    tool_calls: int | None = None
    test_invocations: int | None = None
    agentic_cycles: int | None = None
    problems_before: int | None = None
    injected_block_chars: int | None = None
    injected_block_est_tokens: int | None = None
    recall_fired_total: int | None = None
    recall_returned_total: int | None = None
    recall_returned_count_sum: int | None = None
    no_keywords_count: int | None = None
    injected_count: int | None = None
    served_attempted: int | None = None
    served_failed: int | None = None
    served_confirmed: int | None = None
    funnel_snapshot: dict[str, dict[str, int | None]] | None = None
    truncations: int = 0
    zero_tool_turns: int = 0
    zero_tool_resumes: int = 0
    zero_tool_turn_honest_fails: int = 0
    transport_resume_count: int = 0
    # WO-TRUNC-1: ledger of anomalously-ended turns across every worker
    # invocation of the cell (truncated_no_signal / guard_abort /
    # transport_error / stream_died_open / unclassified_finish), each with its
    # retry linkage. truncated_turns counts them; truncated_turns_retried counts
    # those a later step or resume picked up.
    turn_anomalies: list[dict[str, Any]] | None = None
    truncated_turns: int = 0
    truncated_turns_retried: int = 0
    # WO-TURNACCT-1 (Walter 2026-08-10): relay guard-killed turns, excluded
    # from ``turns`` (scoring) but never silently dropped — counted here.
    guard_aborted_turns: int = 0
    # WO-NUDGE-INF-1 (Walter 2026-08-11): relay finalize-killed turns, excluded
    # from ``turns`` (scoring) on the same grounds and counted here for the same
    # reason. With unbounded nudging these are the turns recovery re-drove; the
    # count is what proves the nudges did not inflate the measurement.
    finalize_timeout_turns: int = 0
    # D-SERVE-MESSAGE-500: phases that lost transcript observation entirely.
    observation_lost_turns: int = 0
    unmetered_turns: int = 0
    unmetered_turn_wall_s: float = 0.0
    contention: ContentionCovariates | None = None
    worker_image_fingerprint: ImageFingerprint | None = None
    # WO-STRIP-2b: deterministic title the cell gave its OpenCode session(s),
    # surfaced to the run-manifest status stream so the prod dashboard can
    # join exported session-DB rows to bench cells.
    session_title: str | None = None


def _resolve_hold_ui_entrypoint(worktree: Path) -> Path:
    """Artifact-driven entrypoint resolution — the Python port of
    tasks/backgammon/gates/lib/harness.ts resolveEntrypoint: package.json
    scripts.start first, then src/server.{ts,js,mjs,cjs}, else a loud throw
    (a distinct failure class, never a silent skip)."""
    pkg_path = worktree / "package.json"
    if pkg_path.is_file():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pkg = None
        scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
        start_cmd = scripts.get("start") if isinstance(scripts, dict) else None
        if isinstance(start_cmd, str) and start_cmd.strip():
            parts = start_cmd.split()
            for idx, part in enumerate(parts):
                if part in {"node", "tsx", "deno", "bun", "next", "ts-node", "esrun"}:
                    if idx + 1 < len(parts) and re.search(
                        r"\.(ts|js|mjs|cjs|tsx|jsx)$", parts[idx + 1], re.IGNORECASE
                    ):
                        resolved = (worktree / parts[idx + 1]).resolve()
                        if resolved.is_file():
                            return resolved
    for name in ("server.ts", "server.js", "server.mjs", "server.cjs"):
        candidate = worktree / "src" / name
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "hold-ui: no entrypoint resolved — searched package.json scripts.start and "
        f"src/server.{{ts,js,mjs,cjs}} in {worktree}"
    )


def _hold_ui_port_listeners(port: int) -> list[int]:
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    return [int(tok) for tok in (out.stdout or "").split() if tok.strip().isdigit()]


def _hold_ui_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _hold_ui_lan_exposed(port: int) -> str | None:
    """Is the held UI reachable from OFF this machine? Returns the reachable
    address, or None when it is loopback-only.

    The prompt REQUIRES the artifact to bind 127.0.0.1, but the agent wrote
    that server and an agent can ignore an instruction — `listen(8002)` with no
    host binds `::` (verified), publishing the game to every device on the
    operator's network. So this is checked, never assumed: bind the machine's
    own LAN address and see whether the port is already taken there by the
    artifact.

    A failure to determine it returns None (treated as not-exposed) — this is a
    warning surface on an operator-local review feature, and it must never fail
    a finished cell.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No packet is sent; this just selects the default-route interface.
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1, RFC 5737
            lan_ip = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return None
    if not lan_ip or lan_ip.startswith("127."):
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((lan_ip, port)) == 0:
                return f"{lan_ip}:{port}"
    except OSError:
        return None
    return None


def _hold_for_ui_review(
    *,
    run_label: str,
    run_dir: Path,
    worktree: Path,
    container_name: str,
    live_view_url: str,
    progress: Callable[[str], None],
) -> None:
    """Hold the cell stack for operator UI review until released.

    No-op unless WEVIBE_BENCH_HOLD_UI=1. Boots the artifact's server host-side
    from the worktree on :8002 (the gate boot, minus Playwright), then waits on
    the RELEASE_HOLD sentinel. Never fails the cell: boot problems are logged
    and the hold still proceeds (container + worktree stay inspectable). The
    UI server is killed in a finally — the ProcessReaper does not watch 8002.
    """
    if (os.environ.get(_HOLD_UI_ENV) or "").strip() != "1":
        return

    release_path = run_dir / _HOLD_UI_RELEASE_FILE
    state_path = run_dir / _HOLD_UI_STATE_FILE
    server_log_path = run_dir / _HOLD_UI_SERVER_LOG
    url = f"http://localhost:{_HOLD_UI_PORT}"

    proc: subprocess.Popen[str] | None = None
    log_handle: Any = None
    ui_healthy = False
    boot_detail = "not_attempted"

    # A stale listener here is the audit's leaked-gate-server class; the gates
    # themselves SIGKILL it on every boot (harness.ts freePort). Mirrored.
    for pid in _hold_ui_port_listeners(_HOLD_UI_PORT):
        try:
            os.kill(pid, signal.SIGKILL)
            progress(f"PROGRESS run_label={run_label} step=hold-ui killed_stale_listener pid={pid}")
        except OSError as exc:
            progress(f"PROGRESS run_label={run_label} step=hold-ui kill_stale_listener_failed pid={pid} detail={exc}")

    try:
        entrypoint = _resolve_hold_ui_entrypoint(worktree)
    except RuntimeError as exc:
        boot_detail = f"entrypoint_unresolved detail={exc}"
        progress(f"PROGRESS run_label={run_label} step=hold-ui boot=fail {boot_detail}")
    else:
        try:
            log_handle = server_log_path.open("w", encoding="utf-8")
            proc = subprocess.Popen(
                ["node", str(entrypoint)],
                cwd=str(worktree),
                env={**os.environ, "BENCH_DEBUG": "1"},
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            boot_detail = f"spawn_failed detail={exc}"
            progress(f"PROGRESS run_label={run_label} step=hold-ui boot=fail {boot_detail}")
            proc = None
        else:
            deadline = time.monotonic() + _HOLD_UI_HEALTH_TIMEOUT_S
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if _hold_ui_healthy(_HOLD_UI_PORT):
                    ui_healthy = True
                    break
                time.sleep(0.25)
            if ui_healthy:
                boot_detail = f"healthy pid={proc.pid} entrypoint={entrypoint}"
            elif proc.poll() is not None:
                boot_detail = f"server_exited exit={proc.returncode} log={server_log_path}"
            else:
                boot_detail = f"health_timeout log={server_log_path}"
            progress(
                f"PROGRESS run_label={run_label} step=hold-ui "
                f"boot={'ok' if ui_healthy else 'fail'} {boot_detail}"
            )

    # Consume any stale sentinel from a prior hold in this run_dir BEFORE waiting.
    try:
        release_path.unlink(missing_ok=True)
    except OSError:
        pass

    # Did the artifact actually bind loopback-only, as the prompt requires?
    lan_exposure = _hold_ui_lan_exposed(_HOLD_UI_PORT) if ui_healthy else None
    if lan_exposure is not None:
        progress(
            f"PROGRESS run_label={run_label} step=hold-ui bind=LAN_EXPOSED "
            f"address={lan_exposure} detail=artifact_ignored_loopback_requirement"
        )

    state = {
        "url": url,
        "ui_healthy": ui_healthy,
        "boot_detail": boot_detail,
        "ui_pid": proc.pid if (proc is not None and proc.poll() is None) else None,
        "container_name": container_name,
        "worktree": str(worktree),
        "live_view_url": live_view_url,
        "release_cmd": f"touch {release_path}",
        "server_log": str(server_log_path),
        "started_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        # ── CONSUMABLE RELEASE CONTRACT (for the dashboard/control plane) ────
        # Deliberately NOT wired into the dashboard here — a separate agent owns
        # that. This is the stable surface it consumes.
        #
        # Release is a FILE TOUCH, not an HTTP endpoint, on purpose: the holding
        # process is a plain blocking loop with no server of its own, and giving
        # it a listening socket would add a second network surface (and a second
        # thing to secure) to a feature whose whole point is a human looking at
        # one page. A file works from the dashboard, a script, or a shell, needs
        # no auth story, and cannot be reached from off-box at all.
        "status": "held",
        "schema_version": 1,
        "release": {
            "method": "touch_file",
            "path": str(release_path),
            "poll_interval_s": _HOLD_UI_POLL_S,
            # A consumer releases the hold by creating this file. The loop polls
            # for it and tears the stack down on the next tick.
            "example_python": f"open({str(release_path)!r}, 'w').close()",
            "example_shell": f"touch {release_path}",
        },
        "bind": {
            # MEASURED, not asserted: the agent wrote the server, so whether it
            # honoured the loopback-only requirement is a fact to check.
            "expected_host": "127.0.0.1",
            "lan_reachable": lan_exposure is not None,
            "lan_address": lan_exposure,
        },
    }
    try:
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        progress(f"PROGRESS run_label={run_label} step=hold-ui state_write_failed detail={exc}")

    hold_banner = (
        f"HOLD-UI ACTIVE run_label={run_label} url={url} "
        f"ui={'live' if ui_healthy else f'UNAVAILABLE ({boot_detail})'} "
        f"container={container_name} live_view={live_view_url} "
        f"release='touch {release_path}'"
    )
    progress(f"PROGRESS run_label={run_label} step=hold-ui waiting {hold_banner}")

    # The operator-facing close-out. The machine-readable banner above is for
    # the log; this is the line a human reads at the end of a run, so it leads
    # with a clickable URL and states plainly that the session is waiting on
    # them. Printed only when the UI actually booted — offering a link to a
    # server that is not listening is worse than saying nothing.
    if ui_healthy:
        if lan_exposure is None:
            reach_lines = (
                "  The page is served on loopback only — reachable from this\n"
                "  machine, not from anything else on your network.\n"
            )
        else:
            reach_lines = (
                f"  WARNING: this server is ALSO reachable at {lan_exposure}\n"
                "  — every device on your network can open it. The artifact did\n"
                "  not honour the loopback-only requirement in its prompt.\n"
            )
        operator_message = (
            f"\n{'=' * 72}\n"
            f"  game is finished — you can view it here: {url}\n"
            f"{'=' * 72}\n"
            f"  This session is now HELD and will wait until you release it.\n"
            f"{reach_lines}\n"
            f"  When you are done looking, release it with:\n"
            f"      touch {release_path}\n"
            f"{'=' * 72}\n"
        )
    else:
        operator_message = (
            f"\n{'=' * 72}\n"
            f"  game is finished, but the UI did NOT boot: {boot_detail}\n"
            f"{'=' * 72}\n"
            f"  No URL is offered because nothing is listening on {url}.\n"
            f"  Server log: {server_log_path}\n"
            f"  The container and worktree are still up for inspection.\n\n"
            f"  Release the hold with:\n"
            f"      touch {release_path}\n"
            f"{'=' * 72}\n"
        )
    print(operator_message, flush=True)

    held_at = time.monotonic()
    last_heartbeat = 0.0
    try:
        while not release_path.exists():
            now = time.monotonic()
            if now - last_heartbeat >= _HOLD_UI_HEARTBEAT_S:
                last_heartbeat = now
                server_alive = proc is not None and proc.poll() is None
                progress(
                    f"PROGRESS run_label={run_label} step=hold-ui heartbeat "
                    f"held_s={now - held_at:.0f} url={url} healthy={_hold_ui_healthy(_HOLD_UI_PORT)} "
                    f"server_alive={server_alive}"
                )
            time.sleep(_HOLD_UI_POLL_S)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.5)
            except OSError:
                pass
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass
        remaining = _hold_ui_port_listeners(_HOLD_UI_PORT)
        if remaining:
            progress(
                f"PROGRESS run_label={run_label} step=hold-ui "
                f"port_still_occupied port={_HOLD_UI_PORT} pids={remaining} "
                "detail=not-our-server; left running"
            )
        try:
            release_path.unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        progress(
            f"PROGRESS run_label={run_label} step=hold-ui released "
            f"held_s={time.monotonic() - held_at:.0f} action=proceed-to-teardown"
        )
        print(f"HOLD-UI RELEASED run_label={run_label} — teardown proceeding\n", flush=True)


# Worker-facing AGENTS.md, written into every cell worktree at seed time. The
# only .md files that reach the isolated docker worker are this one (opencode
# auto-loads /work/AGENTS.md as project instructions) and CONTRACT.md (seeded
# from the scaffold, WO-FEEDBACK-CONTRACT 2026-08-10); the repo-level AGENTS.md
# never enters the container. This file carries two rules: (1) WO-ANTICHEAT-1 —
# the explicit anti-cheat rule (rule only, no verdict threat: Walter 2026-08-10 —
# a cheat ATTEMPT is ignored, the operator monitors the live session, and
# PASS/FAIL is never gated on this text); (2) the chunked-write rule, which
# exists because the model writes code in very large single generations, and
# one oversized stream can be killed mid-flight by the transport — losing the
# whole write (2026-08-09).
_WORKER_AGENTS_MD = """\
# Worker instructions

## Integrity — do not cheat
- Do NOT cheat. Do NOT try to find, read, or run the answer sheet: no hunting
  for the hidden tests, the grader, or expected outputs — anywhere, including
  outside this worktree.
- If a tool call is denied, that denial IS the boundary. Never route around a
  denial by indirection (shell tricks, links, copies, downloads).
- Build only from the task prompts and the public requirements in this
  worktree. A failing check points at a public requirement — never at a
  hidden value you should go looking for.
- CONTRACT.md in this worktree is the complete published requirements list.
  Every REQ-ID in a FAILING feedback line names the exact clause in it.

## Chunk large writes — always
- Never write a large file in a single tool call. A single-shot massive write
  can be cut off mid-stream by the transport, and the entire write is lost.
- Start each new file with a bounded initial write (~200-400 lines), then grow
  it with successive append or edit calls — each its own small generation.
- The same for big rewrites: several small, targeted edits — never one giant
  replacement.
- If a write call fails or the result looks truncated, re-apply only the
  missing chunk; do not restart the file from zero unless it is corrupt.
"""


def _default_progress(message: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[bg] {stamp} {message}", flush=True)


def build_worker_opencode_config(
    *,
    model: str,
    reasoning_effort: str | None,
    proxy_base_url: str | None,
    gates_dir: str,
    golden_dir: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "small_model": model,
        "permission": {
            "*": "allow",
            "external_directory": {"*": "deny"},
            "bash": {
                "*": "allow",
                f"*{gates_dir}*": "deny",
                f"*{golden_dir}*": "deny",
                "*report.mjs*": "deny",
                "*run.mjs*": "deny",
            },
            "edit": {"*": "allow", "*opencode.json": "deny"},
            "doom_loop": "deny",
            "question": "deny",
            "task": "deny",
        },
    }
    provider_id, _, model_id = model.partition("/")
    if not provider_id or not model_id:
        return config

    model_registry = WORKER_MODEL_REGISTRY.get(model_id)
    if model_registry is None:
        raise ValueError(f"unsupported worker model_id for opencode config: {model_id!r}")

    provider_options: dict[str, Any] = {
        "apiKey": "{env:LOCAL_LLM_PROXY_API_KEY}",
    }
    if proxy_base_url is not None:
        provider_options["baseURL"] = proxy_base_url

    model_block: dict[str, Any] = dict(model_registry)
    # NOTE: Never force tool_choice="required" here. Moonshot/kimi rejects it (hard 400),
    # and harness policy is to allow normal tool autonomy.
    model_block["interleaved"] = {"field": "reasoning_content"}
    if session_id:
        model_block["headers"] = {"X-Session-Id": session_id}
    if reasoning_effort is not None:
        options = model_block.setdefault("options", {})
        options["reasoning"] = {"effort": reasoning_effort}

    provider_config: dict[str, Any] = {
        provider_id: {
            "options": provider_options,
            "models": {
                model_id: model_block,
            },
        }
    }

    if provider_config:
        config["provider"] = provider_config
    return config


def _safe_title_org_component(org_id: str | None) -> str:
    """Fold ``org_id`` to ``[A-Za-z0-9-]`` for embedding in a session title."""
    folded = re.sub(r"[^A-Za-z0-9-]+", "-", str(org_id or "")).strip("-")
    return folded or "org"


def bench_session_title(org_id: str | None, memory_mode: str, cell_ts: int) -> str:
    """Deterministic, identifiable OpenCode session title for a bench cell.

    Format: ``wevibe-bench-<org_id>-<arm on|off>-<cell_ts>``. ``cell_ts`` is
    the epoch second captured ONCE at cell start, so the title is stable
    across every attempt and resume of that cell and lands verbatim in the
    exported session DB (``session.title``) for the prod dashboard.
    """
    return f"wevibe-bench-{_safe_title_org_component(org_id)}-{memory_mode}-{int(cell_ts)}"


class BackgammonRunner(AgentRunner):
    def __init__(
        self,
        *,
        task_dir: Path,
        work_root: Path,
        model: str,
        memory_mode: str = "off",
        org_id: str = "",
        mock: str | None = None,
        max_attempts: int = DEFAULT_ATTEMPT_HARD_CEILING,
        resume_budget: int = 2,
        token_cap: int = 200000,
        run_timeout_s: int = DEFAULT_RUN_TIMEOUT_S,
        gate_timeout_s: int = DEFAULT_GATE_TIMEOUT_S,
        completion_grace_s: int = 30,
        cost_limit_usd: float | None = None,
        cost_target_usd: float | None = None,
        max_output_tokens: int | None = None,
        max_steps_per_attempt: int | None = None,
        output_price_per_1m: float | None = None,
        reasoning_effort: str | None = None,
        proxy_base_url: str | None = None,
        proxy_token: str | None = None,
        session_id: str | None = None,
        agent: str = "build",
        logger: Any = None,
        progress: Callable[[str], None] | None = None,
        gate_roster_path: Path | str | None = None,
    ) -> None:
        # WO-GATE-ROSTER: the campaign's gate roster, written once at cell start
        # by the sequencer. Passed to `report.mjs` so it can report which gates
        # did NOT run. None is legitimate (a run predating the artifact); the
        # gate report then says so rather than inferring an empty suite.
        self.gate_roster_path = (
            Path(gate_roster_path).expanduser().resolve()
            if gate_roster_path is not None
            else None
        )
        self.task_dir = Path(task_dir).expanduser().resolve()
        self.work_root = Path(work_root).expanduser().resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)

        self.model = str(model)
        self.memory_mode = str(memory_mode)
        # Bench identity for session titling (WO-STRIP-2b). Empty is legitimate
        # (mock/unit callers); the title then uses the "org" fallback component.
        self.org_id = str(org_id or "")
        # Set once at cell start in _run_cell_impl (stable across attempts and
        # resumes); read by the serve-session create call and the first-run argv.
        self._cell_ts: int | None = None
        self._session_title: str | None = None
        self.mock = mock
        requested_max_attempts = int(max_attempts)
        if requested_max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = min(requested_max_attempts, DEFAULT_ATTEMPT_HARD_CEILING)
        self.resume_budget = int(resume_budget)
        if self.resume_budget < 0:
            raise ValueError("resume_budget must be >= 0")
        self.token_cap = int(token_cap)
        self.run_timeout_s = int(run_timeout_s)
        self.gate_timeout_s = int(gate_timeout_s)
        if self.gate_timeout_s <= 0:
            raise ValueError("gate_timeout_s must be > 0")
        self.completion_grace_s = int(completion_grace_s)
        self.cost_limit_usd = None if cost_limit_usd is None else float(cost_limit_usd)
        self.cost_target_usd = None if cost_target_usd is None else float(cost_target_usd)
        self.max_output_tokens = None if max_output_tokens is None else int(max_output_tokens)
        self.max_steps_per_attempt = None if max_steps_per_attempt is None else int(max_steps_per_attempt)
        self.output_price_per_1m = None if output_price_per_1m is None else float(output_price_per_1m)
        resolved_reasoning_effort: str | None
        if reasoning_effort is not None:
            resolved_reasoning_effort = str(reasoning_effort)
        else:
            # No default effort (2026-08-09 directive): the worker request
            # shape must match the daily opencode driver, which sends no
            # reasoning field. Opt-in only via arg or WEVIBE_BENCH_REASONING_EFFORT.
            env_reasoning_effort = os.getenv(_REASONING_EFFORT_ENV)
            if env_reasoning_effort is not None and env_reasoning_effort.strip():
                resolved_reasoning_effort = env_reasoning_effort.strip()
            else:
                resolved_reasoning_effort = None
        self.reasoning_effort = resolved_reasoning_effort
        self.proxy_base_url = None if proxy_base_url is None else str(proxy_base_url)
        self.proxy_token = None if proxy_token is None else str(proxy_token)
        # Live-view topology: fixed serve ports for the persistent per-cell opencode
        # serve, defaulted from env consistent with config.RunConfig (mirror of the
        # hub_url/mcp_recall_url env-override seam).
        self.serve_host_port = int(os.environ.get("WEVIBE_BENCH_SERVE_HOST_PORT") or "4096")
        self.serve_container_port = int(os.environ.get("WEVIBE_BENCH_SERVE_CONTAINER_PORT") or "4096")
        self.session_id = None if session_id is None else str(session_id)
        self.agent = str(agent)

        # Serve-drive wiring (WO-WATCH-1E): the persistent per-cell opencode serve
        # client and its cell-scoped session id, created at cell open when a live
        # serve is up. None until/unless a serve session is established.
        self._serve_client: ServeClient | None = None
        self._cell_session_id: str | None = None

        self._effective_output_price_per_1m = 0.0
        self._cache_write_allowance_usd = 0.0
        self._fallback_attempt_estimate_usd = 0.0

        self.logger = logger
        self._progress_cb = progress or _default_progress
        self._repo_root = Path(__file__).resolve().parents[2]

        if self.memory_mode not in {"off", "on"}:
            raise ValueError("memory_mode must be 'off' or 'on'")
        if self.mock not in {None, "golden", "scaffold"}:
            raise ValueError("mock must be one of: None, 'golden', 'scaffold'")
        if self.token_cap < 1:
            raise ValueError("token_cap must be >= 1")
        if self.run_timeout_s < 1:
            raise ValueError("run_timeout_s must be >= 1")
        if self.completion_grace_s < 1:
            raise ValueError("completion_grace_s must be >= 1")
        if self.cost_limit_usd is not None and self.cost_limit_usd <= 0:
            raise ValueError("cost_limit_usd must be > 0")
        if self.cost_target_usd is not None and self.cost_target_usd <= 0:
            raise ValueError("cost_target_usd must be > 0")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be > 0")
        if self.max_steps_per_attempt is not None and self.max_steps_per_attempt <= 0:
            raise ValueError("max_steps_per_attempt must be > 0")
        if self.output_price_per_1m is not None and self.output_price_per_1m <= 0:
            raise ValueError("output_price_per_1m must be > 0")
        if self.cost_limit_usd is not None and self.cost_target_usd is not None:
            if self.cost_target_usd >= self.cost_limit_usd:
                raise ValueError("cost_target_usd must be < cost_limit_usd")

        # Single-meter budget design: proxy ledger is authoritative. The adapter keeps
        # only a conservative fallback *estimate* for attempt-cost forecasting.
        if (
            self.cost_limit_usd is not None
            and self.max_output_tokens is not None
            and self.max_steps_per_attempt is not None
        ):
            self._effective_output_price_per_1m = self._resolve_output_price_per_1m(
                model=self.model,
                explicit_output_price_per_1m=self.output_price_per_1m,
            )
            cache_write_price_per_1m = self._resolve_cache_write_price_per_1m(
                model=self.model,
                fallback_price_per_1m=self._effective_output_price_per_1m,
            )
            self._cache_write_allowance_usd = (
                float(self.max_output_tokens) * cache_write_price_per_1m / 1_000_000.0
            )
            self._fallback_attempt_estimate_usd = self._worst_case_reservation_usd(
                max_steps=self.max_steps_per_attempt,
                max_output_tokens=self.max_output_tokens,
                output_price_per_1m=self._effective_output_price_per_1m,
                safety_factor=_RESERVATION_SAFETY_FACTOR,
                cache_write_allowance_usd=self._cache_write_allowance_usd,
            )

        allowed_reasoning_efforts = {"minimal", "low", "medium", "high", "xhigh", "none"}
        if self.reasoning_effort is not None and self.reasoning_effort not in allowed_reasoning_efforts:
            allowed = ", ".join(sorted(allowed_reasoning_efforts))
            raise ValueError(f"reasoning_effort must be one of: {allowed}")

    def build_need_card(self, task_id: str) -> NeedCard:
        intent = "debug" if "debug" in task_id.lower() else "build"
        return NeedCard(
            intent=intent,
            task="build a complete playable backgammon game with Node + TypeScript and backend APIs",
            language="typescript",
            stack=["backgammon", "node", "typescript"],
        )

    def run_cell(self, run_label: str, run_dir: Path, task_id: str = "backgammon") -> BackgammonCellResult:
        return self._run_cell_impl(
            run_label=run_label,
            run_dir=run_dir,
            task_id=task_id,
            injected_memory=[],
        )

    def run_task(self, model: str, task_id: str, injected_memory: list[RecalledMemory]) -> TaskOutcome:
        selected_model = str(model or self.model)
        original_model = self.model
        self.model = selected_model
        try:
            with tempfile.TemporaryDirectory(prefix="bg-run-task-", dir=str(self.work_root)) as temp_dir:
                result = self._run_cell_impl(
                    run_label=f"run-task-{task_id}",
                    run_dir=Path(temp_dir),
                    task_id=task_id,
                    injected_memory=injected_memory,
                )
        finally:
            self.model = original_model

        return TaskOutcome(
            resolved=(result.verdict == "PASS"),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            turns=result.turns,
            wall_cost_usd=result.wall_cost_usd,
            wall_seconds=result.wall_seconds,
        )

    @staticmethod
    def _mark_harness_resume(prev: _OpencodeRunStats | None) -> None:
        """Mark the previous invocation's last burned turn as harness-resumed.

        A follow-up ``opencode run --session <id>`` invocation (pass-injection
        or feedback) on the same session IS the retry of a turn the transport
        burned at the tail of the previous invocation. Mutates the shared
        anomaly dict so the cell ledger sees the linkage.
        """
        if prev is None or not prev.turn_anomalies:
            return
        last = prev.turn_anomalies[-1]
        if not last.get("retried"):
            last["retried"] = True
            last["retry_kind"] = "harness_resume"

    def _run_cell_impl(
        self,
        *,
        run_label: str,
        run_dir: Path,
        task_id: str,
        injected_memory: list[RecalledMemory],
    ) -> BackgammonCellResult:
        started = time.monotonic()
        # WO-STRIP-2b: capture the cell epoch ONCE so every surface of this
        # cell (serve session, first-run argv, result, status stream) carries
        # the identical deterministic title, stable across attempts/resumes.
        self._cell_ts = int(time.time())
        self._session_title = bench_session_title(
            self.org_id, self.memory_mode, self._cell_ts
        )
        cell_cost_usd = 0.0
        run_dir = Path(run_dir).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)

        worktree = run_dir / "worktree"
        if worktree.exists():
            shutil.rmtree(worktree)
        worktree.mkdir(parents=True, exist_ok=True)

        self._copy_tree_contents(self.task_dir / "scaffold", worktree)
        worker_agents_md = _WORKER_AGENTS_MD + (
            "\n## Runtime\n"
            f"- Model: {self.model}\n"
        )
        (worktree / "AGENTS.md").write_text(worker_agents_md, encoding="utf-8")
        self._progress(
            f"PROGRESS run_label={run_label} step=worktree-seed src={self.task_dir / 'scaffold'} dst={worktree}"
        )

        pure = self._prepare_memory_mode(worktree=worktree)
        run_env = os.environ.copy()

        session_id: str | None = None
        input_tokens_total = 0
        output_tokens_total = 0
        turns_total = 0
        truncations_total = 0
        zero_tool_turns_total = 0
        zero_tool_resumes_total = 0
        zero_tool_turn_honest_fails_total = 0
        turn_anomalies_all: list[dict[str, Any]] = []
        unmetered_turns_total = 0
        unmetered_turn_wall_total = 0.0
        prev_run_stats: _OpencodeRunStats | None = None
        events_path = Path(f"{worktree}.events.jsonl")
        user_events_path = Path(f"{worktree}.user-events.jsonl")

        attempt_reports: list[dict[str, Any]] = []
        final_report: dict[str, Any] = {}
        verdict = "FAIL"
        attempts_to_green: int | str = "FAIL"
        termination_reason = "pending"
        _worker_exit_annot: str | None = None
        first_run: _OpencodeRunStats | None = None

        worker_killed_reason: str | None = None
        observed_attempt_costs: list[float] = []
        attempt_costs_usd: dict[int, float] = {}
        active_cell: DockerCell | None = None
        cell_context: Any = nullcontext()
        worker_image_identity: ImageFingerprint | None = None

        if self.mock in {"golden", "scaffold"}:
            mock_src = self.task_dir / str(self.mock)
            self._copy_tree_contents(mock_src, worktree)
            self._progress(
                f"PROGRESS run_label={run_label} step=worker-launch mode=mock mock={self.mock}"
            )
        else:
            docker_ok, docker_detail = docker_available()
            if not docker_ok:
                raise RuntimeError(
                    "Docker required for isolated worker; "
                    f"docker preflight failed: {docker_detail}"
                )
            worker_image_identity = worker_image_fingerprint()
            if worker_image_identity is None:
                raise RuntimeError(
                    "Docker worker image missing. "
                    "Build it with: docker build -t wevibe-bench-worker:v1 docker/worker"
                )

            sanitized_label = re.sub(r"[^a-zA-Z0-9_.-]", "-", run_label)
            container_name = f"wevibe-bench-cell-{sanitized_label}"
            stale_rm = subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                check=False,
            )
            stale_detail = (stale_rm.stderr or stale_rm.stdout or "").strip()
            if stale_rm.returncode == 0:
                self._progress(
                    "PROGRESS run_label="
                    f"{run_label} step=docker-stale-remove name={container_name} detail={stale_detail or 'removed'}"
                )
            elif "no such container" in stale_detail.lower():
                self._progress(
                    f"PROGRESS run_label={run_label} step=docker-stale-remove name={container_name} detail=already-absent"
                )
            else:
                raise RuntimeError(
                    f"failed to remove stale docker container name={container_name}: "
                    f"{stale_detail or f'exit={stale_rm.returncode}'}"
                )

            self._progress(
                f"PROGRESS run_label={run_label} step=worker-isolation isolation=docker "
                f"image={WORKER_IMAGE} image_id={worker_image_identity.image_id} "
                f"image_created={worker_image_identity.created} memory_mode={self.memory_mode} "
                f"container={container_name}"
            )
            self._init_worktree_git(worktree=worktree)
            cell_config = self._build_cell_config(
                worktree=worktree,
                container_name=container_name,
            )
            cell_context = DockerCell(
                cell_config,
                progress=self._progress,
            )

        with cell_context as managed_cell:
            if self.mock is None:
                if not isinstance(managed_cell, DockerCell):
                    raise RuntimeError("docker worker context did not yield a DockerCell")
                active_cell = managed_cell
                self._write_worker_permission_config(worktree=worktree)

                # Live-view topology: start the persistent opencode serve for this
                # cell immediately after the container is entered, before the first
                # scored `opencode run`. Unconditional for both memory arms.
                active_cell.start_serve()
                self._progress(
                    "PROGRESS step=live-view "
                    f"serve=http://127.0.0.1:{self.serve_host_port} "
                    f"attach_cmd='opencode attach http://127.0.0.1:{self.serve_host_port}'"
                )

                # WO-WATCH-1E: establish the serve-drive session and surface it so
                # the founder can attach without hunting. A failure here only
                # disables serve-drive for this cell (the stdout fallback remains
                # authoritative); it is never a scored-cell abort.
                serve_base = f"http://127.0.0.1:{self.serve_host_port}"
                self._serve_client = ServeClient(serve_base)
                cell_session_id: str | None = None
                try:
                    cell_session_id = self._serve_client.create_session(
                        title=self._session_title
                    )
                except ServeClientError as exc:
                    self._progress(
                        f"PROGRESS step=live-view session_create_failed detail={exc}"
                    )
                self._cell_session_id = cell_session_id
                self._progress(
                    "PROGRESS step=live-view "
                    f"session_id={cell_session_id or 'none'} serve={serve_base} "
                    f"attach_cmd='{founder_attach_command(self.serve_host_port, cell_session_id)}'"
                )
                if cell_session_id is not None:
                    try:
                        marker = worktree.parent / "live-view.txt"
                        marker.write_text(
                            f"session_id={cell_session_id}\n"
                            f"attach_cmd={founder_attach_command(self.serve_host_port, cell_session_id)}\n"
                            f"serve=http://127.0.0.1:{self.serve_host_port}\n",
                            encoding="utf-8",
                        )
                        self._progress(
                            f"PROGRESS step=live-view marker={marker}"
                        )
                    except OSError as exc:
                        self._progress(
                            f"PROGRESS step=live-view marker_write_failed detail={exc}"
                        )

                chunk_prompts = self._load_chunk_prompts(injected_memory=injected_memory)
                task_prompt = self._joined_chunk_prompt(chunk_prompts)
                self._progress(
                    f"PROGRESS run_label={run_label} step=worker-launch-start mode=real model={self.model} "
                    f"pure={pure} prompt_chars={len(task_prompt)} prompt_chunks={len(chunk_prompts)} "
                    "prompt_delivery=stdin"
                )
                initial_inner = [
                    "opencode",
                    "run",
                    "--model",
                    self.model,
                    "--agent",
                    self.agent,
                    "--dir",
                    "/work",
                    "--config",
                    "/work/opencode.json",
                    "--format",
                    "json",
                ]
                # WO-STRIP-2b: title the stdout-fallback session the same way
                # the serve session is titled. Resume invocations deliberately
                # omit --title (--session preserves the existing title).
                initial_inner += ["--title", self._session_title or ""]
                if pure:
                    initial_inner.append("--pure")

                self._emit_cost_target_warning_if_reached(
                    run_label=run_label,
                    phase="initial",
                    cumulative_cost_usd=cell_cost_usd,
                )

                budget_decision = self._budget_decision_for_attempt(
                    run_label=run_label,
                    attempt=1,
                    observed_attempt_costs=observed_attempt_costs,
                )
                if budget_decision == "harness_error":
                    verdict = "FAIL"
                    attempts_to_green = "FAIL"
                    termination_reason = "harness_error"
                elif budget_decision == "budget_stop":
                    verdict = "BUDGET_STOP"
                    attempts_to_green = "BUDGET_STOP"
                    termination_reason = "attempts_exhausted_by_budget"
                else:
                    self._append_user_event(
                        kind="chunk",
                        run_label=run_label,
                        sidecar_path=user_events_path,
                        attempt=1,
                        text=task_prompt,
                    )
                    first_run = None
                    if self._serve_client is not None and self._cell_session_id is not None:
                        try:
                            first_run = self._run_opencode_serve_chunked(
                                active_cell=active_cell,
                                serve_client=self._serve_client,
                                session_id=self._cell_session_id,
                                prompts=chunk_prompts,
                                run_label=run_label,
                                prior_cost_usd=cell_cost_usd,
                                timeout_s=self.run_timeout_s,
                                kill_hook=active_cell.kill_worker_processes,
                            )
                            self._progress(
                                f"PROGRESS run_label={run_label} step=serve-drive "
                                f"phase=initial status=used"
                            )
                        except Exception as exc:  # noqa: BLE001 - fall back to stdout path.
                            self._progress(
                                f"PROGRESS run_label={run_label} step=serve-drive "
                                f"phase=initial status=fallback reason=exception detail={exc}"
                            )
                            first_run = None
                    if first_run is None:
                        self._progress(
                            f"PROGRESS run_label={run_label} step=serve-drive "
                            f"phase=initial status=fallback reason=stdout"
                        )
                        first_run = self._run_opencode_with_zero_tool_resumes(
                            active_cell=active_cell,
                            initial_inner=initial_inner,
                            pure=pure,
                            worktree=worktree,
                            events_path=events_path,
                            env=run_env,
                            run_label=run_label,
                            phase="initial",
                            fallback_session_id=None,
                            prior_cost_usd=cell_cost_usd,
                            kill_hook=active_cell.kill_worker_processes,
                            stdin_text=task_prompt,
                        )
                    attempt_costs_usd[1] = first_run.cost_usd
                    observed_attempt_costs.append(first_run.cost_usd)
                    cell_cost_usd += first_run.cost_usd
                    session_id = first_run.session_id
                    input_tokens_total += first_run.input_tokens
                    output_tokens_total += first_run.output_tokens + first_run.reasoning_tokens
                    turns_total += first_run.turns
                    truncations_total += first_run.truncations
                    zero_tool_turns_total += first_run.zero_tool_turns
                    zero_tool_resumes_total += first_run.zero_tool_resumes
                    if first_run.zero_tool_turn_honest_fail:
                        zero_tool_turn_honest_fails_total += 1
                    turn_anomalies_all.extend(first_run.turn_anomalies)
                    unmetered_turns_total += first_run.unmetered_turns
                    unmetered_turn_wall_total += first_run.unmetered_turn_wall_s
                    prev_run_stats = first_run
                    worker_killed_reason = first_run.killed_reason
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-launch-end mode=real "
                        f"exit={first_run.exit_code} killed={first_run.killed_reason or 'none'} "
                        f"turns={first_run.turns} input={first_run.input_tokens} "
                        f"output={first_run.output_tokens} reasoning={first_run.reasoning_tokens} "
                        f"session_id={session_id or 'none'} cost_usd={first_run.cost_usd:.4f} "
                        f"cell_cost_usd={cell_cost_usd:.4f}"
                    )

                    if first_run.budget_stop_detected:
                        verdict = "BUDGET_STOP"
                        attempts_to_green = "BUDGET_STOP"
                        termination_reason = "budget_stop_mid_attempt"
                    elif first_run.zero_tool_turn_honest_fail:
                        verdict = "FAIL"
                        attempts_to_green = "FAIL"
                        termination_reason = "zero_tool_turn_honest_fail"
                    elif (
                        first_run.exit_code not in (0, None)
                        and first_run.killed_reason not in _HARNESS_LIMIT_REASONS
                    ):
                        # D-EXIT1-TERMINAL: stream-incomplete is transport, not terminal.
                        # Resume from checkpoint; keep _can_feedback=True.
                        if first_run.exit_code == 1 and self._detect_stream_incomplete(events_path):
                            self._progress(
                                f"PROGRESS run_label={run_label} step=transport-stoppage "
                                f"phase=initial exit_code=1 finish_reason=stream-incomplete "
                                f"resume_budget={self.resume_budget} session_id={first_run.session_id or 'none'}"
                            )
                            first_run = _OpencodeRunStats(
                                input_tokens=first_run.input_tokens,
                                output_tokens=first_run.output_tokens,
                                reasoning_tokens=first_run.reasoning_tokens,
                                turns=first_run.turns,
                                session_id=first_run.session_id,
                                killed_reason=None,
                                exit_code=None,
                                cost_usd=first_run.cost_usd,
                                budget_stop_detected=first_run.budget_stop_detected,
                                budget_stop_signature=first_run.budget_stop_signature,
                                truncations=first_run.truncations,
                                zero_tool_turns=first_run.zero_tool_turns,
                                terminal_zero_tool_turn=first_run.terminal_zero_tool_turn,
                                zero_tool_resumes=first_run.zero_tool_resumes,
                                zero_tool_turn_honest_fail=first_run.zero_tool_turn_honest_fail,
                                resume_count=1,
                                turn_anomalies=first_run.turn_anomalies,
                                unmetered_turns=first_run.unmetered_turns,
                                unmetered_turn_wall_s=first_run.unmetered_turn_wall_s,
                            )
                            if self.resume_budget > 0:
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=transport-resume-allowed "
                                    f"phase=initial resume_count=1 budget={self.resume_budget}"
                                )
                            # _can_feedback stays True (_worker_exit_annot is still None)
                        else:
                            verdict = "FAIL"
                            attempts_to_green = "FAIL"
                            termination_reason = "harness_error"
                            _worker_exit_annot = "harness_error"
            else:
                attempt_costs_usd[1] = 0.0

            # D-GATE-COUPLE: gates run regardless of termination_reason.
            # harness_error is an ANNOTATION on the cell, not a skip.
            _can_feedback = _worker_exit_annot is None
            for attempt in range(1, self.max_attempts + 1):
                report_json = run_dir / f"attempt-{attempt}-report.json"
                gate_log = run_dir / f"attempt-{attempt}-gate.log"
                self._progress(
                    f"PROGRESS run_label={run_label} step=gate-attempt-start attempt={attempt} target={worktree}"
                )
                try:
                    report = self._run_gate_report(
                        worktree=worktree,
                        report_path=report_json,
                        log_path=gate_log,
                    )
                except GateTimeoutError as exc:
                    # A STALL IS NOT A VERDICT (WO-FEEDBACK-1).
                    #
                    # This exception was raised and never caught anywhere in the
                    # repo, so a timed-out gate propagated out of run_cell and
                    # ABORTED THE CAMPAIGN. The evidence existed (gate log,
                    # `step=gate-timeout`) but never reached the scored
                    # artifacts, so the canonical impractical-not-impossible
                    # event was the one outcome the record could not express.
                    #
                    # The gate was KILLED: nothing was measured. That is not the
                    # model failing, and it must never be recorded as such —
                    # hence its own termination_reason, and an
                    # `attempts_to_green` that says so in words rather than
                    # borrowing FAIL.
                    verdict = "FAIL"
                    attempts_to_green = "GATE_TIMEOUT"
                    termination_reason = "gate_timeout"
                    self._progress(
                        f"PROGRESS run_label={run_label} step=gate-timeout-recorded "
                        f"attempt={attempt} termination_reason=gate_timeout detail={exc}"
                    )
                    if _worker_exit_annot != "harness_error":
                        attempt_reports.append(
                            {
                                "attempt": attempt,
                                "verdict": "FAIL",
                                "conformed": False,
                                "n_problems": 0,
                                # Empty, NOT populated with the suite: no gate
                                # failed, the runner was killed before it could
                                # say. Inventing failures here would attribute a
                                # harness death to the model.
                                "failed_gates": [],
                                # None, not [] — "not published" rather than
                                # "published and empty" (invariants I-2 / I-4).
                                "gate_results": None,
                                "gate_totals": None,
                                "gate_timeout": True,
                                "attempt_cost_usd": float(attempt_costs_usd.get(attempt, 0.0)),
                                "parity_pending": True,
                            }
                        )
                    break
                final_report = report

                attempt_verdict = str(report.get("verdict", "FAIL"))
                conformed = bool(report.get("conformed", False))
                problems = report.get("problems") if isinstance(report.get("problems"), list) else []
                failed_gates_raw = report.get("failed_gates")
                failed_gates = [str(item) for item in failed_gates_raw] if isinstance(failed_gates_raw, list) else []
                # WO-GATE-ROSTER. Carried through as-published, or None when the
                # gate runner did not emit them (no roster, or an older report).
                # None and [] mean different things here and must stay distinct:
                # [] would assert "the suite ran and held no gates".
                gate_results_raw = report.get("gate_results")
                gate_results = gate_results_raw if isinstance(gate_results_raw, list) else None
                gate_totals_raw = report.get("gate_totals")
                gate_totals = gate_totals_raw if isinstance(gate_totals_raw, dict) else None

                if _worker_exit_annot != "harness_error":
                    attempt_reports.append(
                        {
                            "attempt": attempt,
                            "verdict": attempt_verdict,
                            "conformed": conformed,
                            "n_problems": len(problems),
                            "failed_gates": failed_gates,
                            "gate_results": gate_results,
                            "gate_totals": gate_totals,
                            "attempt_cost_usd": float(attempt_costs_usd.get(attempt, 0.0)),
                            # Scored cell whose metering awaits parity confirmation against the
                            # first scored cell / the proxy log before it is treated as data.
                            "parity_pending": True,
                        }
                    )
                self._progress(
                    f"PROGRESS gate attempt={attempt} verdict={attempt_verdict} "
                    f"conformed={conformed} problems={len(problems)}"
                )

                if attempt_verdict == "PASS":
                    verdict = "PASS"
                    attempts_to_green = attempt - 1
                    termination_reason = "gates_green"
                    break

                if attempt >= self.max_attempts:
                    verdict = "FAIL"
                    attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                    if _worker_exit_annot == "harness_error":
                        termination_reason = "harness_error"
                    elif _worker_exit_annot is not None:
                        termination_reason = "transport_incomplete"
                    else:
                        termination_reason = "attempt_ceiling_reached"
                    break

                if worker_killed_reason in _HARNESS_LIMIT_REASONS:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=attempt-harness-limit attempt={attempt} "
                        f"reason={worker_killed_reason} decision=continue_if_budget"
                    )
                elif worker_killed_reason is not None:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=attempt-harness-limit attempt={attempt} "
                        f"reason={worker_killed_reason} decision=stop"
                    )
                    verdict = "FAIL"
                    attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                    termination_reason = "harness_error"
                    break

                if _can_feedback is False:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-skip attempt={attempt} "
                        f"reason={_worker_exit_annot} no_working_session"
                    )
                    if _worker_exit_annot == "harness_error":
                        termination_reason = "harness_error"
                    elif _worker_exit_annot is not None:
                        termination_reason = "transport_incomplete"
                    else:
                        termination_reason = "gates_failed"
                    break

                if self.mock is not None:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-skip attempt={attempt} reason=mock_mode"
                    )
                    continue

                if active_cell is None:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-stop attempt={attempt} "
                        "reason=active_cell_missing"
                    )
                    verdict = "FAIL"
                    attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                    termination_reason = "harness_error"
                    break

                if not session_id:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=feedback-stop attempt={attempt} "
                        "reason=session_id_missing"
                    )
                    verdict = "FAIL"
                    attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                    termination_reason = "harness_error"
                    break

                next_attempt = attempt + 1
                budget_decision = self._budget_decision_for_attempt(
                    run_label=run_label,
                    attempt=next_attempt,
                    observed_attempt_costs=observed_attempt_costs,
                )
                if budget_decision == "harness_error":
                    verdict = "FAIL"
                    attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                    termination_reason = "harness_error"
                    break
                if budget_decision == "budget_stop":
                    verdict = "BUDGET_STOP"
                    attempts_to_green = "BUDGET_STOP"
                    termination_reason = "attempts_exhausted_by_budget"
                    break

                # D-EXIT1-TERMINAL: check transport resume budget
                if self.resume_budget <= 0:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=transport-resume-exhausted "
                        f"attempt={attempt} resume_budget=0"
                    )
                    verdict = "FAIL"
                    attempts_to_green = "FAIL"
                    termination_reason = "transport_incomplete"
                    break

                boundary_compaction = self._compact_attempt_boundary(
                    serve_client=self._serve_client,
                    session_id=session_id,
                    run_label=run_label,
                    attempt=attempt,
                )
                if attempt_reports:
                    attempt_reports[-1]["boundary_compaction"] = boundary_compaction
                self._progress(
                    f"PROGRESS run_label={run_label} step=attempt-compaction "
                    f"attempt={attempt} outcome={boundary_compaction}"
                )

                feedback_inner = [
                    "opencode",
                    "run",
                    "--session",
                    session_id,
                    "--dir",
                    "/work",
                    "--config",
                    "/work/opencode.json",
                    "--format",
                    "json",
                ]
                if pure:
                    feedback_inner.append("--pure")

                # Never pass tool_choice="required" via worker config/CLI for these
                # runs; provider path rejects it and the harness guard test enforces this.

                newly_passing = (
                    sorted(
                        set(attempt_reports[-2]["failed_gates"])
                        - set(attempt_reports[-1]["failed_gates"])
                    )
                    if len(attempt_reports) >= 2
                    else []
                )
                still_failing = sorted(set(attempt_reports[-1]["failed_gates"]))
                self._progress(
                    f"PROGRESS run_label={run_label} step=feedback-verdict-composed attempt={attempt} "
                    f"newly_passing_count={len(newly_passing)} still_failing_count={len(still_failing)}"
                )

                next_attempt_cost_usd = 0.0
                if newly_passing:
                    pass_verdict = self._build_pass_verdict(newly_passing=newly_passing)
                    if pass_verdict:
                        self._progress(
                            f"PROGRESS run_label={run_label} step=feedback-pass-injection attempt={attempt} "
                            f"newly_passing_count={len(newly_passing)} session_id={session_id}"
                        )
                        self._emit_cost_target_warning_if_reached(
                            run_label=run_label,
                            phase=f"verdict-pass-{attempt}",
                            cumulative_cost_usd=cell_cost_usd,
                        )
                        self._append_user_event(
                            kind="pass_verdict",
                            run_label=run_label,
                            sidecar_path=user_events_path,
                            attempt=next_attempt,
                            text=pass_verdict,
                        )
                        self._write_worker_permission_config(worktree=worktree)
                        self._mark_harness_resume(prev_run_stats)
                        pass_run = self._run_cell_attempt(
                            active_cell=active_cell,
                            initial_inner=feedback_inner,
                            pure=pure,
                            worktree=worktree,
                            events_path=events_path,
                            env=run_env,
                            run_label=run_label,
                            phase=f"verdict-pass-{attempt}",
                            fallback_session_id=session_id,
                            prior_cost_usd=cell_cost_usd,
                            kill_hook=active_cell.kill_worker_processes,
                            stdin_text=pass_verdict,
                        )
                        next_attempt_cost_usd += pass_run.cost_usd
                        cell_cost_usd += pass_run.cost_usd
                        if pass_run.session_id:
                            session_id = pass_run.session_id

                        input_tokens_total += pass_run.input_tokens
                        output_tokens_total += pass_run.output_tokens + pass_run.reasoning_tokens
                        turns_total += pass_run.turns
                        truncations_total += pass_run.truncations
                        zero_tool_turns_total += pass_run.zero_tool_turns
                        zero_tool_resumes_total += pass_run.zero_tool_resumes
                        if pass_run.zero_tool_turn_honest_fail:
                            zero_tool_turn_honest_fails_total += 1
                        turn_anomalies_all.extend(pass_run.turn_anomalies)
                        unmetered_turns_total += pass_run.unmetered_turns
                        unmetered_turn_wall_total += pass_run.unmetered_turn_wall_s
                        prev_run_stats = pass_run
                        worker_killed_reason = pass_run.killed_reason
                        self._progress(
                            f"PROGRESS run_label={run_label} step=feedback-pass-injection-done attempt={attempt} "
                            f"exit={pass_run.exit_code} killed={pass_run.killed_reason or 'none'} "
                            f"turns={pass_run.turns} input={pass_run.input_tokens} "
                            f"output={pass_run.output_tokens} reasoning={pass_run.reasoning_tokens} "
                            f"cost_usd={pass_run.cost_usd:.4f} cell_cost_usd={cell_cost_usd:.4f}"
                        )
                        if pass_run.budget_stop_detected:
                            verdict = "BUDGET_STOP"
                            attempts_to_green = "BUDGET_STOP"
                            termination_reason = "budget_stop_mid_attempt"
                            break
                        if pass_run.zero_tool_turn_honest_fail:
                            verdict = "FAIL"
                            attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                            termination_reason = "zero_tool_turn_honest_fail"
                            if attempt_reports:
                                attempt_reports[-1]["zero_tool_turn_honest_fail"] = True
                            break
                        if (
                            pass_run.exit_code not in (0, None)
                            and pass_run.killed_reason not in _HARNESS_LIMIT_REASONS
                        ):
                            verdict = "FAIL"
                            attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                            termination_reason = "harness_error"
                            break

                feedback_checks = [
                    str(p.get("check", "")).strip()
                    for p in problems
                    if isinstance(p, dict) and str(p.get("check", "")).strip()
                ]
                # THE GRADIENT (WO-FEEDBACK-1): a gate that ALSO failed last
                # attempt gets one line of observed evidence attached, so the
                # message the model receives actually changes when its fix did
                # not work. Keyed on the raw gate id — the same strings
                # `failed_gates` carries — so the match is exact.
                repeat_checks = (
                    set(attempt_reports[-2]["failed_gates"])
                    & set(attempt_reports[-1]["failed_gates"])
                    if len(attempt_reports) >= 2
                    else set()
                )
                feedback = self._build_feedback_prompt(
                    problems=problems,
                    had_pass_verdict=bool(newly_passing),
                    repeat_checks=repeat_checks,
                )
                self._progress(
                    f"PROGRESS run_label={run_label} step=feedback-problems-only-built attempt={attempt} "
                    f"checks={len(feedback_checks)} repeats={len(repeat_checks)}"
                )
                self._progress(
                    f"PROGRESS run_label={run_label} step=feedback-injection attempt={attempt} "
                    f"problem_count={len(problems)} session_id={session_id}"
                )

                self._emit_cost_target_warning_if_reached(
                    run_label=run_label,
                    phase=f"feedback-{attempt}",
                    cumulative_cost_usd=cell_cost_usd,
                )

                self._append_user_event(
                    kind="feedback",
                    run_label=run_label,
                    sidecar_path=user_events_path,
                    attempt=next_attempt,
                    text=feedback,
                )

                self._write_worker_permission_config(worktree=worktree)

                self._mark_harness_resume(prev_run_stats)
                feedback_run = self._run_cell_attempt(
                    active_cell=active_cell,
                    initial_inner=feedback_inner,
                    pure=pure,
                    worktree=worktree,
                    events_path=events_path,
                    env=run_env,
                    run_label=run_label,
                    phase=f"feedback-{attempt}",
                    fallback_session_id=session_id,
                    prior_cost_usd=cell_cost_usd,
                    kill_hook=active_cell.kill_worker_processes,
                    stdin_text=feedback,
                )
                next_attempt_cost_usd += feedback_run.cost_usd
                attempt_costs_usd[next_attempt] = next_attempt_cost_usd
                observed_attempt_costs.append(next_attempt_cost_usd)
                cell_cost_usd += feedback_run.cost_usd
                if feedback_run.session_id:
                    session_id = feedback_run.session_id

                input_tokens_total += feedback_run.input_tokens
                output_tokens_total += feedback_run.output_tokens + feedback_run.reasoning_tokens
                turns_total += feedback_run.turns
                truncations_total += feedback_run.truncations
                zero_tool_turns_total += feedback_run.zero_tool_turns
                zero_tool_resumes_total += feedback_run.zero_tool_resumes
                if feedback_run.zero_tool_turn_honest_fail:
                    zero_tool_turn_honest_fails_total += 1
                turn_anomalies_all.extend(feedback_run.turn_anomalies)
                unmetered_turns_total += feedback_run.unmetered_turns
                unmetered_turn_wall_total += feedback_run.unmetered_turn_wall_s
                prev_run_stats = feedback_run
                worker_killed_reason = feedback_run.killed_reason
                self._progress(
                    f"PROGRESS run_label={run_label} step=feedback-injection-done attempt={attempt} "
                    f"exit={feedback_run.exit_code} killed={feedback_run.killed_reason or 'none'} "
                    f"turns={feedback_run.turns} input={feedback_run.input_tokens} "
                    f"output={feedback_run.output_tokens} reasoning={feedback_run.reasoning_tokens} "
                    f"cost_usd={feedback_run.cost_usd:.4f} cell_cost_usd={cell_cost_usd:.4f}"
                )
                if feedback_run.budget_stop_detected:
                    verdict = "BUDGET_STOP"
                    attempts_to_green = "BUDGET_STOP"
                    termination_reason = "budget_stop_mid_attempt"
                    break
                if feedback_run.zero_tool_turn_honest_fail:
                    verdict = "FAIL"
                    attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                    termination_reason = "zero_tool_turn_honest_fail"
                    if attempt_reports:
                        attempt_reports[-1]["zero_tool_turn_honest_fail"] = True
                    break
                if (
                    feedback_run.exit_code not in (0, None)
                    and feedback_run.killed_reason not in _HARNESS_LIMIT_REASONS
                ):
                    verdict = "FAIL"
                    attempts_to_green = "DID_NOT_CONFORM" if not conformed else "FAIL"
                    termination_reason = "harness_error"
                    break

            # WO-HOLD-UI-1: benchmark end, stack held for operator UI review.
            # Every loop-exit path converges here; this is the last statement
            # inside the cell context, so release resumes into the normal
            # unconditional teardown (RC-6). No-op unless WEVIBE_BENCH_HOLD_UI=1.
            if active_cell is not None:
                _hold_for_ui_review(
                    run_label=run_label,
                    run_dir=run_dir,
                    worktree=worktree,
                    container_name=active_cell.container_name,
                    live_view_url=f"http://127.0.0.1:{self.serve_host_port}",
                    progress=self._progress,
                )

        if termination_reason == "pending":
            verdict = "FAIL"
            attempts_to_green = "FAIL"
            termination_reason = "harness_error"

        wall_seconds = time.monotonic() - started
        problems_final = self._normalize_problems(final_report.get("problems"))
        failed_gates_final = self._normalize_string_list(final_report.get("failed_gates"))
        oracle_markers = build_oracle_markers(
            gates_dir=(self.task_dir / "gates").resolve(),
            golden_dir=(self.task_dir / "golden").resolve(),
        )
        cheat_finding = scan_events_for_oracle_access(events_path=events_path, markers=oracle_markers)
        cheated = cheat_finding.cheated
        cheat_detail = cheat_finding.summary()
        if cheated:
            verdict = "CHEAT"
            termination_reason = "cheat_detected"
            cheat_marker = run_dir / "CHEAT.json"
            cheat_marker.write_text(
                json.dumps(
                    {
                        "run_label": run_label,
                        "verdict": "CHEAT",
                        "summary": cheat_detail,
                        "hits": [
                            {
                                "tool": hit.tool,
                                "marker": hit.marker,
                                "call_id": hit.call_id,
                                "excerpt": hit.excerpt,
                            }
                            for hit in cheat_finding.hits
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if self.logger is not None:
                error = getattr(self.logger, "error", None)
                if callable(error):
                    error(
                        "CHEAT DETECTED run_label=%s verdict=CHEAT summary=%s hits=%s",
                        run_label,
                        cheat_detail,
                        len(cheat_finding.hits),
                    )
            self._progress(
                f"PROGRESS run_label={run_label} step=cheat-detected verdict=CHEAT "
                f"hits={len(cheat_finding.hits)} summary={cheat_detail} marker={cheat_marker}"
            )

        if attempt_reports:
            attempt_reports[-1]["termination_reason"] = termination_reason

        tool_calls_count, test_invocations_count = self._extract_event_counts(events_path)
        agentic_cycles_count = self._extract_agentic_cycles(user_events_path)
        problems_before_count: int | None = None
        if attempt_reports:
            first_n_problems = attempt_reports[0].get("n_problems")
            if isinstance(first_n_problems, int):
                problems_before_count = first_n_problems

        if _worktree_has_injection_record(worktree):
            scanned_delivery = _scan_cell_delivery(worktree)
            delivery = scanned_delivery if scanned_delivery is not None else "not_measured"
            injected_block_chars = _scan_injected_block_chars(worktree)
            injected_block_est_tokens = (
                round(injected_block_chars / 4)
                if injected_block_chars is not None
                else None
            )
            funnel = _scan_recall_funnel(worktree)
            funnel_snapshot = _scan_funnel_snapshot(worktree)
            recall_fired_total = funnel.recall_fired_total if funnel is not None else None
            recall_returned_total = funnel.recall_returned_total if funnel is not None else None
            recall_returned_count_sum = (
                funnel.recall_returned_count_sum if funnel is not None else None
            )
            no_keywords_count = funnel.no_keywords_count if funnel is not None else None
            injected_count = funnel.injected_count if funnel is not None else None
            served_attempted = funnel.served_attempted if funnel is not None else None
            served_failed = funnel.served_failed if funnel is not None else None
            served_confirmed = funnel.served_confirmed if funnel is not None else None
        else:
            delivery = "N/A"
            injected_block_chars = None
            injected_block_est_tokens = None
            funnel_snapshot = None
            recall_fired_total = None
            recall_returned_total = None
            recall_returned_count_sum = None
            no_keywords_count = None
            injected_count = None
            served_attempted = None
            served_failed = None
            served_confirmed = None
        # Export the plugin's recall surface host-side for BOTH arms, before the
        # container is torn down. OFF cells strip the recall substrate, so their
        # telemetry is exactly the baseline the ON arm is compared against --
        # exporting only on injection-record cells would rebuild the very blind
        # spot data/ exists to close. Fail-open: never fails a cell.
        exported_to = _export_cell_telemetry(worktree, run_label)
        if exported_to is not None:
            self._progress(
                f"PROGRESS run_label={run_label} step=telemetry-export dest={exported_to}"
            )
        self._progress(
            f"PROGRESS run_label={run_label} step=delivery-scan delivery={delivery} "
            f"memory_mode={self.memory_mode}"
        )

        return BackgammonCellResult(
            verdict=verdict,
            attempts_to_green=attempts_to_green,
            termination_reason=termination_reason,
            conformed=bool(final_report.get("conformed", False)),
            input_tokens=input_tokens_total,
            output_tokens=output_tokens_total,
            turns=turns_total,
            wall_seconds=wall_seconds,
            delivery=delivery,
            failed_gates=failed_gates_final,
            problems_final=problems_final,
            attempt_reports=attempt_reports,
            worktree=str(worktree),
            session_id=session_id,
            session_title=self._session_title,
            memory_mode=self.memory_mode,
            model=self.model,
            wall_cost_usd=cell_cost_usd,
            cheated=cheated,
            cheat_detail=cheat_detail,
            tool_calls=tool_calls_count,
            test_invocations=test_invocations_count,
            agentic_cycles=agentic_cycles_count,
            problems_before=problems_before_count,
            injected_block_chars=injected_block_chars,
            injected_block_est_tokens=injected_block_est_tokens,
            recall_fired_total=recall_fired_total,
            recall_returned_total=recall_returned_total,
            recall_returned_count_sum=recall_returned_count_sum,
            no_keywords_count=no_keywords_count,
            injected_count=injected_count,
            served_attempted=served_attempted,
            served_failed=served_failed,
            served_confirmed=served_confirmed,
            funnel_snapshot=funnel_snapshot,
            truncations=truncations_total,
            zero_tool_turns=zero_tool_turns_total,
            zero_tool_resumes=zero_tool_resumes_total,
            zero_tool_turn_honest_fails=zero_tool_turn_honest_fails_total,
            transport_resume_count=first_run.resume_count if first_run else 0,
            turn_anomalies=turn_anomalies_all,
            truncated_turns=len(turn_anomalies_all),
            truncated_turns_retried=sum(1 for record in turn_anomalies_all if record.get("retried")),
            guard_aborted_turns=sum(
                1
                for record in turn_anomalies_all
                if record.get("terminal") == TURN_TERMINAL_GUARD_ABORT
            ),
            finalize_timeout_turns=sum(
                1
                for record in turn_anomalies_all
                if record.get("terminal") == TURN_TERMINAL_TRANSPORT_ERROR
                and record.get("reason") == REASON_STREAM_FINALIZE_TIMEOUT
            ),
            observation_lost_turns=sum(
                1
                for record in turn_anomalies_all
                if record.get("terminal") == TURN_TERMINAL_OBSERVATION_LOST
            ),
            unmetered_turns=unmetered_turns_total,
            unmetered_turn_wall_s=unmetered_turn_wall_total,
            worker_image_fingerprint=worker_image_identity,
        )

    def _run_opencode_with_zero_tool_resumes(
        self,
        *,
        active_cell: DockerCell,
        initial_inner: list[str],
        pure: bool,
        worktree: Path,
        events_path: Path,
        env: dict[str, str],
        run_label: str,
        phase: str,
        fallback_session_id: str | None,
        prior_cost_usd: float = 0.0,
        kill_hook: Callable[[], None] | None = None,
        stdin_text: str | None = None,
    ) -> _OpencodeRunStats:
        aggregate = self._run_opencode(
            cmd=active_cell.exec_argv(initial_inner),
            worktree=worktree,
            events_path=events_path,
            env=env,
            run_label=run_label,
            phase=phase,
            fallback_session_id=fallback_session_id,
            prior_cost_usd=prior_cost_usd,
            kill_hook=kill_hook,
            stdin_text=stdin_text,
        )

        resume_count = 0
        current = aggregate
        budget_stop_detected_any = bool(aggregate.budget_stop_detected)
        budget_stop_signature = aggregate.budget_stop_signature

        while current.terminal_zero_tool_turn:
            resume_session_id = current.session_id or aggregate.session_id or fallback_session_id
            if not resume_session_id:
                break
            resume_count += 1
            self._progress(
                f"PROGRESS run_label={run_label} step=zero-tool-turn-resume phase={phase} "
                f"resume={resume_count} budget=unbounded session_id={resume_session_id}"
            )
            resume_inner = [
                "opencode",
                "run",
                "--session",
                str(resume_session_id),
                "--dir",
                "/work",
                "--config",
                "/work/opencode.json",
                "--format",
                "json",
            ]
            if pure:
                resume_inner.append("--pure")

            resumed = self._run_opencode(
                cmd=active_cell.exec_argv(resume_inner),
                worktree=worktree,
                events_path=events_path,
                env=env,
                run_label=run_label,
                phase=f"{phase}-zero-tool-resume-{resume_count}",
                fallback_session_id=str(resume_session_id),
                prior_cost_usd=prior_cost_usd + aggregate.cost_usd,
                kill_hook=kill_hook,
                stdin_text=ZERO_TOOL_RESUME_NUDGE,
            )

            budget_stop_detected_any = budget_stop_detected_any or resumed.budget_stop_detected
            if budget_stop_signature is None and resumed.budget_stop_signature is not None:
                budget_stop_signature = resumed.budget_stop_signature

            aggregate = _OpencodeRunStats(
                input_tokens=aggregate.input_tokens + resumed.input_tokens,
                output_tokens=aggregate.output_tokens + resumed.output_tokens,
                reasoning_tokens=aggregate.reasoning_tokens + resumed.reasoning_tokens,
                turns=aggregate.turns + resumed.turns,
                session_id=resumed.session_id or aggregate.session_id,
                killed_reason=resumed.killed_reason or aggregate.killed_reason,
                exit_code=resumed.exit_code,
                cost_usd=aggregate.cost_usd + resumed.cost_usd,
                budget_stop_detected=budget_stop_detected_any,
                budget_stop_signature=budget_stop_signature,
                truncations=aggregate.truncations + resumed.truncations,
                zero_tool_turns=aggregate.zero_tool_turns + resumed.zero_tool_turns,
                terminal_zero_tool_turn=resumed.terminal_zero_tool_turn,
                zero_tool_resumes=resume_count,
                zero_tool_turn_honest_fail=False,
                resume_count=aggregate.resume_count + resumed.resume_count,
                turn_anomalies=aggregate.turn_anomalies + resumed.turn_anomalies,
                unmetered_turns=aggregate.unmetered_turns + resumed.unmetered_turns,
                unmetered_turn_wall_s=aggregate.unmetered_turn_wall_s + resumed.unmetered_turn_wall_s,
            )
            current = resumed

        # WO-NUDGE-INF-1: the resume loop above is unbounded, so a still-terminal
        # zero-tool turn HERE means the loop broke for the only other reason —
        # no session id to resume into. That is an unnudgeable dead end (the
        # harness cannot address the worker at all), never "the model ran out of
        # nudges".
        honest_fail = bool(aggregate.terminal_zero_tool_turn)
        if honest_fail:
            self._progress(
                f"PROGRESS run_label={run_label} step=zero-tool-turn outcome=honest-fail phase={phase} "
                f"resumes={resume_count} reason=no_resumable_session_id"
            )

        return _OpencodeRunStats(
            input_tokens=aggregate.input_tokens,
            output_tokens=aggregate.output_tokens,
            reasoning_tokens=aggregate.reasoning_tokens,
            turns=aggregate.turns,
            session_id=aggregate.session_id,
            killed_reason=aggregate.killed_reason,
            exit_code=aggregate.exit_code,
            cost_usd=aggregate.cost_usd,
            budget_stop_detected=aggregate.budget_stop_detected,
            budget_stop_signature=aggregate.budget_stop_signature,
            truncations=aggregate.truncations,
            zero_tool_turns=aggregate.zero_tool_turns,
            terminal_zero_tool_turn=aggregate.terminal_zero_tool_turn,
            zero_tool_resumes=resume_count,
            zero_tool_turn_honest_fail=honest_fail,
            resume_count=aggregate.resume_count,
            turn_anomalies=aggregate.turn_anomalies,
            unmetered_turns=aggregate.unmetered_turns,
            unmetered_turn_wall_s=aggregate.unmetered_turn_wall_s,
        )

    def _run_cell_attempt(
        self,
        *,
        active_cell: DockerCell,
        initial_inner: list[str],
        pure: bool,
        worktree: Path,
        events_path: Path,
        env: dict[str, str],
        run_label: str,
        phase: str,
        fallback_session_id: str | None,
        prior_cost_usd: float,
        kill_hook: Callable[[], None] | None,
        stdin_text: str,
    ) -> _OpencodeRunStats:
        """Run ONE cell attempt, delivered over the serve session when available.

        WO-WATCH-1F transport unification: every scoring attempt (initial,
        feedback, pass-verdict) is delivered to the founder-visible ``opencode
        serve`` session via :meth:`_run_opencode_serve` (``prompt_async`` ->
        ``/session/status`` idle -> persisted-transcript metering) so the founder
        TUI and the transcript advance and truncation capture fires on EVERY
        attempt — not just the first. When no serve session exists (hermetic fake
        cells whose ``create_session`` fails closed, or live-view disabled) the
        attempt falls back to the stdout subprocess path
        (:meth:`_run_opencode_with_zero_tool_resumes`), unchanged.

        Zero-tool-resume semantics: :func:`serve_client.extract_transcript_metrics`
        does NOT compute ``zero_tool_turns``/``terminal_zero_tool_turn`` from the
        transcript, so a serve-driven attempt cannot detect a terminal zero-tool
        turn from the transcript alone. The zero-tool resume loop therefore stays
        on the stdout path. A serve-driven attempt IS re-driven in place only by
        the WO-LOOPREC-1 loop-guard recovery inside :meth:`_run_opencode_serve`
        (bounded anti-repetition nudge on a ``relay_loop_detected`` terminal);
        no other resume nudge fires on this path.
        """
        if self._serve_client is not None and self._cell_session_id is not None:
            try:
                return self._run_opencode_serve(
                    active_cell=active_cell,
                    serve_client=self._serve_client,
                    session_id=self._cell_session_id,
                    prompt=stdin_text,
                    run_label=run_label,
                    phase=phase,
                    prior_cost_usd=prior_cost_usd,
                    timeout_s=self.run_timeout_s,
                    kill_hook=kill_hook,
                )
            except Exception as exc:  # noqa: BLE001 - mirror the initial-attempt fallback.
                self._progress(
                    f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                    f"status=fallback reason=exception detail={exc}"
                )
        return self._run_opencode_with_zero_tool_resumes(
            active_cell=active_cell,
            initial_inner=initial_inner,
            pure=pure,
            worktree=worktree,
            events_path=events_path,
            env=env,
            run_label=run_label,
            phase=phase,
            fallback_session_id=fallback_session_id,
            prior_cost_usd=prior_cost_usd,
            kill_hook=kill_hook,
            stdin_text=stdin_text,
        )

    def _write_worker_permission_config(self, *, worktree: Path) -> None:
        gates_dir = str((self.task_dir / "gates").resolve())
        golden_dir = str((self.task_dir / "golden").resolve())
        config = build_worker_opencode_config(
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            proxy_base_url=self.proxy_base_url,
            gates_dir=gates_dir,
            golden_dir=golden_dir,
            session_id=self.session_id,
        )
        session_header_set = bool(self.session_id)
        provider_id, _, model_id = self.model.partition("/")
        if provider_id and model_id:
            self._progress(
                "PROGRESS step=worker-permission-config "
                f"model_declared={model_id} provider={provider_id} "
                f"session_header_set={str(session_header_set).lower()}"
            )

        self._progress(
            "PROGRESS step=worker-permission-config-provider "
            f"provider={provider_id or 'none'} model={model_id or 'none'} "
            f"proxy_base_url_set={str(bool(self.proxy_base_url)).lower()} "
            f"session_header_set={str(session_header_set).lower()}"
        )

        # Output token caps are enforced via Docker env
        # OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX, not model `options.max_tokens`
        # in opencode.json.
        if provider_id and model_id and self.reasoning_effort is not None:
            self._progress(
                "PROGRESS step=worker-permission-config "
                f"reasoning_effort={self.reasoning_effort} model={self.model}"
            )
        (worktree / "opencode.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        self._progress(
            "PROGRESS step=worker-permission-config external_directory=deny "
            "oracle_bash_deny=active task_deny=active skip_permissions_removed=true"
        )

    def _build_cell_config(self, *, worktree: Path, container_name: str) -> DockerCellConfig:
        session_db_dir = worktree.parent / "session-db"
        session_db_dir.mkdir(parents=True, exist_ok=True)
        cell_config = DockerCellConfig(
            worktree=worktree,
            memory_mode=self.memory_mode,
            container_name=container_name,
        )
        cell_config.session_db_host_path = session_db_dir
        cell_config.plugin_state_host_path = str(worktree / ".wevibe" / "state")
        cell_config.output_token_max = self.max_output_tokens
        cell_config.proxy_base_url = self.proxy_base_url
        cell_config.proxy_token = self.proxy_token
        cell_config.worker_logs_dir = worktree.parent / "worker-logs"
        cell_config.serve_host_port = self.serve_host_port
        cell_config.serve_container_port = self.serve_container_port
        return cell_config

    def _init_worktree_git(self, *, worktree: Path) -> None:
        # opencode resolves the session worktree by walking up from --dir /work
        # looking for .git; with no .git at/above the bind-mount root it falls
        # back to "/", so the wevibe plugin reads /.wevibe/org.json (absent)
        # and the session stays DORMANT. git-init the seeded worktree so the
        # plugin resolves worktree=/work and reads /work/.wevibe/org.json.
        subprocess.run(
            ["git", "init"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "bench@wevibe.local"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "wevibe-bench"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "bench cell seed"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=True,
        )
        self._progress(f"PROGRESS step=worktree-git-init path={worktree}")

    def _prepare_memory_mode(self, *, worktree: Path) -> bool:

        if self.memory_mode == "on":
            source_org = self._repo_root / ".wevibe" / "org.json"
            if not source_org.is_file():
                raise FileNotFoundError(f"missing required memory marker: {source_org}")

            marker_dir = worktree / ".wevibe"
            marker_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_org, marker_dir / "org.json")

            # Wire the bench-fixture predicate adapter: the plugin observes the
            # agent's own tool-call output, so the runner is copied into the cell
            # worktree (outside the frozen scaffold hash) and a predicate.json
            # declares the bench-fixture reporter. Missing runner source degrades
            # to a stderr warning while still writing predicate.json so existing
            # cells keep working.
            predicate = {"reporter": "bench-fixture", "command": "node bench-check.mjs"}
            marker_dir.joinpath("predicate.json").write_text(
                json.dumps(predicate), encoding="utf-8"
            )
            runner_source = self.task_dir / "bench" / "bench-check.mjs"
            if runner_source.is_file():
                shutil.copy2(runner_source, worktree / "bench-check.mjs")
            else:
                self._progress(
                    f"PROGRESS step=memory-mode warning=bench-runner-missing "
                    f"path={runner_source}"
                )

            self._progress(
                f"PROGRESS step=memory-mode mode=on marker={marker_dir / 'org.json'} "
                "recall_env_injection=container"
            )
            return False

        shutil.rmtree(worktree / ".wevibe", ignore_errors=True)
        self._progress("PROGRESS step=memory-mode mode=off pure=true")
        return True

    def _load_chunk_prompts(self, *, injected_memory: list[RecalledMemory]) -> list[str]:
        """Load the WO-77 chunked first-pass prompts (tasks/backgammon/prompts/chunk-*.md).

        The chunked pass IS the initial pass — there is no monolith fallback.
        Chunk 1 additionally carries the capture/compliance protocol (appended)
        and, for arms that deliver memory in-prompt, the memory blob
        (prepended) — the same delivery points the monolith used. Missing or
        empty chunk data is a loud cell-prep error, never a skip.
        """
        prompts_dir = self.task_dir / "prompts"
        if not prompts_dir.is_dir():
            raise RuntimeError(f"chunked prompts directory missing: {prompts_dir}")
        chunk_paths = sorted(prompts_dir.glob("chunk-*.md"))
        if not chunk_paths:
            raise RuntimeError(f"no chunk prompts (chunk-*.md) found in {prompts_dir}")
        chunks: list[str] = []
        for path in chunk_paths:
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                raise RuntimeError(f"chunk prompt empty: {path}")
            chunks.append(text)

        s_prompt_path = self._repo_root / "scaffold" / "sxe-candidate" / "S-fork-reasoning.md"
        if not s_prompt_path.is_file():
            raise RuntimeError(
                f"producer capture/compliance protocol missing: {s_prompt_path}"
            )
        s_prompt_text = s_prompt_path.read_text(encoding="utf-8")
        if not s_prompt_text.strip():
            raise RuntimeError(
                f"producer capture/compliance protocol empty: {s_prompt_path}"
            )
        self._progress(
            f"PROGRESS step=producer-s-load path={s_prompt_path} chars={len(s_prompt_text)}"
        )

        first = f"{chunks[0]}\n\n=== CAPTURE & COMPLIANCE PROTOCOL ===\n{s_prompt_text}"
        if self.memory_mode != "on":
            memory_blob = _format_memory(injected_memory)
            if memory_blob:
                first = f"{memory_blob}\n{first}"
        return [first, *chunks[1:]]

    @staticmethod
    def _joined_chunk_prompt(chunks: list[str]) -> str:
        """Single-text rendering of the chunk plan (stdout fallback path only)."""
        return "\n\n---\n\n".join(chunks)

    # ── FEEDBACK VOICE (WO-FEEDBACK-1) ───────────────────────────────────────
    #
    # The benchmark's fiction is that a USER is telling the model what is still
    # broken. Everything the model receives must read that way, because a model
    # that recognises an automated grader loop can optimise toward test names
    # instead of toward the product — which is a different measurement than the
    # one this instrument claims to take.
    #
    # Grader-internal identity (`[G05]`, `[F01]`, `conformance:`, `REQ-*`) is
    # therefore STRIPPED from the delivered text. It is NOT stripped from the
    # artifacts: `failed_gates`, `gate_results` and the roster keep the exact
    # tokens, so the board and every scorecard still address gates precisely.
    # The model hears a human; the record keeps the ids.

    _GATE_TOKEN_RE = re.compile(r"^\s*\[[A-Z]+[0-9]*\]\s*")
    _REQ_PREFIX_RE = re.compile(r"^\s*REQ-[A-Z0-9-]+(?:/\S+)?\s*[—–-]\s*")
    _PHASE_PREFIX_RE = re.compile(r"^\s*(?:conformance|backend|frontend):")
    # Absolute paths and stack frames in an assertion message point at the gate
    # files, which the worker cannot read (`external_directory: deny`). Leaving
    # them in only invites turns wasted trying.
    _PATH_RE = re.compile(r"(?:file://)?/\S+")

    @classmethod
    def _humanize_check(cls, check: str) -> str:
        """Render a gate id as the phrase a person would actually say.

        `[G05] REQ-HIGHER-DIE — use higher die` becomes `use higher die`.
        Falls back to the original string rather than to an empty one: a gate
        the model cannot be told about is worse than one told awkwardly.
        """
        text = str(check or "").strip()
        text = cls._PHASE_PREFIX_RE.sub("", text, count=1)
        text = cls._GATE_TOKEN_RE.sub("", text, count=1)
        text = cls._REQ_PREFIX_RE.sub("", text, count=1)
        text = " ".join(text.split())
        return text or " ".join(str(check or "").split())

    @classmethod
    def _sanitize_observed(cls, observed: str, *, limit: int = 160) -> str:
        """The one line of evidence a person would quote back.

        First line only, host paths removed, bounded. Enough to say WHAT went
        wrong without pasting a stack trace no user would paste.
        """
        first = str(observed or "").split("\n", 1)[0]
        # vitest appends its comparator as a trailing `// Object.is equality`
        # note. It is runner furniture, not evidence.
        first = first.split(" // ", 1)[0]
        first = cls._PATH_RE.sub("", first)
        first = first.replace("AssertionError:", "").strip()
        first = " ".join(first.split())
        if len(first) > limit:
            first = f"{first[:limit].rstrip()}…"
        return first

    @staticmethod
    def _build_pass_verdict(*, newly_passing: list[str]) -> str:
        if not newly_passing:
            return ""

        deduped: list[str] = []
        seen: set[str] = set()
        for item in newly_passing:
            first_line = str(item).split("\n", 1)[0]
            sanitized = BackgammonRunner._humanize_check(first_line)
            if len(sanitized) > 80:
                # Trim at a word boundary and mark it. The previous hard slice
                # cut mid-word and left a dangling space — a tell that no human
                # wrote the line.
                sanitized = f"{sanitized[:80].rsplit(' ', 1)[0]}…"
            if not sanitized or sanitized in seen:
                continue
            seen.add(sanitized)
            deduped.append(sanitized)

        if not deduped:
            return ""
        if len(deduped) == 1:
            return f"That fixed it — {deduped[0]} works now."

        shown = deduped[:3]
        joined = ", ".join(shown)
        remaining = len(deduped) - len(shown)
        if remaining > 0:
            joined = f"{joined} and {remaining} more"
        return f"That fixed it — {joined} all pass now."

    @classmethod
    def _build_feedback_prompt(
        cls,
        *,
        problems: list[dict[str, Any]] | None = None,
        checks: list[str] | None = None,
        had_pass_verdict: bool = False,
        repeat_checks: set[str] | None = None,
    ) -> str:
        """Compose the message the model receives after a failed attempt.

        THE GRADIENT (WO-FEEDBACK-1). Previously this listed gate names and
        nothing else, so a gate failing in attempt 2 and again in attempt 3
        produced BYTE-IDENTICAL text. A failed fix therefore returned zero new
        information and the model could not distinguish "closer" from "no
        change" — across a 3-attempt ceiling the loop had no gradient at all,
        which capped the achievable score for reasons that had nothing to do
        with capability.

        The escalation is deliberate:
          · FIRST failure  — the requirement only. Can the model infer the
            implementation from the requirement? That is the thing being
            measured, and handing over the assertion would answer it for free.
          · REPEAT failure — the requirement PLUS one line of what was actually
            observed. The model already had its chance to infer; continuing to
            withhold measures patience, not capability.

        Evidence is one sanitised line, never a stack trace — a user quoting
        back what they saw, not a CI job pasting a log.
        """
        header = (
            "The rest are still failing — fix the implementation so they pass. Do not explain, just edit the code."
            if had_pass_verdict
            else "These are still failing — fix the implementation so they pass. Do not explain, just edit the code."
        )

        # Accept either the rich problem records or a bare check list, so older
        # callers and tests keep working unchanged.
        records: list[dict[str, Any]]
        if problems is not None:
            records = [p for p in problems if isinstance(p, dict)]
        else:
            records = [{"check": c} for c in (checks or [])]

        repeats = repeat_checks or set()

        lines: list[str] = [header, ""]
        seen: set[str] = set()
        rendered = 0

        for record in records:
            raw_check = str(record.get("check", "")).strip()
            if not raw_check:
                continue
            label = cls._humanize_check(raw_check.split("\n", 1)[0])
            if len(label) > 120:
                label = f"{label[:120].rsplit(' ', 1)[0]}…"
            if not label or label in seen:
                continue
            seen.add(label)

            # The gradient: evidence only on a REPEAT failure, keyed on the raw
            # gate id (stable) rather than the humanised label (lossy).
            evidence = ""
            if raw_check in repeats:
                observed = cls._sanitize_observed(record.get("observed", ""))
                if observed:
                    evidence = f" — I'm still seeing: {observed}"

            lines.append(f"- {label}: FAILING{evidence}")
            rendered += 1

        if rendered == 0:
            lines.append("- something is still broken but the checks came back empty: FAILING")
        return "\n".join(lines)

    def _run_gate_report(self, *, worktree: Path, report_path: Path, log_path: Path) -> dict[str, Any]:
        """Run the gate oracle, STREAMING its output to ``log_path`` as it runs.

        WHY STREAMED AND NOT BUFFERED (WO-GRADE-VIS-1). This previously used
        ``subprocess.run(capture_output=True)`` and wrote the log only AFTER the
        process returned. A slow or hung grade therefore produced ZERO bytes for
        its entire duration: measured 2026-08-12, an attempt-3 gate ran 1918s
        (~32 min) against a 45s/113s baseline while `attempt-3-gate.log` did not
        exist, so "grading" and "wedged" were indistinguishable without
        inspecting process stacks by hand. The gate runner already announces
        every phase on stderr BEFORE spawning it (`report.mjs`:
        ``[report] phase=<name> target=...``); those markers were real and
        simply trapped in a pipe buffer until exit.

        Streaming makes the log an append-only progress record whose MTIME is a
        true liveness signal — which is what the board's stall detection reads.
        Both streams are merged (``stderr=STDOUT``) so phase markers and the
        output they describe stay in causal order in one file, and a single
        reader cannot deadlock on two pipes.

        TIMEOUT (belt-and-suspenders). A gate that never returns must fail its
        attempt with evidence rather than hang the campaign forever. On timeout
        the whole process GROUP is killed: the gate spawns npm -> vitest ->
        workers, and signalling only the direct child leaves those children
        alive (exactly the orphan class that burned 341 CPU-minutes on
        2026-08-12). Partial output is already on disk by construction.
        """
        gate_cmd = [
            "node",
            "report.mjs",
            "--target",
            str(worktree.resolve()),
            "--out",
            str(report_path.resolve()),
        ]
        # Only when the artifact actually exists. Passing a path to a missing
        # file would make the report claim an unreadable roster instead of the
        # true "this run has no roster", and those are different facts.
        if self.gate_roster_path is not None and self.gate_roster_path.is_file():
            gate_cmd += ["--roster", str(self.gate_roster_path)]
        gates_cwd = str((self.task_dir / "gates").resolve())
        log_path.parent.mkdir(parents=True, exist_ok=True)

        gate_started = time.monotonic()
        timed_out = False  # set by the watchdog below, never inferred
        # Header is written and flushed BEFORE the child starts, so the file
        # exists from t=0 and its absence can never be mistaken for a slow gate.
        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"cmd: {gate_cmd}\n")
            log_file.write(f"cwd: {gates_cwd}\n")
            log_file.write(f"timeout_seconds: {self.gate_timeout_s}\n")
            log_file.write("--- output (streamed, stdout+stderr merged) ---\n")
            log_file.flush()

            proc = subprocess.Popen(  # noqa: S603 - fixed argv, host-only gate oracle
                gate_cmd,
                cwd=gates_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered: a phase marker lands as it is emitted
                start_new_session=True,  # own process group, so timeout kills the tree
            )
            assert proc.stdout is not None
            # WATCHDOG, NOT `wait(timeout=...)`. The reader loop below blocks in
            # `for line in proc.stdout` until the child closes the pipe, so a
            # hung gate never reaches a post-loop wait() — the timeout would be
            # structurally unreachable. (That is precisely the defect class this
            # work exists to fix: vitest's own 60s testTimeout could not fire
            # because a microtask loop starved its timer.) An independent timer
            # thread owns the deadline, kills the process group, and the pipe
            # closes as a consequence, which unblocks the reader.
            timeout_fired = threading.Event()

            def _on_deadline() -> None:
                timeout_fired.set()
                self._kill_process_group(proc)

            watchdog = threading.Timer(self.gate_timeout_s, _on_deadline)
            watchdog.daemon = True
            watchdog.start()
            try:
                for line in proc.stdout:
                    log_file.write(line)
                    # Flushed per line: an unflushed buffer would reintroduce
                    # exactly the invisibility this change exists to remove.
                    log_file.flush()
                    self._emit_gate_phase_progress(line, log_path=log_path)
                proc.wait()
            finally:
                watchdog.cancel()
                if proc.poll() is None:
                    # Pipe closed while the child still lives. Never leave the
                    # tree running.
                    self._kill_process_group(proc)
                    proc.wait()
            timed_out = timeout_fired.is_set()

            gate_wall = time.monotonic() - gate_started
            returncode = proc.returncode
            if timed_out:
                log_file.write(
                    f"\n[harness] gate TIMED OUT after {gate_wall:.3f}s "
                    f"(limit {self.gate_timeout_s}s); process group killed\n"
                )
            log_file.write(f"\nexit: {returncode}\n")
            log_file.write(f"wall_seconds: {gate_wall:.3f}\n")
            log_file.flush()

        if timed_out:
            self._progress(
                f"PROGRESS step=gate-timeout wall_s={gate_wall:.1f} "
                f"limit_s={self.gate_timeout_s} log={log_path}"
            )
            raise GateTimeoutError(
                f"gate oracle exceeded {self.gate_timeout_s}s "
                f"(ran {gate_wall:.1f}s); partial output at {log_path}"
            )

        if not report_path.is_file():
            raise RuntimeError(
                f"gate report missing at {report_path} (exit={returncode})"
            )

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"gate report must be an object: {report_path}")
        return payload

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen[Any]) -> None:
        """SIGKILL the child's whole process group; fall back to the child.

        The gate spawns ``npm exec`` -> ``vitest`` -> worker processes. Killing
        only ``proc`` leaves those children reparented to init and still
        burning CPU — the exact orphan class measured at 341 CPU-minutes on
        2026-08-12. ``start_new_session=True`` at spawn is what makes the
        group addressable here.
        """
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            # Group already gone, or not ours to signal. Never mask the
            # timeout itself behind a teardown error.
            try:
                proc.kill()
            except OSError:
                pass

    def _emit_gate_phase_progress(self, line: str, *, log_path: Path) -> None:
        """Republish a gate phase marker as a harness PROGRESS line.

        The gate runner's own markers live in the gate log, which the control
        plane does not read. Mirroring them into the run log puts grading into
        the same ``PROGRESS step=`` vocabulary every downstream consumer already
        parses, so grading progress appears in the live event feed instead of
        reading as dead air between attempts.

        Instrumentation only: this must never alter gate behaviour, and a
        malformed line is ignored rather than raised.
        """
        text = line.strip()

        # WO-GATE-ROSTER live signal. The gate runner announces each phase's
        # gate SET before spawning it, so the wall can mark those gates
        # under-test the moment the phase begins instead of waiting ~30 minutes
        # for the attempt record. PER-PHASE-SET, not per-test: `report.mjs`
        # spawns each runner with `spawnSync`, so a child's per-test output is
        # buffered until the phase has already ended and could never be live.
        #
        # The line carries a COUNT, not ids — identity already lives in the
        # roster the board reads, and the count is what makes roster/runner
        # drift detectable.
        if text.startswith("[report] gateset "):
            fields = dict(
                part.split("=", 1)
                for part in text[len("[report] gateset ") :].split()
                if "=" in part
            )
            phase = fields.get("phase")
            if phase:
                self._progress(
                    f"PROGRESS step=gate-phase-gates phase={phase} "
                    f"count={fields.get('count', 'unknown')} log={log_path}"
                )
            return

        if not text.startswith("[report] phase="):
            return
        fields = dict(
            part.split("=", 1)
            for part in text[len("[report] ") :].split()
            if "=" in part
        )
        phase = fields.get("phase")
        if not phase:
            return
        status = fields.get("status")
        if status is None:
            # Phase ANNOUNCED. Emitted before the phase runs, so a stall inside
            # it is attributable to a named phase rather than to "the gate".
            self._progress(
                f"PROGRESS step=gate-phase-start phase={phase} log={log_path}"
            )
        else:
            self._progress(
                f"PROGRESS step=gate-phase-end phase={phase} status={status} "
                f"problems={fields.get('problems', 'unknown')} log={log_path}"
            )

    def _run_opencode_serve(
        self,
        *,
        active_cell: DockerCell,
        serve_client: ServeClient,
        session_id: str,
        prompt: str,
        run_label: str,
        phase: str,
        prior_cost_usd: float = 0.0,
        timeout_s: float = 5400.0,
        kill_hook: Callable[[], None] | None = None,
    ) -> _OpencodeRunStats:
        """Drive ONE scoring attempt through the persistent opencode serve.

        WO-WATCH-1F serve-drive path: enqueue the prompt via
        ``POST /session/{sid}/prompt_async``, wait for the session to go idle
        (``GET /session/status`` busy->idle), then meter from the persisted
        transcript ``GET /session/{sid}/message`` via :func:`serve_client.metrics`.
        The harness's OWN timeout path MAY call ``POST /session/{sid}/abort``
        on ITS OWN session to stop serve-side generation; the never-abort rule
        applies ONLY to the founder's passive viewer, never to the harness's
        own timeout path. The harness never kills the serve itself (the
        per-attempt kill hook is serve-PID-scoped and survives by design).

        Unlike :meth:`_run_opencode` (subprocess stdout parsing, retained as the
        fallback), this path does NOT re-raise transport failures: a send error
        returns an ``_OpencodeRunStats`` with ``exit_code=1`` so the caller's
        budget/gating logic decides. ``session_id`` here is the serve-side session
        id (persisted on the serve); the container-side ``opencode run`` session
        id is NOT applicable to this path.

        WO-LOOPREC-1: a relay loop-guard kill (``relay_loop_detected`` in the
        persisted assistant ``info.error``) is classified ``guard_abort`` and
        re-driven with the anti-repetition nudge. WO-FINALIZE-REC-1: a relay
        finalize-watchdog kill is classified
        ``transport_error/stream_finalize_timeout`` and re-driven with the
        resume nudge. WO-NUDGE-INF-1: both recoveries are UNBOUNDED — no
        budget, no exhaustion kill. A killed turn never reads as completed
        work because it is subtracted from ``turns`` (scoring), not because
        the phase is failed; its tokens stay metered.
        """
        if kill_hook is None:
            kill_hook = active_cell.kill_worker_processes

        # Surface every transient observation-read retry on the progress
        # stream. A retry nobody can see is indistinguishable from a serve that
        # never faulted, and a rising retry rate is the leading indicator of
        # D-SERVE-MESSAGE-500 degrading underneath a run that still looks green.
        set_read_retry_observer(
            lambda what, attempt, exc: self._progress(
                f"PROGRESS run_label={run_label} step=serve-read-retry "
                f"phase={phase} what={what} attempt={attempt} detail={exc}"
            )
        )

        # WO-WATCH-1E per-attempt evidence correlation id + start timestamp,
        # matching the stdout path's ``state`` fields. The evidence file path is
        # derived from the cell's worktree when available (real DockerCell);
        # otherwise it falls back to the system temp dir so the serve-drive
        # path stays hermetic (the only DockerCell surface it depends on is
        # ``kill_worker_processes``) without polluting the source tree.
        attempt_id = f"{run_label}-{phase}-{uuid.uuid4().hex[:12]}"
        ts_start_epoch_ms = int(time.time() * 1000)
        try:
            cell_worktree = Path(active_cell.config.worktree).expanduser().resolve()
        except (AttributeError, TypeError):
            cell_worktree = None
        evidence_dir = cell_worktree.parent if cell_worktree is not None else Path(tempfile.gettempdir())
        evidence_path = evidence_dir / TRUNCATION_EVIDENCE_FILENAME

        # Phase baseline for per-phase delta metering: serve transcript metrics
        # are session-CUMULATIVE, so a phase's true cost is the delta against
        # this snapshot. Without deltas, every phase after the first
        # double-counts session totals, and a fully-dead phase (stream
        # dropped, assistant message discarded) masquerades as success with
        # stale cumulative numbers (2026-08-09 feedback-phase void).
        try:
            baseline = serve_client.metrics(session_id)
        except ServeClientError:
            baseline = None
            self._progress(
                f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                "status=baseline_metrics_error detail=deltas degrade to cumulative"
            )

        # Classification watermark (2026-08-10 live-cell defect): a guard- or
        # finalize-killed message keeps its info.error in the transcript
        # FOREVER, and extract_transcript_metrics surfaces it in error_texts on
        # every cumulative read. Classifying on the cumulative read re-trips
        # the SAME kill after a successful recovery — the live chunk-2 drive
        # recovered, landed CHUNK FINISHED, and was still classified
        # guard_abort twice more, which under the then-current nudge budget
        # exhausted it and voided the cell (that exhaustion kill is gone —
        # WO-NUDGE-INF-1 — but a stale re-classification would still burn
        # unbounded nudges on a kill that already recovered). The
        # classification surface is therefore WINDOWED to messages at/after
        # this watermark, and the watermark advances past each classified kill
        # before the recovery nudge re-drives. Metering deltas above are
        # unaffected (they diff cumulative reads).
        try:
            class_watermark = len(serve_client.get_messages(session_id))
        except ServeClientError:
            class_watermark = 0

        # WO-LOOPREC-1/FINALIZE-REC-1 transport recovery: a guard-killed or
        # finalize-killed turn is metered (its tokens burned) but must NOT read
        # as completed work. When the terminal classification is recoverable,
        # mark the anomaly retried and re-drive the phase — anti-repetition
        # nudge for a guard kill (never the original prompt: the same prompt
        # into the same context is the loop's fuel), resume nudge for a finalize
        # kill.
        # WO-NUDGE-INF-1 (Walter 2026-08-11): UNBOUNDED. There is no budget and
        # no exhaustion kill — a repeating kill is re-nudged for as long as it
        # repeats, because stalling and looping are normal agentic behaviour
        # under measurement, not a bench-voiding fault. The measurement is
        # protected on the other side instead: every recovered turn is
        # subtracted from scoring turns below, so no number of nudges can
        # inflate turns/phases, while the tokens they burn stay fully metered.
        # Non-termination is therefore possible by design; a wedged relay is
        # caught by the operator/poller watching the log, not by voiding the run.
        recovery_nudges = 0
        prompt_to_send = prompt
        turn_anomaly_list: list[dict[str, Any]] = []
        killed_reason: str | None = None
        exit_code = 0
        # Set when the transcript read fails past serve_client's transient
        # retries: the harness has lost its window onto the session, so the
        # phase carries no trustworthy measurement (D-SERVE-MESSAGE-500).
        observation_lost = False
        m: dict[str, Any] = {}
        while True:
            # 1) Enqueue the prompt asynchronously.
            try:
                serve_client.send_prompt(session_id, prompt_to_send)
            except ServeClientError as exc:
                self._progress(
                    f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                    f"status=send_error detail={exc}"
                )
                return _OpencodeRunStats(
                    input_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=0,
                    turns=0,
                    session_id=session_id,
                    killed_reason=None,
                    exit_code=1,
                    cost_usd=0.0,
                    turn_anomalies=tuple(turn_anomaly_list),
                    recovery_nudges=recovery_nudges,
                )

            # 2) Confirm the serve actually picked the prompt up (busy), THEN wait
            #    for completion (busy->idle). prompt_async is fire-and-forget: a
            #    bare wait_idle races the serve's busy flag and returns a false
            #    idle in milliseconds, metering turns=0 while gates run against a
            #    worktree the model is still writing (2026-08-09 void cell).
            #    A never-busy send is a loud transport failure (exit 1), never a
            #    clean zero-turn "ok" — unless the transcript already shows turns
            #    (a turn that raced past the busy window is metered, not voided).
            busy_grace_s = 60.0
            went_busy = serve_client.wait_busy(session_id, timeout_s=busy_grace_s)
            killed_reason = None
            exit_code = 0
            if not went_busy:
                self._progress(
                    f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                    f"status=never_busy grace_s={busy_grace_s:.0f} session_id={session_id}"
                )
                try:
                    early = serve_client.metrics(session_id)
                except ServeClientError:
                    early = None
                dead = True
                if early:
                    if baseline is not None:
                        dead = (
                            int(early.get("turns") or 0) - int(baseline.get("turns") or 0) <= 0
                            and int(early.get("output_tokens") or 0)
                            - int(baseline.get("output_tokens") or 0)
                            <= 0
                        )
                    else:
                        dead = int(early.get("turns") or 0) <= 0
                if dead:
                    return _OpencodeRunStats(
                        input_tokens=0,
                        output_tokens=0,
                        reasoning_tokens=0,
                        turns=0,
                        session_id=session_id,
                        killed_reason=None,
                        exit_code=1,
                        cost_usd=0.0,
                        turn_anomalies=tuple(turn_anomaly_list),
                        recovery_nudges=recovery_nudges,
                    )
                # The turn completed inside the busy-grace window; fall through to
                # the normal metering path with idle already reached.
                idle = True
            else:
                # The serve-side generation may continue briefly after idle
                # returns; do NOT wait further.
                idle = serve_client.wait_idle(session_id, timeout_s=timeout_s)
            if not idle:
                killed_reason = "run_timeout"
                exit_code = 1
                # WO-WATCH-1F: genuinely stop serve-side generation before teardown.
                # An abort failure is logged but must NOT mask the timeout outcome.
                try:
                    serve_client.abort(session_id)
                except Exception as exc:  # noqa: BLE001 - never mask the timeout outcome.
                    self._progress(
                        f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                        f"status=abort_failed reason={exc}"
                    )
                else:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                        f"status=abort_issued session_id={session_id}"
                    )
                try:
                    kill_hook()
                except Exception as exc:  # noqa: BLE001 - never mask the timeout outcome.
                    self._progress(
                        f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                        f"status=kill_hook_error detail={exc}"
                    )
                self._progress(
                    f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                    f"status=timeout timeout_s={timeout_s:.1f} session_id={session_id}"
                )

            # 3) Pull metrics from the persisted transcript.
            try:
                m = serve_client.metrics(session_id)
                m_window = serve_client.metrics(session_id, since=class_watermark)
            except ServeClientError as exc:
                # OBSERVATION LOST (D-SERVE-MESSAGE-500). serve_client already
                # retried this read through every transient fault, so reaching
                # here means the harness can no longer see the session at all.
                #
                # This is NOT a capability result and must never be scored as
                # one: the classification window is empty, so the recovery
                # classifier below is blind by construction (it reads only
                # error_texts/truncations/error_parts/info_errors) and would
                # report "no anomaly" for a session that may still be running.
                # That blindness is exactly what voided the 2026-08-11 cell.
                #
                # Record it as a first-class terminal so run_artifacts can gate
                # the cell VOID-INSTRUMENT instead of letting gates run against
                # a half-written worktree and report a false capability FAIL.
                self._progress(
                    f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                    f"status=observation_lost detail={exc}"
                )
                observation_lost = True
                m = (
                    dict(baseline)
                    if baseline is not None
                    else {
                        "turns": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "cost_usd": 0.0,
                        "truncations": 0,
                        "error_parts": 0,
                    }
                )
                m_window = {}
                if exit_code == 0:
                    exit_code = 1

            if observation_lost:
                # The classifier cannot see anything (m_window is empty), so
                # record the lost-observation terminal explicitly rather than
                # letting the blind classifier report a clean phase.
                turn_anomaly_list.append(
                    {
                        "phase": str(phase),
                        "turn_index": int(m.get("turns", 0) or 0),
                        "terminal": TURN_TERMINAL_OBSERVATION_LOST,
                        "reason": REASON_OBSERVATION_LOST,
                        "tool_uses": 0,
                        "file_writes": 0,
                        "input_tokens": int(m.get("input_tokens", 0) or 0),
                        "output_tokens": int(m.get("output_tokens", 0) or 0),
                        "reasoning_tokens": int(m.get("reasoning_tokens", 0) or 0),
                        "cost_usd": float(m.get("cost_usd", 0.0) or 0.0),
                        "tokens_unmetered": True,
                        "wall_seconds": None,
                        "retried": False,
                        "retry_kind": None,
                        "session_id": session_id,
                    }
                )
                break

            terminal, reason = classify_transport_anomaly(m_window)
            if terminal is not None:
                if terminal == "truncated":
                    mapped_terminal = TURN_TERMINAL_TRUNCATED
                elif terminal == TERMINAL_GUARD_ABORT:
                    mapped_terminal = TURN_TERMINAL_GUARD_ABORT
                elif terminal == "transport_error":
                    mapped_terminal = TURN_TERMINAL_TRANSPORT_ERROR
                else:
                    mapped_terminal = terminal
                anomaly_record: dict[str, Any] = {
                    "phase": str(phase),
                    "turn_index": int(m.get("turns", 0)),
                    "terminal": str(mapped_terminal),
                    "reason": str(reason or ""),
                    "tool_uses": 0,
                    "file_writes": 0,
                    "input_tokens": int(m.get("input_tokens", 0) or 0),
                    "output_tokens": int(m.get("output_tokens", 0) or 0),
                    "reasoning_tokens": int(m.get("reasoning_tokens", 0) or 0),
                    "cost_usd": float(m.get("cost_usd", 0.0) or 0.0),
                    "tokens_unmetered": False,
                    "wall_seconds": None,
                    "retried": False,
                    "retry_kind": None,
                    "session_id": session_id,
                }
                turn_anomaly_list.append(anomaly_record)
                self._write_truncation_evidence(
                    record=_build_truncation_evidence(
                        attempt_id=attempt_id,
                        run_label=run_label,
                        phase=phase,
                        terminal=mapped_terminal,
                        reason=str(reason or ""),
                        ts_start_epoch_ms=ts_start_epoch_ms,
                        ts_end_epoch_ms=int(time.time() * 1000),
                        wall_seconds=None,
                        session_id=session_id,
                        received_bytes=None,
                        received_lines=None,
                        last_event_type=None,
                        last_event_ts=None,
                        finish_reason=m.get("last_finish"),
                        output_tokens_received=int(m.get("output_tokens", 0) or 0),
                        input_tokens_received=int(m.get("input_tokens", 0) or 0),
                        reasoning_tokens_received=int(m.get("reasoning_tokens", 0) or 0),
                        truncations_seen=int(m.get("truncations", 0) or 0),
                    ),
                    evidence_path=evidence_path,
                )
                recoverable = mapped_terminal == TURN_TERMINAL_GUARD_ABORT or (
                    mapped_terminal == TURN_TERMINAL_TRANSPORT_ERROR
                    and str(reason or "") == REASON_STREAM_FINALIZE_TIMEOUT
                )
                if recoverable and killed_reason is None and exit_code == 0:
                    anomaly_record["retried"] = True
                    anomaly_record["retry_kind"] = "harness_resume"
                    recovery_nudges += 1
                    self._progress(
                        f"PROGRESS run_label={run_label} step=transport-recovery "
                        f"phase={phase} terminal={mapped_terminal} action=nudge "
                        f"nudge={recovery_nudges} "
                        f"budget=unbounded session_id={session_id}"
                    )
                    prompt_to_send = (
                        _LOOP_RECOVERY_NUDGE
                        if mapped_terminal == TURN_TERMINAL_GUARD_ABORT
                        else _FINALIZE_RECOVERY_NUDGE
                    )
                    # Advance the classification window PAST the kill just
                    # classified: the persisted error never leaves the
                    # transcript, so without this the post-nudge read
                    # re-classifies the same kill (see the watermark note
                    # at phase baseline). A failed probe keeps the old
                    # watermark — a stuck window re-trips LOUD, never
                    # silently clean.
                    try:
                        class_watermark = len(serve_client.get_messages(session_id))
                    except ServeClientError:
                        pass
                    continue
            break

        turn_anomalies: tuple[dict[str, Any], ...] = tuple(turn_anomaly_list)

        def _d(key: str) -> int:
            end_v = int(m.get(key, 0) or 0)
            if baseline is None:
                return end_v
            return end_v - int(baseline.get(key, 0) or 0)

        d_input = _d("input_tokens")
        d_output = _d("output_tokens")
        d_reasoning = _d("reasoning_tokens")
        d_turns = _d("turns")
        d_guard_aborted = _d("guard_aborted_turns")
        d_finalize_timeouts = _d("finalize_timeouts")
        # WO-TURNACCT-1 (Walter 2026-08-10): guard-killed turns are EXCLUDED
        # from scoring turns — their tokens stay metered (real burn), the
        # excluded count is carried on guard_aborted_turns (never silent), and
        # the raw session turn_index cursors stay untouched (watermarks key on
        # them). The stdout path never counts a killed turn in the first place
        # (the interrupted step gets no step_finish), so this subtraction is
        # what keeps the two arms identical (RC-4).
        # WO-NUDGE-INF-1 (Walter 2026-08-11): finalize-killed turns are excluded
        # on exactly the same grounds. Nudging is now unbounded, so this is what
        # keeps recovery from inflating the measurement: a phase that was
        # nudged N times reports the same scoring turns as one that was never
        # nudged, while every burned token stays on the token counters.
        scoring_turns = max(0, d_turns - d_guard_aborted - d_finalize_timeouts)
        d_truncations = _d("truncations")
        d_cost = float(m.get("cost_usd", 0.0) or 0.0) - (
            float(baseline.get("cost_usd", 0.0) or 0.0) if baseline is not None else 0.0
        )

        if exit_code == 0 and d_turns <= 0 and d_output <= 0 and d_input <= 0:
            # The phase reached idle but produced NOTHING: the final assistant
            # message was discarded (relay stream-finalize defect, 2026-08-09)
            # or the serve never generated. Loud exit 1 — never a clean zero
            # that lets gates run against a stale worktree.
            exit_code = 1
            turn_anomalies = turn_anomalies + (
                {
                    "phase": str(phase),
                    "turn_index": int(m.get("turns", 0)),
                    "terminal": "silent_phase",
                    "reason": "phase produced zero new turns and zero new tokens",
                    "tool_uses": 0,
                },
            )
            self._progress(
                f"PROGRESS run_label={run_label} step=serve-drive phase={phase} "
                f"status=silent_phase session_id={session_id}"
            )

        self._progress(
            f"PROGRESS run_label={run_label} step=serve-drive-end phase={phase} "
            f"turns={scoring_turns} guard_aborted_turns={d_guard_aborted} "
            f"finalize_timeout_turns={d_finalize_timeouts} "
            f"observation_lost={'1' if observation_lost else '0'} "
            f"recovery_nudges={recovery_nudges} "
            f"session_turns={m.get('turns', 0)} "
            f"input={d_input} output={d_output} reasoning={d_reasoning} "
            f"session_id={session_id} cost_usd={d_cost:.4f} "
            f"status={'ok' if idle else 'timeout'}"
        )

        return _OpencodeRunStats(
            input_tokens=d_input,
            output_tokens=d_output,
            reasoning_tokens=d_reasoning,
            turns=scoring_turns,
            session_id=session_id,
            killed_reason=killed_reason,
            exit_code=exit_code,
            cost_usd=d_cost,
            budget_stop_detected=False,
            budget_stop_signature=None,
            truncations=d_truncations,
            zero_tool_turns=0,
            terminal_zero_tool_turn=False,
            zero_tool_resumes=0,
            zero_tool_turn_honest_fail=False,
            resume_count=0,
            turn_anomalies=turn_anomalies,
            unmetered_turns=0,
            unmetered_turn_wall_s=0.0,
            recovery_nudges=recovery_nudges,
            guard_aborted_turns=d_guard_aborted,
            finalize_timeout_turns=d_finalize_timeouts,
            observation_lost_turns=1 if observation_lost else 0,
        )

    def _run_opencode_serve_chunked(
        self,
        *,
        active_cell: DockerCell,
        serve_client: ServeClient,
        session_id: str,
        prompts: list[str],
        run_label: str,
        prior_cost_usd: float = 0.0,
        timeout_s: float = 5400.0,
        kill_hook: Callable[[], None] | None = None,
    ) -> _OpencodeRunStats:
        """WO-77 chunked first pass: drive the chunk prompts IN ORDER through the
        one serve session. Per chunk: drive -> watermark-windowed marker scan
        (only messages THIS chunk produced) -> unbounded marker nudges
        (WO-NUDGE-INF-1: a missing marker is a stall, re-nudged until it lands,
        never an attempt failure) -> inter-chunk compaction phase (fail-open,
        skipped after the last chunk). Aggregates are the sum of per-chunk
        deltas (the per-phase metering in :meth:`_run_opencode_serve` is
        delta-true).
        """
        sum_input = 0
        sum_output = 0
        sum_reasoning = 0
        sum_turns = 0
        sum_cost = 0.0
        sum_truncations = 0
        sum_recovery_nudges = 0
        sum_guard_aborted = 0
        sum_finalize_timeouts = 0
        sum_observation_lost = 0
        anomalies: list[dict[str, Any]] = []
        chunk_reports: list[dict[str, Any]] = []

        def _aggregate(*, exit_code: int | None, killed_reason: str | None) -> _OpencodeRunStats:
            return _OpencodeRunStats(
                input_tokens=sum_input,
                output_tokens=sum_output,
                reasoning_tokens=sum_reasoning,
                turns=sum_turns,
                session_id=session_id,
                killed_reason=killed_reason,
                exit_code=exit_code,
                cost_usd=sum_cost,
                truncations=sum_truncations,
                turn_anomalies=tuple(anomalies),
                chunk_reports=tuple(chunk_reports),
                recovery_nudges=sum_recovery_nudges,
                guard_aborted_turns=sum_guard_aborted,
                finalize_timeout_turns=sum_finalize_timeouts,
                observation_lost_turns=sum_observation_lost,
            )

        def _drive(phase: str, prompt: str) -> _OpencodeRunStats:
            nonlocal sum_input, sum_output, sum_reasoning, sum_turns, sum_cost, sum_truncations
            nonlocal sum_recovery_nudges, sum_guard_aborted, sum_finalize_timeouts
            nonlocal sum_observation_lost
            stats = self._run_opencode_serve(
                active_cell=active_cell,
                serve_client=serve_client,
                session_id=session_id,
                prompt=prompt,
                run_label=run_label,
                phase=phase,
                prior_cost_usd=prior_cost_usd + sum_cost,
                timeout_s=timeout_s,
                kill_hook=kill_hook,
            )
            sum_input += stats.input_tokens
            sum_output += stats.output_tokens
            sum_reasoning += stats.reasoning_tokens
            sum_turns += stats.turns
            sum_cost += stats.cost_usd
            sum_truncations += stats.truncations
            sum_recovery_nudges += stats.recovery_nudges
            sum_guard_aborted += stats.guard_aborted_turns
            sum_finalize_timeouts += stats.finalize_timeout_turns
            sum_observation_lost += stats.observation_lost_turns
            anomalies.extend(stats.turn_anomalies)
            return stats

        compact_enabled = (os.environ.get("WEVIBE_BENCH_CHUNK_COMPACT") or "1").strip() != "0"
        compact_grace_s = float(os.environ.get("WEVIBE_BENCH_COMPACT_GRACE_S") or "15")
        compact_total_s = float(os.environ.get("WEVIBE_BENCH_COMPACT_TOTAL_S") or "1800")

        for index, chunk_prompt in enumerate(prompts, start=1):
            phase = f"initial-chunk-{index}"
            report: dict[str, Any] = {
                "chunk": index,
                "nudged": False,
                "nudges": 0,
                "marker": False,
                "compaction": None,
                "recovery_nudges": 0,
                "guard_aborted_turns": 0,
                "finalize_timeout_turns": 0,
            }
            # Watermark BEFORE the drive: the marker scan below covers only the
            # messages THIS chunk produced, so a marker from an earlier chunk
            # can never satisfy a later one.
            watermark = len(serve_client.get_messages(session_id))
            stats = _drive(phase, chunk_prompt)
            report["recovery_nudges"] += stats.recovery_nudges
            report["guard_aborted_turns"] += stats.guard_aborted_turns
            report["finalize_timeout_turns"] += stats.finalize_timeout_turns
            report.update(
                turns=stats.turns,
                input_tokens=stats.input_tokens,
                output_tokens=stats.output_tokens,
                exit_code=stats.exit_code,
            )
            chunk_reports.append(report)
            if stats.exit_code != 0:
                return _aggregate(exit_code=stats.exit_code, killed_reason=stats.killed_reason)
            while not any(
                CHUNK_MARKER in text
                for text in serve_client.assistant_texts_since(session_id, watermark)
            ):
                report["nudges"] += 1
                report["nudged"] = True
                self._progress(
                    f"PROGRESS run_label={run_label} step=chunk-marker-missing "
                    f"chunk={index} session_id={session_id} action=nudge "
                    f"nudge={report['nudges']} budget=unbounded"
                )
                nudge = (
                    f"Your previous response did not end with the {CHUNK_MARKER} marker. "
                    "If the current chunk task is fully complete, reply with exactly "
                    f"{CHUNK_MARKER}. Otherwise finish the remaining work for this chunk "
                    f"first, then end with {CHUNK_MARKER}."
                )
                nudge_stats = _drive(f"{phase}-marker-nudge-{report['nudges']}", nudge)
                report["recovery_nudges"] += nudge_stats.recovery_nudges
                report["guard_aborted_turns"] += nudge_stats.guard_aborted_turns
                report["finalize_timeout_turns"] += nudge_stats.finalize_timeout_turns
                if nudge_stats.exit_code != 0:
                    return _aggregate(
                        exit_code=nudge_stats.exit_code, killed_reason=nudge_stats.killed_reason
                    )
            report["marker"] = True

            if compact_enabled and index < len(prompts):
                report["compaction"] = self._await_chunk_compaction(
                    serve_client=serve_client,
                    session_id=session_id,
                    watermark=watermark,
                    run_label=run_label,
                    chunk=index,
                    grace_s=compact_grace_s,
                    total_s=compact_total_s,
                )

        return _aggregate(exit_code=0, killed_reason=None)

    def _await_chunk_compaction(
        self,
        *,
        serve_client: ServeClient,
        session_id: str,
        watermark: int,
        run_label: str,
        chunk: int,
        grace_s: float,
        total_s: float,
    ) -> str:
        """Inter-chunk compaction, FAIL-OPEN (a run without compaction is the
        pre-2026-08-10 status quo; a wedged probe must never kill a cell).

        The worker is instructed to end each chunk by calling its self_compact
        tool (arm-on-idle -> session.summarize). This phase watches for that
        self-fired compaction: the session going busy within ``grace_s`` of the
        marker turn, then a compaction part landing within ``total_s``. When
        neither materializes the harness fires the backstop summarize itself
        (auto=False — the harness, not a synthetic continue turn, sends the
        next chunk). Returns the outcome recorded in the chunk report.
        """
        deadline_grace = time.monotonic() + grace_s
        deadline_total = time.monotonic() + total_s
        saw_busy = False
        idle_since: float | None = None
        while time.monotonic() < deadline_total:
            try:
                if serve_client.compaction_since(session_id, watermark):
                    return "self"
                busy = serve_client.session_busy(session_id)
            except Exception as exc:  # probe failure must not wedge the loop
                self._progress(
                    f"PROGRESS run_label={run_label} step=chunk-compaction "
                    f"chunk={chunk} session_id={session_id} probe_error={exc!r} "
                    f"action=continue"
                )
                return "probe_error"
            now = time.monotonic()
            if busy:
                saw_busy = True
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = now
                if not saw_busy and now >= deadline_grace:
                    break  # agent never armed a compaction -> backstop below
                if saw_busy and now - idle_since > 10.0:
                    # A busy window ended without compaction evidence: the
                    # summarize failed server-side. Fail open.
                    return "no_evidence"
            time.sleep(min(serve_client.poll_interval, 5.0))
        else:
            self._progress(
                f"PROGRESS run_label={run_label} step=chunk-compaction "
                f"chunk={chunk} session_id={session_id} action=timeout"
            )
            return "timeout"

        return self._fire_backstop_summarize(
            serve_client=serve_client,
            session_id=session_id,
            watermark=watermark,
            run_label=run_label,
            step="chunk-compaction",
            label=f"chunk={chunk}",
            timeout_s=max(60.0, deadline_total - time.monotonic()),
        )

    def _fire_backstop_summarize(
        self,
        *,
        serve_client: ServeClient,
        session_id: str,
        watermark: int,
        run_label: str,
        step: str,
        label: str,
        timeout_s: float,
    ) -> str:
        """Fail-open backstop summarize shared by the inter-chunk watch and the
        attempt-boundary compaction: resolve the session model, POST
        summarize(auto=False), then confirm a compaction part landed."""
        model = serve_client.session_model(session_id)
        if model is None:
            self._progress(
                f"PROGRESS run_label={run_label} step={step} "
                f"{label} session_id={session_id} action=backstop-skipped "
                f"reason=no-model"
            )
            return "skipped_no_model"
        provider_id, model_id = model
        self._progress(
            f"PROGRESS run_label={run_label} step={step} "
            f"{label} session_id={session_id} action=backstop "
            f"provider={provider_id} model={model_id}"
        )
        try:
            serve_client.summarize(
                session_id,
                provider_id=provider_id,
                model_id=model_id,
                auto=False,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            self._progress(
                f"PROGRESS run_label={run_label} step={step} "
                f"{label} session_id={session_id} backstop_error={exc!r}"
            )
            return "backstop_error"
        if serve_client.compaction_since(session_id, watermark):
            return "backstop"
        # Async-accept servers: brief confirm window before giving up.
        confirm_deadline = time.monotonic() + 30.0
        while time.monotonic() < confirm_deadline:
            if serve_client.compaction_since(session_id, watermark):
                return "backstop"
            time.sleep(min(serve_client.poll_interval, 5.0))
        return "backstop_unconfirmed"

    def _compact_attempt_boundary(
        self,
        *,
        serve_client: ServeClient | None,
        session_id: str | None,
        run_label: str,
        attempt: int,
    ) -> str:
        """Attempt-boundary compaction, FAIL-OPEN (same posture as the
        inter-chunk watch: a compaction failure must never kill a cell).

        The inter-chunk machinery compacts only between chunks 1-5 of the
        first pass; chunk 6 and every failure-phase repair turn otherwise ride
        one uncompacted tail for the rest of the cell. The failure-phase
        prompts never instruct self_compact, so there is nothing to watch for
        — fire the backstop summarize directly so the next feedback run starts
        on a compacted transcript.
        """
        if (os.environ.get("WEVIBE_BENCH_CHUNK_COMPACT") or "1").strip() == "0":
            return "disabled"
        if serve_client is None or not session_id:
            self._progress(
                f"PROGRESS run_label={run_label} step=attempt-compaction "
                f"attempt={attempt} session_id={session_id or 'none'} "
                "action=skipped reason=no-serve-session"
            )
            return "skipped_no_serve"
        try:
            watermark = len(serve_client.get_messages(session_id))
        except Exception as exc:  # probe failure must not wedge the loop
            self._progress(
                f"PROGRESS run_label={run_label} step=attempt-compaction "
                f"attempt={attempt} session_id={session_id} watermark_error={exc!r}"
            )
            return "watermark_error"
        total_s = float(os.environ.get("WEVIBE_BENCH_COMPACT_TOTAL_S") or "1800")
        return self._fire_backstop_summarize(
            serve_client=serve_client,
            session_id=session_id,
            watermark=watermark,
            run_label=run_label,
            step="attempt-compaction",
            label=f"attempt={attempt}",
            timeout_s=max(60.0, total_s),
        )

    def _run_opencode(
        self,
        *,
        cmd: list[str],
        worktree: Path,
        events_path: Path,
        env: dict[str, str],
        run_label: str,
        phase: str,
        fallback_session_id: str | None,
        prior_cost_usd: float = 0.0,
        kill_hook: Callable[[], None] | None = None,
        stdin_text: str | None = None,
    ) -> _OpencodeRunStats:
        state_lock = threading.Lock()
        state: dict[str, Any] = {
            "session_id": fallback_session_id,
            "turns": 0,
            "sum_output": 0,
            "sum_reasoning": 0,
            "max_input": 0,
            "sum_cost": 0.0,
            "completed_at": None,
            "budget_stop_detected": False,
            "budget_stop_signature": None,
            "truncations": 0,
            "zero_tool_turns": 0,
            "terminal_zero_tool_turn": False,
            "active_tool_uses": 0,
            "active_file_writes": 0,
            "active_step_index": 0,
            # WO-TRUNC-1 turn-terminal tracking. ``active_step_open`` marks an
            # in-flight step (the ts is only for wall measurement and may be
            # absent); ``turn_anomalies`` accumulates one record per
            # anomalously-ended turn; ``unretried_anomaly`` indexes the latest
            # anomaly a later step_start may still mark as retried.
            "active_step_open": False,
            "active_step_open_ts_ms": None,
            "turn_anomalies": [],
            "unretried_anomaly": None,
            "unmetered_turns": 0,
            "unmetered_turn_wall_s": 0.0,
            # WO-WATCH-1E evidence capture fields: a per-attempt correlation id
            # and stream counters the stdout reader updates so a real
            # truncation/transport-error is self-documenting at capture time.
            "attempt_id": None,
            "ts_start_epoch_ms": None,
            "bytes_read": 0,
            "lines_read": 0,
            "last_event_type": None,
            "last_event_ts": None,
        }
        if not state["attempt_id"]:
            state["attempt_id"] = f"{run_label}-{phase}-{uuid.uuid4().hex[:12]}"
            state["ts_start_epoch_ms"] = int(time.time() * 1000)
        evidence_path = worktree.parent / TRUNCATION_EVIDENCE_FILENAME
        stderr_tail: collections.deque[str] = collections.deque(maxlen=120)
        reader_failures: list[str] = []

        started = time.monotonic()
        events_path.parent.mkdir(parents=True, exist_ok=True)
        container_name = cmd[5] if len(cmd) > 5 and cmd[:2] == ["docker", "exec"] else "none"
        self._progress(
            f"PROGRESS run_label={run_label} step=session-db path={worktree.parent / 'session-db'} "
            f"container={container_name} — live worker session observable here"
        )

        with events_path.open("a", encoding="utf-8") as events_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                text=True,
                bufsize=1,
                start_new_session=True,
                env=env,
            )

            stdin_writer_thread: threading.Thread | None = None
            if stdin_text is not None:
                payload = str(stdin_text)
                payload_chars = len(payload)
                payload_fp = self._fingerprint_text(payload)

                def stdin_writer() -> None:
                    try:
                        if proc.stdin is None:
                            self._progress(
                                f"INFO op=worker-stdin-write run_label={run_label} phase={phase} "
                                "status=skipped reason=stdin_not_available"
                            )
                            return
                        proc.stdin.write(payload)
                        proc.stdin.flush()
                        self._progress(
                            f"PROGRESS op=worker-stdin-write run_label={run_label} phase={phase} "
                            f"status=ok chars={payload_chars} text_fp={payload_fp}"
                        )
                    except BrokenPipeError:
                        self._progress(
                            f"INFO op=worker-stdin-write run_label={run_label} phase={phase} "
                            f"status=broken_pipe chars={payload_chars} text_fp={payload_fp}"
                        )
                    except Exception as exc:  # noqa: BLE001 - surface and continue teardown.
                        reader_failures.append(f"stdin writer failure ({phase}): {exc}")
                    finally:
                        try:
                            if proc.stdin:
                                proc.stdin.close()
                        except Exception:
                            pass

                stdin_writer_thread = threading.Thread(
                    target=stdin_writer,
                    name=f"bg-stdin-{phase}",
                    daemon=True,
                )
                stdin_writer_thread.start()

            stdout_drained = threading.Event()
            stderr_drained = threading.Event()

            def stdout_reader() -> None:
                try:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        events_fh.write(line)
                        events_fh.flush()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            if self._line_indicates_budget_stop(line):
                                line_fp = self._fingerprint_text(line)
                                with state_lock:
                                    state["budget_stop_detected"] = True
                                    if not state["budget_stop_signature"]:
                                        state["budget_stop_signature"] = f"stdout_line_fp={line_fp}"
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=budget-stop-detected phase={phase} "
                                    f"source=stdout-unparsed line_fp={line_fp}"
                                )
                            continue

                        sid = event.get("sessionID")
                        event_type = event.get("type")
                        event_ts = event.get("timestamp")
                        with state_lock:
                            state["bytes_read"] = self._to_int(state.get("bytes_read", 0)) + len(line.encode("utf-8"))
                            state["lines_read"] = self._to_int(state.get("lines_read", 0)) + 1
                            state["last_event_type"] = event_type
                            if event_ts is not None:
                                state["last_event_ts"] = event_ts
                            if sid and not state["session_id"]:
                                state["session_id"] = str(sid)

                        if event_type == "error":
                            signal = self._budget_stop_signature_from_event(event)
                            if signal is not None:
                                with state_lock:
                                    state["budget_stop_detected"] = True
                                    if not state["budget_stop_signature"]:
                                        state["budget_stop_signature"] = signal
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=budget-stop-detected phase={phase} "
                                    f"source=event-error signal={signal}"
                                )
                                continue
                            # WO-TRUNC-1: a non-budget error event is a terminal
                            # signal for the in-flight turn — the stream died (or
                            # the proxy guard aborted it) and the open step will
                            # never see a step_finish. Close it as an anomaly.
                            error_reason = self._classify_transport_error(event)
                            terminal = (
                                TURN_TERMINAL_GUARD_ABORT
                                if error_reason == "loop_guard"
                                else TURN_TERMINAL_TRANSPORT_ERROR
                            )
                            with state_lock:
                                open_index = self._to_int(state.get("active_step_index", 0))
                                open_ts = state.get("active_step_open_ts_ms")
                                wall_s = self._step_wall_seconds(open_ts, event.get("timestamp"))
                                self._append_turn_anomaly(
                                    state,
                                    phase=phase,
                                    turn_index=open_index,
                                    terminal=terminal,
                                    reason=error_reason or "error_event",
                                    tool_uses=self._to_int(state.get("active_tool_uses", 0)),
                                    file_writes=self._to_int(state.get("active_file_writes", 0)),
                                    input_tokens=0,
                                    output_tokens=0,
                                    reasoning_tokens=0,
                                    cost_usd=0.0,
                                    tokens_unmetered=True,
                                    wall_seconds=wall_s,
                                    session_id=state.get("session_id"),
                                )
                                self._capture_truncation_evidence(
                                    state=state,
                                    run_label=run_label,
                                    phase=phase,
                                    terminal=terminal,
                                    reason=error_reason or "error_event",
                                    wall_seconds=wall_s,
                                    finish_reason=None,
                                    evidence_path=evidence_path,
                                )
                                state["active_step_open"] = False
                                state["active_step_open_ts_ms"] = None
                                state["active_tool_uses"] = 0
                                state["active_file_writes"] = 0
                            self._progress(
                                f"PROGRESS run_label={run_label} step=TURN-TERMINAL phase={phase} "
                                f"turn={open_index} terminal={terminal} "
                                f"reason={error_reason or 'error_event'} unmetered=1"
                            )
                            continue
                        if event_type == "step_start":
                            with state_lock:
                                state["completed_at"] = None
                                state["active_tool_uses"] = 0
                                state["active_file_writes"] = 0
                                state["active_step_index"] = self._to_int(state.get("active_step_index", 0)) + 1
                                state["active_step_open"] = True
                                state["active_step_open_ts_ms"] = event.get("timestamp")
                                state["terminal_zero_tool_turn"] = False
                                # A new step after an anomalous terminal is the
                                # client's internal auto-retry of the burned turn.
                                self._mark_anomaly_retried(state, retry_kind="client_auto")
                            continue
                        if event_type == "tool_use":
                            part = event.get("part") if isinstance(event.get("part"), dict) else {}
                            tool_name = part.get("tool")
                            if not isinstance(tool_name, str):
                                tool_name = part.get("name")
                            tool_name_normalized = str(tool_name).strip().lower()
                            with state_lock:
                                state["active_tool_uses"] = self._to_int(state.get("active_tool_uses", 0)) + 1
                                if tool_name_normalized in _FILE_WRITE_TOOL_NAMES:
                                    state["active_file_writes"] = self._to_int(state.get("active_file_writes", 0)) + 1
                            continue
                        if event_type != "step_finish":
                            continue

                        part = event.get("part") if isinstance(event.get("part"), dict) else {}
                        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
                        reason = part.get("reason")
                        input_tokens = max(0, self._to_int(tokens.get("input")))
                        output_tokens = max(0, self._to_int(tokens.get("output")))
                        reasoning_tokens = max(0, self._to_int(tokens.get("reasoning")))
                        step_cost = part.get("cost")

                        with state_lock:
                            state["turns"] += 1
                            state["sum_output"] += output_tokens
                            state["sum_reasoning"] += reasoning_tokens
                            state["sum_cost"] += float(step_cost) if isinstance(step_cost, (int, float)) else 0.0
                            if input_tokens > state["max_input"]:
                                state["max_input"] = input_tokens
                            turn_index = self._to_int(state.get("active_step_index", 0))
                            open_ts = state.get("active_step_open_ts_ms")
                            state["active_step_open"] = False
                            state["active_step_open_ts_ms"] = None
                            if reason == "stop":
                                state["completed_at"] = time.monotonic()
                            elif reason == "length":
                                state["truncations"] = self._to_int(state.get("truncations", 0)) + 1
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=TRUNCATION phase={phase} "
                                    f"turn={state['turns']} reason=length"
                                )

                            tool_uses = self._to_int(state.get("active_tool_uses", 0))
                            file_writes = self._to_int(state.get("active_file_writes", 0))

                            if reason in TRUNCATED_STEP_FINISH_REASONS:
                                # WO-TRUNC-1: the stream ended without a finish
                                # reason; the client synthesized this finish with
                                # a zeroed usage frame. The turn burned real
                                # upstream tokens that never reached the client —
                                # recorded as unmetered, never synthesized. This
                                # is NOT a zero-tool turn: the model did not
                                # choose silence, the transport chose for it.
                                unmetered = (
                                    input_tokens == 0 and output_tokens == 0 and reasoning_tokens == 0
                                )
                                wall_s = self._step_wall_seconds(open_ts, event.get("timestamp"))
                                self._append_turn_anomaly(
                                    state,
                                    phase=phase,
                                    turn_index=turn_index,
                                    terminal=TURN_TERMINAL_TRUNCATED,
                                    reason=str(reason),
                                    tool_uses=tool_uses,
                                    file_writes=file_writes,
                                    input_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    reasoning_tokens=reasoning_tokens,
                                    cost_usd=float(step_cost) if isinstance(step_cost, (int, float)) else 0.0,
                                    tokens_unmetered=unmetered,
                                    wall_seconds=wall_s,
                                    session_id=state.get("session_id"),
                                )
                                self._capture_truncation_evidence(
                                    state=state,
                                    run_label=run_label,
                                    phase=phase,
                                    terminal=TURN_TERMINAL_TRUNCATED,
                                    reason=str(reason),
                                    wall_seconds=wall_s,
                                    finish_reason=str(reason),
                                    evidence_path=evidence_path,
                                )
                                state["terminal_zero_tool_turn"] = False
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=TURN-TERMINAL phase={phase} "
                                    f"turn={turn_index} terminal={TURN_TERMINAL_TRUNCATED} "
                                    f"reason={reason} unmetered={1 if unmetered else 0}"
                                )
                            elif reason not in _NORMAL_STEP_FINISH_REASONS:
                                wall_s = self._step_wall_seconds(open_ts, event.get("timestamp"))
                                self._append_turn_anomaly(
                                    state,
                                    phase=phase,
                                    turn_index=turn_index,
                                    terminal=TURN_TERMINAL_UNCLASSIFIED_FINISH,
                                    reason=str(reason),
                                    tool_uses=tool_uses,
                                    file_writes=file_writes,
                                    input_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    reasoning_tokens=reasoning_tokens,
                                    cost_usd=float(step_cost) if isinstance(step_cost, (int, float)) else 0.0,
                                    tokens_unmetered=False,
                                    wall_seconds=wall_s,
                                    session_id=state.get("session_id"),
                                )

                            if reason in TRUNCATED_STEP_FINISH_REASONS:
                                pass
                            elif tool_uses == 0 and file_writes == 0:
                                state["zero_tool_turns"] = self._to_int(state.get("zero_tool_turns", 0)) + 1
                                state["terminal_zero_tool_turn"] = True
                                self._progress(
                                    f"PROGRESS run_label={run_label} step=zero-tool-turn phase={phase} "
                                    f"turn={state['turns']} tool_uses=0 file_writes=0"
                                )
                            else:
                                state["terminal_zero_tool_turn"] = False

                            state["active_tool_uses"] = 0
                            state["active_file_writes"] = 0
                    stdout_drained.set()
                except Exception as exc:  # noqa: BLE001 - log and continue teardown.
                    reader_failures.append(f"stdout reader failure ({phase}): {exc}")
                finally:
                    try:
                        if proc.stdout:
                            proc.stdout.close()
                    except Exception:
                        pass

            def stderr_reader() -> None:
                try:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        text = line.rstrip("\n")
                        stderr_tail.append(text)
                        if self._line_indicates_budget_stop(text):
                            line_fp = self._fingerprint_text(text)
                            with state_lock:
                                state["budget_stop_detected"] = True
                                if not state["budget_stop_signature"]:
                                    state["budget_stop_signature"] = f"stderr_line_fp={line_fp}"
                            self._progress(
                                f"PROGRESS run_label={run_label} step=budget-stop-detected phase={phase} "
                                f"source=stderr line_fp={line_fp}"
                            )
                        self._progress(
                            f"PROGRESS run_label={run_label} step=worker-stderr phase={phase} line={text}"
                        )
                    stderr_drained.set()
                except Exception as exc:  # noqa: BLE001 - log and continue teardown.
                    reader_failures.append(f"stderr reader failure ({phase}): {exc}")
                finally:
                    try:
                        if proc.stderr:
                            proc.stderr.close()
                    except Exception:
                        pass

            stdout_thread = threading.Thread(target=stdout_reader, name=f"bg-stdout-{phase}", daemon=True)
            stderr_thread = threading.Thread(target=stderr_reader, name=f"bg-stderr-{phase}", daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            killed_reason: str | None = None
            kill_hook_ran = False
            target_warning_emitted = False

            if self.cost_target_usd is not None and prior_cost_usd >= self.cost_target_usd:
                target_warning_emitted = True
                self._progress(
                    f"WARNING run_label={run_label} step=cost-target phase={phase} "
                    f"reason=cost_target_reached cumulative_cost_usd={prior_cost_usd:.4f} "
                    f"target_usd={self.cost_target_usd:.4f}"
                )

            def run_kill_hook(*, reason: str) -> None:
                nonlocal kill_hook_ran
                if kill_hook is None or kill_hook_ran:
                    return
                kill_hook_ran = True
                try:
                    kill_hook()
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill-hook phase={phase} "
                        f"reason={reason} status=ok"
                    )
                except Exception as exc:  # noqa: BLE001 - surface and continue teardown.
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill-hook phase={phase} "
                        f"reason={reason} status=error detail={exc}"
                    )

            while True:
                rc = proc.poll()
                elapsed = time.monotonic() - started
                with state_lock:
                    est_tokens = state["sum_output"] + state["sum_reasoning"] + state["max_input"]
                    turns = self._to_int(state["turns"])
                    sum_cost = float(state["sum_cost"])
                    cumulative_cost = prior_cost_usd + sum_cost

                if (
                    self.cost_target_usd is not None
                    and not target_warning_emitted
                    and cumulative_cost >= self.cost_target_usd
                ):
                    target_warning_emitted = True
                    self._progress(
                        f"WARNING run_label={run_label} step=cost-target phase={phase} "
                        f"reason=cost_target_reached cumulative_cost_usd={cumulative_cost:.4f} "
                        f"target_usd={self.cost_target_usd:.4f}"
                    )

                if rc is not None:
                    break
                # Bound process teardown after a final stop while allowing any subsequent resumed step to reset the grace window.
                with state_lock:
                    completed_at = state["completed_at"]
                if completed_at is not None and (time.monotonic() - completed_at) >= self.completion_grace_s:
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-complete phase={phase} "
                        f"reason=idle_after_stop grace={self.completion_grace_s}s elapsed={elapsed:.2f}s"
                    )
                    self._kill_process_group(proc)
                    break
                if elapsed > self.run_timeout_s:
                    killed_reason = "run_timeout"
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill phase={phase} "
                        f"reason=run_timeout elapsed={elapsed:.2f}s limit={self.run_timeout_s}s"
                    )
                    self._kill_process_group(proc)
                    run_kill_hook(reason="run_timeout")
                    break
                if self.max_steps_per_attempt is not None and turns > self.max_steps_per_attempt:
                    killed_reason = "max_steps_per_attempt"
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill phase={phase} "
                        f"reason=max_steps_per_attempt turns={turns} max_steps_per_attempt={self.max_steps_per_attempt}"
                    )
                    self._kill_process_group(proc)
                    run_kill_hook(reason="max_steps_per_attempt")
                    break
                if est_tokens > self.token_cap:
                    killed_reason = "token_cap"
                    self._progress(
                        f"PROGRESS run_label={run_label} step=worker-kill phase={phase} "
                        f"reason=token_cap est_tokens={est_tokens} cap={self.token_cap}"
                    )
                    self._kill_process_group(proc)
                    run_kill_hook(reason="token_cap")
                    break
                time.sleep(2.0)

            if proc.poll() is None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._kill_process_group(proc)
                    proc.wait(timeout=5)

            # Block on the reader threads: the child was already reaped above
            # (proc.wait / kill+wait), so its stdout/stderr write-ends are closed
            # and these loops hit EOF. A blocking join therefore cannot hang — it
            # only waits for the readers to drain the final buffered output. The
            # former 5s timeout could abandon the daemon reader while it still
            # held the trailing step_finish, silently dropping it under load.
            stdout_thread.join()
            stderr_thread.join()
            if not stdout_drained.is_set():
                self._progress(
                    f"PROGRESS run_label={run_label} step=reader-drain phase={phase} "
                    "stream=stdout status=incomplete detail=reader_did_not_reach_eof"
                )
            if not stderr_drained.is_set():
                self._progress(
                    f"PROGRESS run_label={run_label} step=reader-drain phase={phase} "
                    "stream=stderr status=incomplete detail=reader_did_not_reach_eof"
                )
            if stdin_writer_thread is not None:
                stdin_writer_thread.join(timeout=5)
                if stdin_writer_thread.is_alive():
                    self._progress(
                        f"INFO op=worker-stdin-write run_label={run_label} phase={phase} "
                        "status=join_timeout timeout_s=5"
                    )
            exit_code = proc.returncode

        if (
            self.cost_target_usd is not None
            and not target_warning_emitted
            and (prior_cost_usd + float(state.get("sum_cost", 0.0))) >= self.cost_target_usd
        ):
            self._progress(
                f"WARNING run_label={run_label} step=cost-target phase={phase} "
                f"reason=cost_target_reached cumulative_cost_usd={(prior_cost_usd + float(state.get('sum_cost', 0.0))):.4f} "
                f"target_usd={self.cost_target_usd:.4f}"
            )

        for failure in reader_failures:
            self._progress(f"PROGRESS run_label={run_label} step=reader-failure phase={phase} detail={failure}")

        # WO-TRUNC-1: the process exited with a step still open — no step_finish,
        # no error event. The turn burned upstream tokens and wall-clock with no
        # terminal signal of any kind. Close it as stream_died_open and count it
        # toward the turn budget: a truncation storm must never run unbounded,
        # and a burned turn must never be invisible.
        with state_lock:
            open_ts = state.get("active_step_open_ts_ms")
            if open_ts is not None:
                open_index = self._to_int(state.get("active_step_index", 0))
                wall_s = self._step_wall_seconds(open_ts, int(time.time() * 1000))
                state["turns"] += 1
                self._append_turn_anomaly(
                    state,
                    phase=phase,
                    turn_index=open_index,
                    terminal=TURN_TERMINAL_STREAM_DIED_OPEN,
                    reason="no_terminal_signal",
                    tool_uses=self._to_int(state.get("active_tool_uses", 0)),
                    file_writes=self._to_int(state.get("active_file_writes", 0)),
                    input_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=0,
                    cost_usd=0.0,
                    tokens_unmetered=True,
                    wall_seconds=wall_s,
                    session_id=state.get("session_id"),
                )
                self._capture_truncation_evidence(
                    state=state,
                    run_label=run_label,
                    phase=phase,
                    terminal=TURN_TERMINAL_STREAM_DIED_OPEN,
                    reason="no_terminal_signal",
                    wall_seconds=wall_s,
                    finish_reason=None,
                    evidence_path=evidence_path,
                )
                state["active_step_open_ts_ms"] = None
                state["active_tool_uses"] = 0
                state["active_file_writes"] = 0
                state["terminal_zero_tool_turn"] = False
                self._progress(
                    f"PROGRESS run_label={run_label} step=TURN-TERMINAL phase={phase} "
                    f"turn={open_index} terminal={TURN_TERMINAL_STREAM_DIED_OPEN} "
                    f"reason=no_terminal_signal unmetered=1 exit={exit_code}"
                )

        with state_lock:
            session_id = state["session_id"]
            turns = self._to_int(state["turns"])
            input_tokens = self._to_int(state["max_input"])
            output_tokens = self._to_int(state["sum_output"])
            reasoning_tokens = self._to_int(state["sum_reasoning"])
            cost_usd = float(state["sum_cost"])
            budget_stop_detected = bool(state.get("budget_stop_detected", False))
            budget_stop_signature = state.get("budget_stop_signature")
            truncations = self._to_int(state.get("truncations", 0))
            zero_tool_turns = self._to_int(state.get("zero_tool_turns", 0))
            terminal_zero_tool_turn = bool(state.get("terminal_zero_tool_turn", False))
            turn_anomalies = tuple(dict(record) for record in state.get("turn_anomalies") or [])
            unmetered_turns = self._to_int(state.get("unmetered_turns", 0))
            unmetered_turn_wall_s = float(state.get("unmetered_turn_wall_s", 0.0))

        if exit_code not in (0, None) and stderr_tail:
            self._progress(
                f"PROGRESS run_label={run_label} step=worker-nonzero phase={phase} exit={exit_code} "
                f"stderr_tail={' | '.join(stderr_tail)}"
            )

        external_signal = self._external_signal_from_exit_code(exit_code)
        if (
            exit_code not in (0, None)
            and killed_reason is None
            and not kill_hook_ran
            and external_signal is not None
        ):
            self._progress(
                f"PROGRESS op=worker.external_exit run_label={run_label} phase={phase} "
                f"exit={exit_code} signal={external_signal} attribution=external killed_reason=none"
            )

        if not budget_stop_detected and stderr_tail:
            stderr_blob = "\n".join(stderr_tail)
            if self._line_indicates_budget_stop(stderr_blob):
                budget_stop_detected = True
                budget_stop_signature = f"stderr_tail_fp={self._fingerprint_text(stderr_blob)}"
                self._progress(
                    f"PROGRESS run_label={run_label} step=budget-stop-detected phase={phase} "
                    f"source=stderr-tail signal={budget_stop_signature}"
                )

        return _OpencodeRunStats(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            turns=turns,
            session_id=session_id,
            killed_reason=killed_reason,
            exit_code=exit_code,
            cost_usd=cost_usd,
            budget_stop_detected=budget_stop_detected,
            budget_stop_signature=None if budget_stop_signature is None else str(budget_stop_signature),
            truncations=truncations,
            zero_tool_turns=zero_tool_turns,
            terminal_zero_tool_turn=terminal_zero_tool_turn,
            turn_anomalies=turn_anomalies,
            unmetered_turns=unmetered_turns,
            unmetered_turn_wall_s=unmetered_turn_wall_s,
        )

    def _emit_cost_target_warning_if_reached(
        self,
        *,
        run_label: str,
        phase: str,
        cumulative_cost_usd: float,
    ) -> None:
        if self.cost_target_usd is None:
            return
        if cumulative_cost_usd < self.cost_target_usd:
            return
        self._progress(
            f"WARNING run_label={run_label} step=cost-target phase={phase} "
            f"reason=cost_target_reached cumulative_cost_usd={cumulative_cost_usd:.4f} "
            f"target_usd={self.cost_target_usd:.4f}"
        )

    def _append_user_event(
        self,
        *,
        run_label: str,
        sidecar_path: Path,
        attempt: int,
        text: str,
        kind: str = "feedback",
    ) -> None:
        """Record, VERBATIM, every message the model is told a user sent.

        THIS FILE IS THE TRUTH (WO-FEEDBACK-1). It is the only place the exact
        bytes handed to the model are preserved — the PROGRESS log carries a
        length and a fingerprint but not the text, and the worker's own event
        stream shows the message only as it was consumed. The control plane
        serves this file so the TUI and the event feed show the operator what
        the model was actually told, not a reconstruction of it.

        `kind` distinguishes the three voices: `chunk` (the task itself),
        `pass_verdict` ("that fixed it"), `feedback` ("still failing"). The UI
        needs that separation — a chunk prompt and a failure report are not the
        same kind of message and must not render identically.

        Append-only, one JSON object per line: a run that dies mid-write leaves
        every earlier message intact and parseable.
        """
        payload = {
            "type": "user",
            "kind": str(kind),
            "timestamp": int(time.time() * 1000),
            "attempt": int(attempt),
            "chars": len(str(text)),
            "text_fp": self._fingerprint_text(text),
            "text": str(text),
        }
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        with sidecar_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        # The marker stays a fingerprint + length, never the body: this text is
        # multi-line and a single-line log record cannot carry it without
        # corrupting either the text or the log.
        self._progress(
            f"PROGRESS run_label={run_label} step=user-event-sidecar attempt={attempt} "
            f"kind={kind} chars={len(text)} text_fp={self._fingerprint_text(text)} "
            f"path={sidecar_path}"
        )

    def _extract_event_counts(self, events_path: Path) -> tuple[int | None, int | None]:
        """Return (tool_calls, test_invocations) from an opencode events jsonl file.

        test_invocations counts bash tool_use events where state.input.command
        contains any DECLARED_TEST_COMMANDS entry (plain case-sensitive
        substring match).
        """
        malformed_lines = 0
        tool_calls = 0
        test_invocations = 0

        try:
            with events_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue

                    if not isinstance(payload, dict):
                        malformed_lines += 1
                        continue

                    if payload.get("type") != "tool_use":
                        continue

                    tool_calls += 1
                    part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
                    if part.get("tool") != "bash":
                        continue

                    state = part.get("state") if isinstance(part.get("state"), dict) else {}
                    tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
                    command = tool_input.get("command")
                    if not isinstance(command, str):
                        continue

                    if any(declared in command for declared in DECLARED_TEST_COMMANDS):
                        test_invocations += 1
        except OSError as exc:
            _LOG.warning(
                "backgammon event telemetry unavailable path=%s error_class=%s",
                events_path,
                exc.__class__.__name__,
            )
            return None, None

        if malformed_lines > 0:
            _LOG.warning(
                "backgammon event telemetry malformed_lines=%d path=%s",
                malformed_lines,
                events_path,
            )

        return tool_calls, test_invocations

    def _extract_agentic_cycles(self, user_events_path: Path) -> int | None:
        """Return number of context-submission cycles from user-events jsonl.

        One cycle equals one user context submission (initial prompt plus each
        feedback injection). When attempt fields exist, cycles are counted as
        distinct attempt values; if parsed user lines have no attempt fields,
        fallback is the number of parsed user lines.
        """
        malformed_lines = 0
        user_line_count = 0
        attempts: set[int] = set()
        saw_attempt_field = False

        try:
            with user_events_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        malformed_lines += 1
                        continue

                    if not isinstance(payload, dict):
                        malformed_lines += 1
                        continue
                    if payload.get("type") != "user":
                        continue

                    user_line_count += 1
                    if "attempt" not in payload:
                        continue

                    attempt = payload.get("attempt")
                    try:
                        attempts.add(int(attempt))
                        saw_attempt_field = True
                    except (TypeError, ValueError):
                        continue
        except OSError as exc:
            _LOG.warning(
                "backgammon user-event telemetry unavailable path=%s error_class=%s",
                user_events_path,
                exc.__class__.__name__,
            )
            return None

        if malformed_lines > 0:
            _LOG.warning(
                "backgammon user-event telemetry malformed_lines=%d path=%s",
                malformed_lines,
                user_events_path,
            )

        if saw_attempt_field:
            return len(attempts)
        return user_line_count

    def _budget_decision_for_attempt(
        self,
        *,
        run_label: str,
        attempt: int,
        observed_attempt_costs: list[float],
    ) -> str:
        estimate_usd = self._estimate_full_attempt_cost_usd(
            observed_attempt_costs,
            fallback_usd=self._fallback_attempt_estimate_usd,
        )
        checkpoint_path = self._proxy_checkpoint_path()

        if checkpoint_path is None:
            if self.cost_limit_usd is None:
                self._progress(
                    f"PROGRESS run_label={run_label} step=budget-decision attempt={attempt} "
                    f"decision=allow source=unbounded remaining_usd=inf estimate_attempt_usd={estimate_usd:.6f}"
                )
                return "allow"
            self._progress(
                f"PROGRESS run_label={run_label} step=budget-decision attempt={attempt} "
                f"decision=harness_error reason=missing_checkpoint_env env={_PROXY_CHECKPOINT_ENV} "
                f"estimate_attempt_usd={estimate_usd:.6f}"
            )
            return "harness_error"

        try:
            snapshot = self._read_proxy_budget_snapshot(checkpoint_path=checkpoint_path)
        except Exception as exc:  # noqa: BLE001 - classified as harness_error upstream.
            self._progress(
                f"PROGRESS run_label={run_label} step=budget-decision attempt={attempt} "
                f"decision=harness_error reason=checkpoint_read_error checkpoint={checkpoint_path} "
                f"error_fp={self._fingerprint_text(str(exc))}"
            )
            return "harness_error"

        decision = "allow" if snapshot.remaining_usd >= estimate_usd else "budget_stop"
        configured_cap = "none" if self.cost_limit_usd is None else f"{self.cost_limit_usd:.6f}"
        self._progress(
            f"PROGRESS run_label={run_label} step=budget-decision attempt={attempt} decision={decision} "
            f"remaining_usd={snapshot.remaining_usd:.6f} estimate_attempt_usd={estimate_usd:.6f} "
            f"hard_cap_usd={snapshot.hard_cap_usd:.6f} accrued_actual_usd={snapshot.accrued_actual_usd:.6f} "
            f"accrued_derived_usd={snapshot.accrued_derived_usd:.6f} "
            f"committed_unproven_usd={snapshot.committed_unproven_usd:.6f} cost_limit_usd={configured_cap} "
            f"checkpoint={snapshot.checkpoint_path}"
        )
        return decision

    @staticmethod
    def _proxy_checkpoint_path() -> Path | None:
        raw = os.environ.get(_PROXY_CHECKPOINT_ENV, "").strip()
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    @staticmethod
    def _estimate_full_attempt_cost_usd(observed_attempt_costs: list[float], fallback_usd: float = 0.0) -> float:
        observed_max = 0.0
        for value in observed_attempt_costs:
            if isinstance(value, (int, float)):
                observed_max = max(observed_max, float(value))
        return max(observed_max, float(fallback_usd), 0.0)

    def _read_proxy_budget_snapshot(self, *, checkpoint_path: Path) -> _ProxyBudgetSnapshot:
        if not checkpoint_path.is_file():
            raise RuntimeError(f"proxy checkpoint missing: {checkpoint_path}")

        last_error: Exception | None = None
        for _ in range(3):
            try:
                payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError("proxy checkpoint payload is not an object")
                hard_cap_usd = float(payload["hard_cap_usd"])
                accrued_actual_usd = float(payload["accrued_actual_usd"])
                accrued_derived_usd = float(payload.get("accrued_derived_usd", 0.0))
                committed_unproven_usd = float(payload["committed_unproven_usd"])
                remaining_usd = (
                    hard_cap_usd - accrued_actual_usd - accrued_derived_usd - committed_unproven_usd
                )
                return _ProxyBudgetSnapshot(
                    hard_cap_usd=hard_cap_usd,
                    accrued_actual_usd=accrued_actual_usd,
                    accrued_derived_usd=accrued_derived_usd,
                    committed_unproven_usd=committed_unproven_usd,
                    remaining_usd=remaining_usd,
                    checkpoint_path=str(checkpoint_path),
                )
            except Exception as exc:  # noqa: BLE001 - retries for concurrent writes.
                last_error = exc
                time.sleep(0.1)

        raise RuntimeError(
            f"failed reading proxy checkpoint {checkpoint_path}: {last_error}"
        )

    @staticmethod
    def _line_indicates_budget_stop(text: str) -> bool:
        lower = str(text).lower()
        if "budget_exceeded" in lower or "insufficient_quota" in lower:
            return True
        if "statuscode\":402" in lower or "status code: 402" in lower or "status=402" in lower:
            return True
        if "reservation would exceed hard cap" in lower:
            return True
        return False

    @staticmethod
    def _external_signal_from_exit_code(exit_code: int | None) -> str | None:
        if exit_code is None:
            return None
        if exit_code < 128:
            return None

        signal_number = exit_code - 128
        if signal_number < 1:
            return None

        try:
            return signal.Signals(signal_number).name
        except ValueError:
            return f"SIGNAL_{signal_number}"

    def _detect_stream_incomplete(self, events_path: Path) -> bool:
        """Return True if the events file carries a transport-death signature.

        Two live signatures of the same upstream condition, from two observation
        points: a ``step_finish`` with ``reason`` in
        TRUNCATED_STEP_FINISH_REASONS, or an ``error`` event whose payload
        matches a transport/guard signature (``relay: stream incomplete (...)``
        is what the local relay actually emits; the step_finish form has zero
        occurrences in retained artifacts but is what the D-EXIT1-TERMINAL path
        was built against).
        """
        try:
            with events_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type")
                    if event_type == "step_finish":
                        part = event.get("part") if isinstance(event.get("part"), dict) else {}
                        if part.get("reason") in TRUNCATED_STEP_FINISH_REASONS:
                            return True
                        continue
                    if event_type == "error":
                        if self._budget_stop_signature_from_event(event) is not None:
                            continue
                        if self._classify_transport_error(event) is not None:
                            return True
        except OSError:
            pass
        return False

    @staticmethod
    def _classify_transport_error(event: dict[str, Any]) -> str | None:
        """Classify a non-budget ``error`` event's transport/guard signature.

        Returns a stable reason code (``loop_guard``, ``stream_incomplete``,
        ``idle_timeout``, ``provider_error``, …) or ``generic_error`` when the
        payload matches no known signature. Never returns None: every non-budget
        error event terminates the in-flight turn and must be recorded.
        """
        error_block = event.get("error") if isinstance(event.get("error"), dict) else {}
        data = error_block.get("data") if isinstance(error_block.get("data"), dict) else {}
        message = str(data.get("message", ""))
        haystack = message.lower()
        if any(sig in haystack for sig in LOOP_GUARD_SIGNATURES):
            return "loop_guard"
        for reason_code, signature in _TRANSPORT_ERROR_SIGNATURES:
            if signature in haystack:
                return reason_code
        if message.strip():
            return "generic_error"
        return None

    @staticmethod
    def _step_wall_seconds(open_ts_ms: Any, close_ts_ms: Any) -> float | None:
        """Wall-clock of one turn from event timestamps (ms epoch)."""
        try:
            if open_ts_ms is None or close_ts_ms is None:
                return None
            delta = (float(close_ts_ms) - float(open_ts_ms)) / 1000.0
        except (TypeError, ValueError):
            return None
        return delta if delta >= 0 else None

    @staticmethod
    def _append_turn_anomaly(
        state: dict[str, Any],
        *,
        phase: str,
        turn_index: int,
        terminal: str,
        reason: str,
        tool_uses: int,
        file_writes: int,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        cost_usd: float,
        tokens_unmetered: bool,
        wall_seconds: float | None,
        session_id: Any,
    ) -> None:
        """Append one turn_terminal anomaly record to the invocation ledger.

        Caller must hold ``state_lock``. ``retried`` starts False; a later
        ``step_start`` (client auto-retry) flips it via
        ``_mark_anomaly_retried``.
        """
        record = {
            "schema_version": 1,
            "phase": str(phase),
            "turn_index": int(turn_index),
            "terminal": str(terminal),
            "reason": str(reason),
            "tool_uses": int(tool_uses),
            "file_writes": int(file_writes),
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "reasoning_tokens": int(reasoning_tokens),
            "cost_usd": float(cost_usd),
            "tokens_unmetered": bool(tokens_unmetered),
            "wall_seconds": wall_seconds,
            "retried": False,
            "retry_kind": None,
            "session_id": session_id if isinstance(session_id, str) else None,
        }
        state["turn_anomalies"].append(record)
        state["unretried_anomaly"] = len(state["turn_anomalies"]) - 1
        if tokens_unmetered:
            state["unmetered_turns"] = int(state.get("unmetered_turns", 0) or 0) + 1
            if wall_seconds is not None:
                state["unmetered_turn_wall_s"] = float(state.get("unmetered_turn_wall_s", 0.0)) + wall_seconds

    @staticmethod
    def _mark_anomaly_retried(state: dict[str, Any], *, retry_kind: str) -> None:
        """Mark the latest unretried anomaly as retried. Caller holds the lock."""
        index = state.get("unretried_anomaly")
        if index is None:
            return
        anomalies = state.get("turn_anomalies") or []
        if 0 <= int(index) < len(anomalies):
            anomalies[int(index)]["retried"] = True
            anomalies[int(index)]["retry_kind"] = str(retry_kind)
        state["unretried_anomaly"] = None

    def _capture_truncation_evidence(
        self,
        *,
        state: dict[str, Any],
        run_label: str,
        phase: str,
        terminal: str,
        reason: str,
        wall_seconds: float | None,
        finish_reason: Any = None,
        evidence_path: Path,
    ) -> None:
        """Build + persist one WO-WATCH-1E evidence record from ``state``.

        Caller must hold ``state_lock`` so the counters read a consistent
        snapshot. Best-effort: never raises, never affects scoring or metering.
        READ-ONLY against the proxy — records correlation fields only.
        """
        try:
            record = _build_truncation_evidence(
                attempt_id=state.get("attempt_id"),
                run_label=run_label,
                phase=phase,
                terminal=terminal,
                reason=reason,
                ts_start_epoch_ms=state.get("ts_start_epoch_ms"),
                ts_end_epoch_ms=int(time.time() * 1000),
                wall_seconds=wall_seconds,
                session_id=state.get("session_id"),
                received_bytes=state.get("bytes_read"),
                received_lines=state.get("lines_read"),
                last_event_type=state.get("last_event_type"),
                last_event_ts=state.get("last_event_ts"),
                finish_reason=finish_reason,
                output_tokens_received=self._to_int(state.get("sum_output", 0)),
                input_tokens_received=self._to_int(state.get("max_input", 0)),
                reasoning_tokens_received=self._to_int(state.get("sum_reasoning", 0)),
                truncations_seen=self._to_int(state.get("truncations", 0)),
            )
            self._write_truncation_evidence(record=record, evidence_path=evidence_path)
        except Exception as exc:  # noqa: BLE001 - evidence capture must never affect scoring.
            _LOG.warning(
                "truncation evidence capture failed run_label=%s phase=%s terminal=%s: %s",
                run_label,
                phase,
                terminal,
                exc,
            )

    @staticmethod
    def _write_truncation_evidence(*, record: dict[str, Any], evidence_path: Path) -> None:
        """Append one evidence record as a JSON line. Lazy: creates on first call."""
        try:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            with evidence_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 - evidence write must never affect scoring.
            _LOG.warning("truncation evidence write failed %s: %s", evidence_path, exc)

    def _budget_stop_signature_from_event(self, event: dict[str, Any]) -> str | None:
        if str(event.get("type", "")).strip().lower() != "error":
            return None
        error_block = event.get("error") if isinstance(event.get("error"), dict) else {}
        data = error_block.get("data") if isinstance(error_block.get("data"), dict) else {}
        status_code = self._to_int(data.get("statusCode"))
        message = str(data.get("message", ""))
        response_body = str(data.get("responseBody", ""))

        error_type = ""
        error_code = ""
        if response_body:
            try:
                body_payload = json.loads(response_body)
            except json.JSONDecodeError:
                body_payload = None
            if isinstance(body_payload, dict):
                body_error = body_payload.get("error") if isinstance(body_payload.get("error"), dict) else {}
                error_type = str(body_error.get("type", "")).strip()
                error_code = str(body_error.get("code", "")).strip()
                if not message:
                    message = str(body_error.get("message", ""))

        haystack = " ".join((message, response_body, error_type, error_code)).lower()
        if status_code == 402 or "budget_exceeded" in haystack or "insufficient_quota" in haystack:
            return (
                f"status_code={status_code or 'none'} "
                f"error_type={error_type or 'none'} "
                f"error_code={error_code or 'none'} "
                f"message_fp={self._fingerprint_text(message)} "
                f"body_fp={self._fingerprint_text(response_body)}"
            )
        return None

    @staticmethod
    def _fingerprint_text(text: str) -> str:
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def _model_id_from_selector(model: str) -> str:
        provider_id, sep, model_id = str(model).partition("/")
        if sep and model_id:
            return model_id
        return provider_id

    @classmethod
    def _pricing_row_for_model(cls, model: str) -> dict[str, float] | None:
        selector = str(model)
        return _MODEL_PRICING_USD_PER_1M.get(selector) or _MODEL_PRICING_USD_PER_1M.get(
            cls._model_id_from_selector(selector)
        )

    @classmethod
    def _resolve_output_price_per_1m(
        cls,
        *,
        model: str,
        explicit_output_price_per_1m: float | None,
    ) -> float:
        if explicit_output_price_per_1m is not None:
            return float(explicit_output_price_per_1m)
        pricing = cls._pricing_row_for_model(model)
        if pricing is None:
            model_id = cls._model_id_from_selector(model)
            raise ValueError(
                "missing authoritative output pricing for "
                f"model_id={model_id!r}; set output_price_per_1m override to run with cost_limit_usd"
            )
        return float(pricing["output"])

    @classmethod
    def _resolve_cache_write_price_per_1m(
        cls,
        *,
        model: str,
        fallback_price_per_1m: float,
    ) -> float:
        pricing = cls._pricing_row_for_model(model)
        if pricing is not None and "cache_write" in pricing:
            return float(pricing["cache_write"])
        return float(fallback_price_per_1m)

    @staticmethod
    def _worst_case_reservation_usd(
        max_steps: int,
        max_output_tokens: int,
        output_price_per_1m: float,
        safety_factor: float,
        cache_write_allowance_usd: float,
    ) -> float:
        output_price_per_token = float(output_price_per_1m) / 1_000_000.0
        return (
            float(max_steps) * float(max_output_tokens) * output_price_per_token * float(safety_factor)
            + float(cache_write_allowance_usd)
        )

    @staticmethod
    def _copy_tree_contents(src_dir: Path, dst_dir: Path) -> None:
        if not src_dir.is_dir():
            raise FileNotFoundError(f"source directory does not exist: {src_dir}")

        dst_dir.mkdir(parents=True, exist_ok=True)
        for item in src_dir.iterdir():
            target = dst_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def _progress(self, message: str) -> None:
        self._progress_cb(message)
        if self.logger is None:
            return

        info = getattr(self.logger, "info", None)
        if callable(info):
            info(message)

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            if value is None:
                return 0
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text)
        return out

    @staticmethod
    def _normalize_problems(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                normalized.append({"check": "unknown", "expected": "", "observed": str(item)})
                continue
            normalized.append(
                {
                    "check": str(item.get("check", "unknown")),
                    "expected": str(item.get("expected", "")),
                    "observed": str(item.get("observed", "")),
                }
            )
        return normalized
