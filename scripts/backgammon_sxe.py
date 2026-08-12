"""Capture backgammon worker reasoning, extract memory, submit+approve to org-0, prove delivery."""

from __future__ import annotations

import argparse
import hashlib
import json
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
from wevibe_bench.session_db_integrity import require_sound_session_db
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
        model: str,
        session_db_path: str,
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
            session_db_path=session_db_path,
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

    # ── STRUCTURED STAGE EMITTER ────────────────────────────────────────────
    # The `stage` local below is the extraction pipeline's real state machine,
    # but before this it was only ever PRINTED on the error path
    # (`ERROR stage=<x>`), so a consumer could not tell which stage was running
    # without scraping prose PROGRESS lines and inferring.
    #
    # A UI built on prose-scraping lies the moment a message is reworded. This
    # emits ONE machine-readable line per stage transition, with a stable
    # schema, so the dashboard renders the pipeline from a declared fact rather
    # than an inference. The prose lines are untouched — this is additive.
    #
    # `gated` is a FIRST-CLASS state, distinct from `failed`: the WO-DBVOL-1
    # integrity check refusing a corrupt substrate is the instrument working
    # correctly, and must never render as a crash (nor, ever, as a pass).
    # Stages that already emitted a TERMINAL state (gated/failed). The error
    # handler must not overwrite a `gated` with a `failed`: the distinction is
    # the whole point — a gate refusing a corrupt substrate is the instrument
    # working, and it raises afterwards to void the cell.
    _terminal_stages: set[str] = set()

    def emit_stage(
        name: str,
        state: str,
        *,
        count: int | None = None,
        detail: str | None = None,
    ) -> None:
        if state in {"gated", "failed"}:
            _terminal_stages.add(name)
        payload: dict[str, Any] = {
            "stage": name,
            "state": state,
            "at": time.time(),
        }
        if count is not None:
            payload["count"] = int(count)
        if detail is not None:
            # Bounded: a stage detail is a reason a human reads on a stream,
            # never a stack dump or a transcript.
            payload["detail"] = str(detail)[:400]
        print(
            "BACKGAMMON_SXE_STAGE " + json.dumps(payload, separators=(",", ":"), sort_keys=True),
            flush=True,
        )

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
    emit_stage("init", "running")
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

        stage = "substrate"
        emit_stage("init", "complete")
        emit_stage("substrate", "running")
        session_db_path = session_dir / "session-db" / "opencode.db"
        if not session_db_path.is_file():
            emit_stage(
                "substrate",
                "gated",
                detail=f"session database not found for extraction: {session_db_path}",
            )
            raise RuntimeError(f"session database not found for extraction: {session_db_path}")
        # WO-DBVOL-1: existence is NOT soundness. SQLite corruption is partial —
        # the 2026-08-11 DB answered `count(*)` fine while `PRAGMA quick_check`
        # reported a malformed image — so an unverified substrate silently
        # under-reports memories instead of failing. Fail closed here: a corrupt
        # DB voids the cell rather than producing a plausible wrong number.
        #
        # A corrupt substrate emits `gated`, NOT `failed`: the gate refusing is
        # the instrument behaving correctly, and the UI must show it as a
        # deliberate refusal rather than a crash.
        try:
            integrity = require_sound_session_db(session_db_path)
        except Exception as exc:
            emit_stage("substrate", "gated", detail=str(exc))
            raise
        progress(f"substrate integrity {integrity.summary()}")
        emit_stage("substrate", "complete", detail=integrity.summary())
        session_id = None
        progress(f"substrate source session_db={session_db_path}")

        stage = "identity"
        emit_stage("identity", "running")
        leader = _load_identity("WEVIBE_BENCH_LEADER_SEED_HEX")
        contributor = _load_identity("WEVIBE_BENCH_CONTRIB_SEED_HEX")

        cfg = LifecycleConfig(
            leader_mcp_url="http://127.0.0.1:4550",
            contributor_mcp_url="http://127.0.0.1:4551",
        )

        stage = "preflight"
        emit_stage("identity", "complete")
        emit_stage("preflight", "running")
        progress(f"preflight hub start hub_url={cfg.hub_url}")
        preflight(hub_url=cfg.hub_url, mcp_recall_url=None)
        progress("preflight hub ok")
        emit_stage("preflight", "complete")

        stage = "orchestrator"
        emit_stage("orchestrator", "running")
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
        emit_stage("orchestrator", "complete")
        emit_stage("org_resolve", "running")
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
        emit_stage("org_resolve", "complete", detail=f"org_id={org_id}")

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
        emit_stage("extract", "running", detail=f"model={extract_model}")
        extract_start = time.perf_counter()
        progress(
            "extract start "
            f"session_model={args.session_model} extract_model={extract_model} "
            f"extract_timeout_s={args.extract_timeout} session_db={session_db_path} "
            f"prompt=E-assembled provider={extract_provider}"
        )

        memories_raw = proof.produce_memories(
            session_db_path=str(session_db_path),
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
        # A count of 0 is a MEASURED ZERO — a real result, not absence. The
        # board must render it as such (contract rule 1), which is why the
        # count is always emitted rather than omitted when empty.
        emit_stage("extract", "complete", count=len(memories))

        committed_memories: list[dict[str, Any]] = []
        delivery_targets: list[dict[str, str]] = []
        if memories:
            emit_stage("submit", "running", count=0)
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
        # Submit/approve advance per memory; they are reported as one stage each
        # carrying the count actually completed, so a partial run shows how far
        # it got rather than a bare "failed".
        emit_stage("submit", "complete", count=len(committed_memories))
        emit_stage("approve", "complete", count=len(committed_memories))
        emit_stage("prove_delivery", "running")
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
        # Delivery NO is a real, measured outcome — the stage completed and the
        # answer was negative. It is NOT a stage failure, and must not render as
        # one; the delivery verdict itself carries the meaning.
        emit_stage(
            "prove_delivery",
            "complete",
            count=matched_count,
            detail=f"delivery={delivery} matched={matched_count}/{n_memories}",
        )
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
        # The stage name may carry an index suffix (`submit[3/7]`); the emitted
        # stage id is the base name so it matches the declared stage list the
        # UI renders. A `gated` stage has already emitted its own terminal
        # state above and is not overwritten here.
        _base_stage = str(stage).split("[")[0]
        if _base_stage not in _terminal_stages:
            emit_stage(_base_stage, "failed", detail=str(exc))
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
