"""Capture OFF backgammon worker reasoning, extract memory, submit+approve to org-0, prove delivery."""

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

from build_distilled_corpus import DEFAULT_OPENCODE_AUTH, _load_openrouter_key
from seed_corpus import _bring_up_for_resume, _load_identity, _required_env
from wevibe_bench.benv import load_bench_env
from wevibe_bench.lifecycle.lconfig import LifecycleConfig
from wevibe_bench.lifecycle.logging_util import run_logger
from wevibe_bench.lifecycle.m2_proof import M2Proof
from wevibe_bench.lifecycle.mcp_process import McpInstance, McpProcessManager
from wevibe_bench.lifecycle.mcp_rest import McpRest
from wevibe_bench.lifecycle.orchestrator import LifecycleOrchestrator
from wevibe_bench.preflight import preflight


DEFAULT_RUN_LABEL = "gemini-off-smoke"
DEFAULT_ORG_ID = "wevibe-org-0"
DEFAULT_RUNS_DIR = "runs/backgammon"
DEFAULT_TRANSCRIPT_CHAR_CAP = 120_000
DEFAULT_SOURCE_CHAR_CAP = 35_000
DEFAULT_EXTRACT_TIMEOUT_S = 900
TASK_HEADER = "Build a Node+TypeScript backgammon game passing the CONTRACT gates"
_KEYWORD_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_first8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n...[truncated]"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)] + marker


def _one_line(value: str) -> str:
    return " ".join(value.split())


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
        transcript: str,
        model: str,
        project_context: dict[str, Any] | None = None,
        org_id: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        num_ctx: int | None = None,
        prompt: str | None = None,
    ) -> str:
        return self._inner.extract(
            transcript=transcript,
            model=model,
            project_context=project_context,
            org_id=org_id,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            num_ctx=num_ctx,
            prompt=prompt if prompt is not None else self._prompt,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture OFF backgammon worker reasoning, distill memory with S/E prompts, "
            "submit+approve into org-0 via bench clone :4550, and prove delivery."
        ),
    )
    parser.add_argument(
        "--run-label",
        default=DEFAULT_RUN_LABEL,
        help="Backgammon run label under --runs-dir (default: gemini-off-smoke).",
    )
    parser.add_argument(
        "--org-id",
        default=DEFAULT_ORG_ID,
        help="Target org id for submit/approve/prove (default: wevibe-org-0).",
    )
    parser.add_argument(
        "--session-model",
        required=True,
        help=(
            "The model slug that PRODUCED the OFF session being distilled — "
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


def _resolve_api_key() -> tuple[str, str]:
    env_key = os.environ.get("WEVIBE_BENCH_API_KEY", "").strip()
    if env_key:
        return env_key, "WEVIBE_BENCH_API_KEY"

    openrouter_env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if openrouter_env:
        return openrouter_env, "OPENROUTER_API_KEY"

    auth_path = Path(DEFAULT_OPENCODE_AUTH).expanduser()
    try:
        return _load_openrouter_key(auth_path), str(auth_path)
    except Exception:
        return "", "none"


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


def _event_files(off_dir: Path) -> list[Path]:
    files = sorted({p.resolve() for p in off_dir.rglob("*.events.jsonl")})
    if files:
        return files

    fallback = off_dir / "worktree.events.jsonl"
    if fallback.is_file():
        return [fallback.resolve()]
    raise RuntimeError(f"no worker events JSONL files found under {off_dir}")


def _assistant_text_chunks(events_file: Path) -> list[str]:
    chunks: list[str] = []
    for line_no, raw in enumerate(events_file.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        entry = _json_line(events_file, line_no, line)
        if entry.get("type") != "text":
            continue
        part = entry.get("part")
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    return chunks


def _source_section(path: Path, *, cap: int) -> str:
    if not path.is_file():
        return f"[missing] {path}"
    body = path.read_text(encoding="utf-8")
    return _truncate(body, cap)


def _build_transcript(
    *,
    off_dir: Path,
    run_label: str,
    s_prompt: str,
    transcript_cap: int,
    source_cap: int,
) -> tuple[str, int, list[Path]]:
    events_files = _event_files(off_dir)

    chunks: list[str] = []
    for events_file in events_files:
        chunks.extend(_assistant_text_chunks(events_file))

    worktree_src = off_dir / "worktree" / "src"
    game_path = worktree_src / "game.ts"
    ai_path = worktree_src / "ai.ts"
    server_path = worktree_src / "server.ts"

    transcript_parts = [
        f"TASK: {TASK_HEADER}",
        f"RUN_LABEL: {run_label}",
        f"OFF_DIR: {off_dir}",
        "",
        "=== S PROMPT (fork reasoning strategy) ===",
        s_prompt.strip(),
        "",
        "=== ASSISTANT TEXT CHUNKS (type:text part.text) ===",
        "\n\n---\n\n".join(chunks) if chunks else "<no assistant text chunks found>",
        "",
        "=== FINAL SOURCE: src/game.ts ===",
        _source_section(game_path, cap=source_cap),
        "",
        "=== FINAL SOURCE: src/ai.ts ===",
        _source_section(ai_path, cap=source_cap),
        "",
        "=== FINAL SOURCE: src/server.ts ===",
        _source_section(server_path, cap=source_cap),
    ]

    transcript = "\n".join(transcript_parts).strip()
    transcript = _truncate(transcript, transcript_cap)
    return transcript, len(chunks), events_files


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


def _memory_fragment(text: str, limit: int = 84) -> str:
    compact = _one_line(text)
    return compact[:limit]


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
        "memory_fragment": "",
        "extract_path": "",
        "logfile": logfile,
    }

    stage = "init"
    extract_path = ""
    leader_instance: McpInstance | None = None
    contributor_instance: McpInstance | None = None
    leader_reused = False
    procman: McpProcessManager | None = None

    try:
        off_dir = (runs_dir / args.run_label / "off").resolve()
        if not off_dir.is_dir():
            raise RuntimeError(f"off run directory not found: {off_dir}")

        repo_root = Path(__file__).resolve().parents[1]
        e_prompt_path = repo_root / "scaffold" / "sxe-candidate" / "E-assembled.txt"
        s_prompt_path = repo_root / "scaffold" / "sxe-candidate" / "S-fork-reasoning.md"
        e_prompt = _load_prompt(e_prompt_path)
        s_prompt = _load_prompt(s_prompt_path)

        stage = "transcript"
        transcript, text_chunks, events_files = _build_transcript(
            off_dir=off_dir,
            run_label=args.run_label,
            s_prompt=s_prompt,
            transcript_cap=DEFAULT_TRANSCRIPT_CHAR_CAP,
            source_cap=DEFAULT_SOURCE_CHAR_CAP,
        )
        progress(
            "transcript built "
            f"events_files={len(events_files)} text_chunks={text_chunks} transcript_chars={len(transcript)}"
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
        leader_keystore = os.environ.get("WEVIBE_BENCH_LEADER_KEYSTORE", "/tmp/wevibe-bench-leader-keystore.json")
        contributor_keystore = os.environ.get(
            "WEVIBE_BENCH_CONTRIB_KEYSTORE",
            "/tmp/wevibe-bench-contrib-keystore.json",
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

        api_key, api_key_source = _resolve_api_key()
        api_key_present = bool(api_key)
        api_key_fp = _sha256_first8(api_key) if api_key_present else "none"
        progress(
            "extract key resolved "
            f"source={api_key_source} present={api_key_present} sha256_first8={api_key_fp}"
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
            "title": f"backgammon-off-{args.run_label}",
            "directory": str(off_dir / "worktree"),
            "stack": ["typescript", "node", "backgammon"],
            "task": TASK_HEADER,
            "strategy_s_prompt": s_prompt,
            "strategy_e_prompt_path": str(e_prompt_path),
            "api_key_source": api_key_source,
        }

        extract_provider = "openrouter"
        if not extract_provider.strip():
            raise RuntimeError("extract provider must be non-empty")
        if extract_model.startswith("openrouter/"):
            raise RuntimeError(
                "extract model slug must be the RAW provider id (e.g. "
                "'anthropic/claude-opus-4.8'), NOT openrouter/-prefixed; "
                f"provider is passed separately: got {extract_model!r}"
            )

        stage = "extract"
        extract_start = time.perf_counter()
        progress(
            "extract start "
            f"session_model={args.session_model} extract_model={extract_model} "
            f"extract_timeout_s={args.extract_timeout} transcript_chars={len(transcript)} "
            f"prompt=E-assembled provider={extract_provider}"
        )

        memory_raw = proof.produce_memory(
            transcript=transcript,
            model=extract_model,
            api_key=api_key,
            project_context=project_context,
            org_id=org_id,
            provider=extract_provider,
            extract_timeout_s=args.extract_timeout,
        )
        extract_path = "extract"

        memory = _normalize_memory(
            memory_raw,
            fallback_keywords=[
                "backgammon",
                "typescript",
                "game_engine",
                "doubling_cube",
                "bear_off",
            ],
        )
        extract_dur_ms = int((time.perf_counter() - extract_start) * 1000)
        progress(
            "extract end "
            f"path={extract_path} dur_ms={extract_dur_ms} text_size={len(memory['text'])} keywords={len(memory['keywords'])}"
        )

        stage = "submit"
        submission_hash = proof.submit_memory(org_id, memory)
        progress(f"submit ok org_id={org_id} submission_hash={submission_hash}")

        stage = "approve"
        verify_payload = proof.leader_verify_and_commit(org_id, submission_hash, list(memory["keywords"]))
        approve_status = _commit_status_label(verify_payload, submission_hash)
        progress(f"approve/commit ok submission_hash={submission_hash} status={approve_status}")

        stage = "prove_delivery"
        memory_fragment = _memory_fragment(str(memory["text"]))
        delivery_payload = proof.prove_delivery(org_id, memory_fragment)
        delivery = str(delivery_payload.get("delivery") or "NO")
        n_memories = int(delivery_payload.get("n_memories") or 0)
        matched = bool(delivery_payload.get("matched"))
        progress(
            "delivery proof "
            f"delivery={delivery} n_memories={n_memories} matched={matched} fragment={memory_fragment!r}"
        )

        status = "ok" if delivery == "YES" else "delivery_no"
        result_payload = {
            "status": status,
            "org_id": org_id,
            "submission_hash": submission_hash,
            "approve_status": approve_status,
            "delivery": delivery,
            "memory_fragment": memory_fragment,
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
