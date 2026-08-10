"""Capture backgammon worker reasoning, extract memory, submit+approve to org-0, prove delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seed_corpus import _bring_up_for_resume, _load_identity, _required_env
from wevibe_bench.benv import load_bench_env
from wevibe_bench.lifecycle.lconfig import (
    DEFAULT_CONTRIB_KEYSTORE_PATH,
    DEFAULT_LEADER_KEYSTORE_PATH,
    LifecycleConfig,
)
from wevibe_bench.lifecycle.logging_util import run_logger
from wevibe_bench.lifecycle.m2_proof import M2Proof
from wevibe_bench.lifecycle.mcp_process import McpInstance, McpProcessManager
from wevibe_bench.lifecycle.mcp_rest import McpRest
from wevibe_bench.lifecycle.orchestrator import LifecycleOrchestrator
from wevibe_bench.preflight import preflight
from wevibe_bench.spend_key import (
    key_fingerprint,
    resolve_local_llm_proxy_api_key,
    resolve_spend_proxy_base_url,
)


DEFAULT_RUN_LABEL = "backgammon-smoke"
# D5a: no silent default org. Extraction requires an explicitly pinned org; wevibe-org-0 is never a valid arm target.
DEFAULT_ORG_ID = ""
DEFAULT_RUNS_DIR = "runs/backgammon"
DEFAULT_EXTRACT_TIMEOUT_S = 900
TASK_HEADER = "Build a Node+TypeScript backgammon game passing the CONTRACT gates"
_KEYWORD_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_first8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _default_extract_timeout_s() -> int:
    raw = os.environ.get("WEVIBE_BENCH_EXTRACT_TIMEOUT_S", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_EXTRACT_TIMEOUT_S
        if value > 0:
            return value
    return DEFAULT_EXTRACT_TIMEOUT_S


class _PromptInjectingRest:
    """Thin wrapper that injects extractor prompt while delegating all other MCP calls."""

    def __init__(self, inner: McpRest, prompt: str) -> None:
        self._inner = inner
        self._prompt = prompt

    def extract(
        self,
        events: list[dict[str, Any]],
        model: str,
        project_context: dict[str, Any] | None = None,
        org_id: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        num_ctx: int | None = None,
        prompt: str | None = None,
        session_id: str | None = None,
    ) -> str:
        return self._inner.extract(
            events=events,
            model=model,
            project_context=project_context,
            org_id=org_id,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            num_ctx=num_ctx,
            prompt=prompt if prompt is not None else self._prompt,
            session_id=session_id,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture backgammon worker reasoning from the selected session mode, distill memory with S/E prompts, "
            "submit+approve into org-0 via bench clone :4550, and prove delivery."
        ),
    )
    parser.add_argument(
        "--run-label",
        default=DEFAULT_RUN_LABEL,
        help="Backgammon run label under --runs-dir (default: backgammon-smoke).",
    )
    parser.add_argument(
        "--source-mode",
        choices=("off", "on"),
        default="off",
        help="Session mode subdirectory under --run-label to extract from (default: off).",
    )
    parser.add_argument(
        "--org-id",
        default=DEFAULT_ORG_ID,
        help='Target org id for submit/approve/prove. MUST be explicitly pinned; "wevibe-org-0" is never a valid arm target.',
    )
    parser.add_argument(
        "--session-model",
        required=True,
        help=(
            "The model slug that PRODUCED the selected session being distilled — "
            "the default self-extraction model E."
        ),
    )
    parser.add_argument(
        "--extract-model",
        default=None,
        help="Optional override of the extraction model; defaults to --session-model (self-extraction).",
    )
    parser.add_argument(
        "--extract-timeout",
        type=int,
        default=_default_extract_timeout_s(),
        help="Client wait budget in seconds for the async extract job (default 900).",
    )
    parser.add_argument(
        "--runs-dir",
        default=DEFAULT_RUNS_DIR,
        help="Root runs directory containing backgammon runs (default: runs/backgammon).",
    )
    return parser


def _resolve_extract_base_url() -> str | None:
    value = resolve_spend_proxy_base_url().strip()
    return value or None


def _resolve_extract_num_ctx() -> int | None:
    raw = os.environ.get("WEVIBE_BENCH_EXTRACT_NUM_CTX", "").strip()
    if not raw:
        return None

    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("WEVIBE_BENCH_EXTRACT_NUM_CTX must be a positive integer") from exc

    if value <= 0:
        raise RuntimeError("WEVIBE_BENCH_EXTRACT_NUM_CTX must be a positive integer")
    return value


def _resolve_extract_api_key() -> tuple[str, str]:
    return resolve_local_llm_proxy_api_key()


def _load_prompt(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"prompt file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise RuntimeError(f"prompt file empty: {path}")
    return raw


def _json_line(path: Path, line_no: int, raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid JSONL object at {path}:{line_no}: expected object")
    return payload


def _event_files(session_dir: Path) -> list[Path]:
    files = sorted({p.resolve() for p in session_dir.rglob("*.events.jsonl")})
    if files:
        return files

    fallback = session_dir / "worktree.events.jsonl"
    if fallback.is_file():
        return [fallback.resolve()]
    raise RuntimeError(f"no worker events JSONL files found under {session_dir}")


def _session_id_counts_from_events(session_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for events_file in _event_files(session_dir):
        try:
            lines = events_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RuntimeError(f"unable to read events file {events_file}: {exc}") from exc

        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            session_id = payload.get("sessionID")
            if not isinstance(session_id, str):
                continue
            normalized = session_id.strip()
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
    return counts


def _session_id_from_events(
    session_dir: Path,
    *,
    session_counts: dict[str, int] | None = None,
) -> str | None:
    counts = session_counts if session_counts is not None else _session_id_counts_from_events(session_dir)
    if not counts:
        return None
    if len(counts) == 1:
        return next(iter(counts))

    chosen_session_id, _ = max(counts.items(), key=lambda item: (item[1], item[0]))
    counts_blob = ", ".join(
        f"{session_id}:{count}"
        for session_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    print(
        f"[{_utc_iso()}] WARNING multiple sessionIDs in events session_dir={session_dir} "
        f"chosen_session_id={chosen_session_id} distinct_sessions={counts_blob}",
        flush=True,
    )
    return chosen_session_id


def _canonical_json(value: Any, *, source: str) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{source} must be JSON-serializable: {exc}") from exc


def _finite_time_ms(value: Any, *, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{source} must be finite numeric ms timestamp, got: {value!r}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise RuntimeError(f"{source} must be finite numeric ms timestamp, got: {value!r}")
    return int(numeric)


def _event_time_ms(entry: dict[str, Any], part: dict[str, Any], *, path: Path, line_no: int) -> int:
    part_time = part.get("time")
    if isinstance(part_time, dict) and "start" in part_time:
        return _finite_time_ms(
            part_time.get("start"),
            source=f"worker event part.time.start at {path}:{line_no}",
        )
    return _finite_time_ms(
        entry.get("timestamp"),
        source=f"worker event timestamp at {path}:{line_no}",
    )


def _user_sidecar_for_events(events_file: Path) -> Path:
    suffix = ".events.jsonl"
    if not events_file.name.endswith(suffix):
        raise RuntimeError(f"events file name must end with {suffix}: {events_file}")
    return events_file.with_name(events_file.name[: -len(suffix)] + ".user-events.jsonl")


def _build_substrate_events(
    *,
    session_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[Path]]:
    events_files = _event_files(session_dir)
    events: list[dict[str, Any]] = []
    seq = 0
    skipped_error_events = 0

    def emit(event: dict[str, Any]) -> None:
        nonlocal seq
        event["seq"] = seq
        seq += 1

        kind = event.get("kind")
        if kind not in {"user", "assistant", "reasoning", "tool", "edit"}:
            raise RuntimeError(f"invalid substrate event kind={kind!r}")

        time_value = event.get("time")
        if isinstance(time_value, bool) or not isinstance(time_value, (int, float)):
            raise RuntimeError(f"invalid substrate event time kind={kind!r} time={time_value!r}")
        if not math.isfinite(float(time_value)):
            raise RuntimeError(f"invalid substrate event time kind={kind!r} time={time_value!r}")

        seq_value = event.get("seq")
        if not isinstance(seq_value, int) or seq_value < 0:
            raise RuntimeError(f"invalid substrate event seq kind={kind!r} seq={seq_value!r}")

        events.append(event)

    for events_file in events_files:
        sidecar_file = _user_sidecar_for_events(events_file)
        if not sidecar_file.is_file():
            raise RuntimeError(
                "missing user sidecar for worker events file: "
                f"events={events_file} sidecar={sidecar_file}"
            )

        for line_no, raw in enumerate(sidecar_file.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            user_entry = _json_line(sidecar_file, line_no, line)
            if user_entry.get("type") != "user":
                raise RuntimeError(
                    f"invalid user sidecar record at {sidecar_file}:{line_no}: "
                    f"type must be 'user', got {user_entry.get('type')!r}"
                )
            user_text = user_entry.get("text")
            if not isinstance(user_text, str):
                raise RuntimeError(
                    f"invalid user sidecar record at {sidecar_file}:{line_no}: text must be string"
                )
            emit(
                {
                    "kind": "user",
                    "time": _finite_time_ms(
                        user_entry.get("timestamp"),
                        source=f"user sidecar timestamp at {sidecar_file}:{line_no}",
                    ),
                    "text": user_text,
                }
            )

        for line_no, raw in enumerate(events_file.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue

            entry = _json_line(events_file, line_no, line)
            event_type = entry.get("type")
            if event_type in {"step_start", "step_finish"}:
                continue
            if event_type == "error":
                skipped_error_events += 1
                continue

            part = entry.get("part")
            if not isinstance(part, dict):
                raise RuntimeError(f"worker event part missing object at {events_file}:{line_no}")

            event_time = _event_time_ms(entry, part, path=events_file, line_no=line_no)

            part_metadata = part.get("metadata")
            openrouter_meta = part_metadata.get("openrouter") if isinstance(part_metadata, dict) else None
            reasoning_details = (
                openrouter_meta.get("reasoning_details") if isinstance(openrouter_meta, dict) else None
            )
            if isinstance(reasoning_details, list):
                for detail in reasoning_details:
                    if not isinstance(detail, dict):
                        continue
                    if detail.get("type") != "reasoning.text":
                        continue
                    reasoning_text = detail.get("text")
                    if isinstance(reasoning_text, str) and reasoning_text.strip():
                        emit(
                            {
                                "kind": "reasoning",
                                "time": event_time,
                                "role": "assistant",
                                "text": reasoning_text,
                            }
                        )

            if event_type == "text":
                assistant_text = part.get("text")
                if isinstance(assistant_text, str) and assistant_text.strip():
                    emit(
                        {
                            "kind": "assistant",
                            "time": event_time,
                            "role": "assistant",
                            "text": assistant_text,
                        }
                    )
                continue

            if event_type != "tool_use":
                continue

            tool_name = part.get("tool")
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise RuntimeError(f"worker tool_use missing tool name at {events_file}:{line_no}")

            state = part.get("state")
            if not isinstance(state, dict):
                raise RuntimeError(f"worker tool_use state missing object at {events_file}:{line_no}")

            status_value = state.get("status")
            state_input = state.get("input")
            if tool_name in {"edit", "write"} and status_value != "error":
                if not isinstance(state_input, dict):
                    raise RuntimeError(f"edit/write tool input missing object at {events_file}:{line_no}")

                file_path = state_input.get("filePath")
                if not isinstance(file_path, str) or not file_path.strip():
                    raise RuntimeError(f"edit/write tool missing filePath at {events_file}:{line_no}")

                detail_key = "newString" if tool_name == "edit" else "content"
                detail = state_input.get(detail_key)
                if not isinstance(detail, str):
                    raise RuntimeError(
                        f"edit/write tool missing string {detail_key} at {events_file}:{line_no}"
                    )

                emit(
                    {
                        "kind": "edit",
                        "time": event_time,
                        "file": file_path,
                        "detail": detail,
                    }
                )
                continue

            state_output = state.get("output")
            state_metadata = state.get("metadata")

            tool_event: dict[str, Any] = {
                "kind": "tool",
                "time": event_time,
                "name": tool_name,
                "input": _canonical_json(
                    state_input,
                    source=f"tool input at {events_file}:{line_no}",
                ),
                "output": state_output,
                "exit": state_metadata.get("exit") if isinstance(state_metadata, dict) else None,
                "status": status_value,
            }
            if status_value == "error":
                error_text: str | None = None
                state_error = state.get("error")
                if isinstance(state_error, str) and state_error.strip():
                    error_text = state_error
                elif isinstance(state_output, str) and state_output.strip():
                    error_text = state_output
                elif state_output is not None:
                    error_text = _canonical_json(
                        state_output,
                        source=f"tool output at {events_file}:{line_no}",
                    )
                if error_text is not None:
                    tool_event["error"] = error_text
            emit(tool_event)

    kind_counts = {kind: 0 for kind in ("user", "assistant", "reasoning", "tool", "edit")}
    for event in events:
        kind = event.get("kind")
        if isinstance(kind, str):
            kind_counts[kind] = kind_counts.get(kind, 0) + 1

    canonical_events_json = _canonical_json(events, source=f"substrate events list under {session_dir}")
    stats = {
        "event_count": len(events),
        "kind_counts": kind_counts,
        "skipped_error_events": skipped_error_events,
        "total_chars": len(canonical_events_json),
        "events_sha256_first8": _sha256_first8(canonical_events_json),
    }
    return events, stats, events_files


def _normalize_keywords(raw: Any) -> list[str]:
    if raw is None:
        return []

    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    seen: set[str] = set()
    normalized: list[str] = []
    for item in items:
        candidate = ""
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, dict):
            keyword = item.get("keyword")
            if isinstance(keyword, str):
                candidate = keyword

        candidate = candidate.strip().lower().replace("-", "_")
        if not candidate:
            continue
        if not _KEYWORD_RE.fullmatch(candidate):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
        if len(normalized) >= 20:
            break
    return normalized


def _normalize_stack_hint(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",") if part.strip()]
        return values or None
    if isinstance(raw, list):
        values = [str(part).strip() for part in raw if str(part).strip()]
        return values or None
    return None


def _normalize_memory(memory: dict[str, Any], *, fallback_keywords: list[str]) -> dict[str, Any]:
    text_raw = memory.get("text")
    if not isinstance(text_raw, str) or not text_raw.strip():
        raise RuntimeError(f"memory.text missing after extraction/fallback: {memory}")
    text = text_raw.strip()

    keywords = _normalize_keywords(memory.get("keywords"))
    if not keywords:
        keywords = list(fallback_keywords)
    if not keywords:
        raise RuntimeError("memory keywords empty after normalization")

    stack_hint = _normalize_stack_hint(memory.get("stack_hint"))
    return {
        "text": text,
        "keywords": keywords,
        "stack_hint": stack_hint,
        "memory_type": "memory",
    }


def _memory_fragment(text: str) -> str:
    return M2Proof.memory_fragment(text)


def _memory_fingerprint_fields(text: str) -> dict[str, Any]:
    """Content-free fingerprint of a memory's plaintext for logs/results (no plaintext).

    Emits ONLY a sha256 first-8 hex fingerprint + byte size — NEVER the plaintext
    itself (no fragment, no prefix). Used wherever delivery-proof outcome is logged.
    """
    return {"memory_fp": _sha256_first8(text), "text_size": len(text)}


def _commit_status_label(verify_payload: Any, submission_hash: str) -> str:
    if not isinstance(verify_payload, dict):
        return "unknown"

    commit_status = verify_payload.get("commit_status")
    if isinstance(commit_status, dict):
        submissions = commit_status.get("submissions")
        if isinstance(submissions, list):
            for item in submissions:
                if not isinstance(item, dict):
                    continue
                if item.get("submission_hash") != submission_hash:
                    continue
                status = item.get("status")
                if isinstance(status, str) and status.strip():
                    return status.strip()
        for key in ("status", "state", "phase"):
            value = commit_status.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return "committed"


def main() -> int:
    load_bench_env()
    args = _build_arg_parser().parse_args()
    # D5a: extraction requires an explicitly pinned org; fail loudly if none is pinned.
    resolved_org_id = str(args.org_id or "").strip()
    if resolved_org_id == "":
        raise RuntimeError("--org-id MUST be explicitly pinned for this extraction (no silent default); e.g. --org-id wevibe-org-2")
    if resolved_org_id == "wevibe-org-0":
        raise RuntimeError('--org-id="wevibe-org-0" is NEVER a valid arm target; pin an explicit org (e.g. wevibe-org-2)')
    extract_model = args.extract_model or args.session_model

    runs_dir = Path(args.runs_dir).expanduser().resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)

    logger = run_logger("backgammon-sxe", str(runs_dir))
    logfile = str(getattr(logger, "logfile_path", "")).strip()

    def progress(message: str) -> None:
        line = f"[{_utc_iso()}] PROGRESS {message}"
        print(line, flush=True)
        logger.info(line)

    result_payload: dict[str, Any] = {
        "status": "error",
        "org_id": args.org_id,
        "submission_hash": "",
        "approve_status": "",
        "delivery": "NO",
        "delivery_proof": {},
        "memory_fp": "",
        "n_memories": 0,
        "memories": [],
        "extract_path": "",
        "logfile": logfile,
    }

    stage = "init"
    extract_path = ""
    leader_instance: McpInstance | None = None
    contributor_instance: McpInstance | None = None
    leader_reused = False
    procman: McpProcessManager | None = None
    session_dir = (runs_dir / args.run_label / args.source_mode).resolve()
    session_id: str | None = None
    progress(f"source selection source_mode={args.source_mode} session_dir={session_dir}")

    try:
        if not session_dir.is_dir():
            raise RuntimeError(f"session run directory not found: {session_dir}")

        repo_root = Path(__file__).resolve().parents[1]
        e_prompt_path = repo_root / "scaffold" / "sxe-candidate" / "E-assembled.txt"
        s_prompt_path = repo_root / "scaffold" / "sxe-candidate" / "S-fork-reasoning.md"
        e_prompt = _load_prompt(e_prompt_path)
        s_prompt = _load_prompt(s_prompt_path)

        stage = "substrate_events"
        events, event_stats, events_files = _build_substrate_events(session_dir=session_dir)
        session_counts = _session_id_counts_from_events(session_dir)
        session_id = _session_id_from_events(session_dir, session_counts=session_counts)
        progress(
            "session linkage "
            f"source_mode={args.source_mode} distinct_session_count={len(session_counts)} "
            f"chosen_session_id={session_id or 'none'}"
        )
        if args.source_mode == "on" and session_id is None:
            progress(
                "WARNING source_mode=on no sessionID found in events; "
                "injected_memory dedup will be skipped for this run"
            )
        kind_counts = event_stats.get("kind_counts", {})
        progress(
            "substrate events built "
            f"events_files={len(events_files)} event_count={event_stats.get('event_count', len(events))} "
            f"kind_user={kind_counts.get('user', 0)} kind_assistant={kind_counts.get('assistant', 0)} "
            f"kind_reasoning={kind_counts.get('reasoning', 0)} kind_tool={kind_counts.get('tool', 0)} "
            f"kind_edit={kind_counts.get('edit', 0)} "
            f"skipped_error_events={event_stats.get('skipped_error_events', 0)} "
            f"total_chars={event_stats.get('total_chars', 0)} "
            f"events_sha256_first8={event_stats.get('events_sha256_first8', 'none')}"
        )

        stage = "identity"
        leader = _load_identity("WEVIBE_BENCH_LEADER_SEED_HEX")
        contributor = _load_identity("WEVIBE_BENCH_CONTRIB_SEED_HEX")

        cfg = LifecycleConfig(
            leader_mcp_url="http://127.0.0.1:4550",
            contributor_mcp_url="http://127.0.0.1:4551",
        )

        stage = "preflight"
        progress(f"preflight hub start hub_url={cfg.hub_url}")
        preflight(hub_url=cfg.hub_url, mcp_recall_url=None)
        progress("preflight hub ok")

        stage = "orchestrator"
        wevibe_root = os.environ.get("WEVIBE_BENCH_WEVIBE_ROOT", str(Path(__file__).resolve().parents[2]))
        leader_keystore = os.environ.get("WEVIBE_BENCH_LEADER_KEYSTORE", DEFAULT_LEADER_KEYSTORE_PATH)
        contributor_keystore = os.environ.get(
            "WEVIBE_BENCH_CONTRIB_KEYSTORE",
            DEFAULT_CONTRIB_KEYSTORE_PATH,
        )
        leader_wallet = _required_env("WEVIBE_BENCH_LEADER_WALLET")

        procman = McpProcessManager(wevibe_root=wevibe_root, cfg=cfg, logger=logger)
        orchestrator = LifecycleOrchestrator(
            cfg=cfg,
            wevibe_root=wevibe_root,
            leader=leader,
            contributor=contributor,
            leader_keystore=leader_keystore,
            contributor_keystore=contributor_keystore,
            leader_wallet=leader_wallet,
            logger=logger,
            procman=procman,
        )

        progress("bring-up/attach start mode=resume-path build=false")
        leader_instance, contributor_instance, leader_reused = _bring_up_for_resume(
            orchestrator=orchestrator,
            procman=procman,
            cfg=cfg,
            leader=leader,
            contributor=contributor,
            leader_keystore=leader_keystore,
            contributor_keystore=contributor_keystore,
            leader_wallet=leader_wallet,
            build=False,
            logger=logger,
        )
        progress(
            "bring-up/attach ok "
            f"leader_port={leader_instance.port} leader_pid={leader_instance.pid} leader_reused={leader_reused} "
            f"contrib_port={contributor_instance.port} contrib_pid={contributor_instance.pid}"
        )

        stage = "org_resolve"
        progress("run_m1 start (org resolve)")
        m1_result = orchestrator.run_m1()
        resolved_org_id = str(m1_result.get("org_id") or "").strip()
        if not resolved_org_id:
            raise RuntimeError("run_m1 completed without org_id")
        if args.org_id and resolved_org_id != args.org_id:
            raise RuntimeError(
                f"resolved org_id mismatch: run_m1={resolved_org_id} expected={args.org_id}"
            )
        org_id = args.org_id or resolved_org_id
        progress(f"org resolve ok org_id={org_id}")

        extract_base_url = _resolve_extract_base_url()
        extract_num_ctx = _resolve_extract_num_ctx()
        api_key, api_key_source = _resolve_extract_api_key()
        api_key_present = bool(api_key)
        api_key_fp = key_fingerprint(api_key) if api_key_present else "none"
        progress(
            "extract key resolved "
            f"source={api_key_source} present={api_key_present} sha256_first8={api_key_fp}"
        )
        num_ctx_label = str(extract_num_ctx) if extract_num_ctx is not None else "none"
        progress(
            "extract llm route "
            f"provider=local-llm-proxy base_url={extract_base_url} num_ctx={num_ctx_label} "
            f"key_source={api_key_source} key_fp={api_key_fp}"
        )

        def _rest_factory(base_url: str) -> _PromptInjectingRest:
            return _PromptInjectingRest(McpRest(base_url, cfg, logger), e_prompt)

        proof = M2Proof(
            cfg=cfg,
            orchestrator=orchestrator,
            leader=leader,
            contributor=contributor,
            logger=logger,
            mcp_rest_factory=_rest_factory,
        )

        project_context = {
            "title": f"backgammon-{args.source_mode}-{args.run_label}",
            "directory": str(session_dir / "worktree"),
            "stack": ["typescript", "node", "backgammon"],
            "task": TASK_HEADER,
            "strategy_s_prompt": s_prompt,
            "strategy_e_prompt_path": str(e_prompt_path),
            "api_key_source": api_key_source,
        }

        extract_provider = "local-llm-proxy"
        # The opencode SESSION slug is provider-prefixed (e.g. 'local-llm-proxy/wevibe-bench-worker'),
        # but /v1/extract wants the RAW provider id with `provider` passed separately. For
        # self-extraction (extract_model defaults to session_model) normalize by stripping a leading
        # provider prefix that matches extract_provider.
        _provider_prefix = f"{extract_provider}/"
        if extract_model.startswith(_provider_prefix):
            _normalized = extract_model[len(_provider_prefix):]
            progress(f"extract model normalized from={extract_model} to={_normalized} provider={extract_provider}")
            extract_model = _normalized
        if not extract_model.strip():
            raise RuntimeError(f"extract model empty after normalization: provider={extract_provider!r}")

        stage = "extract"
        extract_start = time.perf_counter()
        progress(
            "extract start "
            f"session_model={args.session_model} extract_model={extract_model} "
            f"extract_timeout_s={args.extract_timeout} events={event_stats.get('event_count', len(events))} "
            f"events_sha256_first8={event_stats.get('events_sha256_first8', 'none')} "
            f"prompt=E-assembled provider={extract_provider}"
        )

        memories_raw = proof.produce_memories(
            events=events,
            model=extract_model,
            api_key=api_key,
            project_context=project_context,
            org_id=org_id,
            provider=extract_provider,
            base_url=extract_base_url,
            num_ctx=extract_num_ctx,
            extract_timeout_s=args.extract_timeout,
            session_id=session_id,
        )
        extract_path = "extract"

        fallback_keywords = [
            "backgammon",
            "typescript",
            "game_engine",
            "doubling_cube",
            "bear_off",
        ]
        memories = [
            _normalize_memory(memory_raw, fallback_keywords=fallback_keywords)
            for memory_raw in memories_raw
        ]
        extract_dur_ms = int((time.perf_counter() - extract_start) * 1000)
        total_text_size = sum(len(str(memory["text"])) for memory in memories)
        total_keywords = sum(len(list(memory["keywords"])) for memory in memories)
        progress(
            "extract end "
            f"path={extract_path} dur_ms={extract_dur_ms} n_memories={len(memories)} "
            f"total_text_size={total_text_size} total_keywords={total_keywords}"
        )

        committed_memories: list[dict[str, Any]] = []
        delivery_targets: list[dict[str, str]] = []
        for index, memory in enumerate(memories, start=1):
            stage = f"submit[{index}/{len(memories)}]"
            submission_hash = proof.submit_memory(org_id, memory)

            stage = f"approve[{index}/{len(memories)}]"
            verify_payload = proof.leader_verify_and_commit(
                org_id,
                submission_hash,
                list(memory["keywords"]),
            )
            approve_status = _commit_status_label(verify_payload, submission_hash)

            fp_fields = _memory_fingerprint_fields(str(memory["text"]))
            committed_memories.append(
                {
                    "submission_hash": submission_hash,
                    "approve_status": approve_status,
                    "keywords": list(memory["keywords"]),
                    **fp_fields,
                }
            )
            delivery_targets.append(
                {
                    "fragment": _memory_fragment(str(memory["text"])),
                    "cid": submission_hash,
                }
            )
            progress(
                "memory commit "
                f"idx={index}/{len(memories)} submission_hash={submission_hash} "
                f"status={approve_status} memory_fp={fp_fields['memory_fp']} "
                f"text_size={fp_fields['text_size']} keywords={len(memory['keywords'])}"
            )

        stage = "prove_delivery"
        delivery_payload = proof.prove_delivery(org_id, delivery_targets)
        delivery = str(delivery_payload.get("delivery") or "NO")
        n_memories = int(delivery_payload.get("n_memories") or len(delivery_targets))
        matched = bool(delivery_payload.get("matched"))
        per_memory_delivery_raw = delivery_payload.get("per_memory")
        per_memory_delivery = (
            per_memory_delivery_raw if isinstance(per_memory_delivery_raw, list) else []
        )
        matched_count = sum(
            1
            for item in per_memory_delivery
            if isinstance(item, dict) and bool(item.get("matched"))
        )
        progress(
            "delivery proof "
            f"delivery={delivery} n_memories={n_memories} matched={matched} "
            f"matched_count={matched_count}"
        )
        for index, item in enumerate(per_memory_delivery, start=1):
            if not isinstance(item, dict):
                continue
            progress(
                "delivery probe "
                f"idx={index}/{len(delivery_targets)} fragment_fp={item.get('fragment_fp')} "
                f"matched={bool(item.get('matched'))} delivery_mode={item.get('delivery_mode')}"
            )
            if str(item.get("delivery_mode") or "") == "twin_of_returned":
                suppression = item.get("suppression") if isinstance(item.get("suppression"), dict) else {}
                progress(
                    "delivery twin-delivered "
                    f"idx={index}/{len(delivery_targets)} fragment_fp={item.get('fragment_fp')} "
                    f"cid_fp={item.get('cid')} winner_cid_fp={suppression.get('winner_cid')} "
                    f"dropped_twin_cid_fp={suppression.get('dropped_twin_cid')} "
                    f"score_gap={suppression.get('score_gap')}"
                )

        status = "ok" if delivery == "YES" else "delivery_no"
        primary_memory = committed_memories[0] if committed_memories else {}
        result_payload = {
            "status": status,
            "org_id": org_id,
            # Compatibility-only fields for the historical import-cell schema.
            "submission_hash": str(primary_memory.get("submission_hash") or ""),
            "approve_status": str(primary_memory.get("approve_status") or ""),
            "delivery": delivery,
            "delivery_proof": delivery_payload if isinstance(delivery_payload, dict) else {},
            "memory_fp": str(primary_memory.get("memory_fp") or ""),
            "n_memories": len(committed_memories),
            "memories": [
                {
                    "submission_hash": str(item.get("submission_hash") or ""),
                    "memory_fp": str(item.get("memory_fp") or ""),
                    "text_size": int(item.get("text_size") or 0),
                    "approve_status": str(item.get("approve_status") or ""),
                }
                for item in committed_memories
            ],
            "extract_path": extract_path,
            "logfile": logfile,
        }
        print(
            "BACKGAMMON_SXE_RESULT_JSON "
            + json.dumps(result_payload, separators=(",", ":"), sort_keys=True),
            flush=True,
        )
        return 0 if status == "ok" else 1

    except Exception as exc:
        progress(f"ERROR stage={stage} err={exc}")
        result_payload.update(
            {
                "status": "error",
                "extract_path": extract_path,
                "error": str(exc),
                "stage": stage,
            }
        )
        print(
            "BACKGAMMON_SXE_RESULT_JSON "
            + json.dumps(result_payload, separators=(",", ":"), sort_keys=True),
            flush=True,
        )
        return 1

    finally:
        if procman is not None:
            if contributor_instance is not None:
                procman.stop(contributor_instance)
            if leader_instance is not None and not leader_reused:
                procman.stop(leader_instance)


if __name__ == "__main__":
    raise SystemExit(main())
