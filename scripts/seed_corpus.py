"""Seed a controlled multi-memory corpus into one org, then keep leader clone alive."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from wevibe_bench import recall_gold
from wevibe_bench.benv import load_bench_env
from wevibe_bench.lifecycle.identity import Identity
from wevibe_bench.lifecycle.lconfig import LifecycleConfig
from wevibe_bench.lifecycle.logging_util import run_logger
from wevibe_bench.lifecycle.m2_proof import M2Proof
from wevibe_bench.lifecycle.mcp_process import McpInstance, McpProcessManager
from wevibe_bench.lifecycle.orchestrator import LifecycleOrchestrator
from wevibe_bench.lifecycle.qdrant_probe import find_org_collection, snapshot_counts
from wevibe_bench.preflight import preflight


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required env {name}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_identity(env_name: str) -> Identity:
    seed_hex = _required_env(env_name)
    identity = Identity.from_hex(seed_hex)
    print(f"{env_name} provided seed_fp={identity.seed_fp()} ed_pub_fp={identity.ed_pub_fp()}")
    return identity


def _text_sha256_first8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _load_corpus(path: Path) -> tuple[str, list[dict[str, Any]]]:
    if not path.is_file():
        raise RuntimeError(f"WEVIBE_BENCH_CORPUS_FILE not found: {path}")

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise RuntimeError(f"WEVIBE_BENCH_CORPUS_FILE is empty: {path}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"WEVIBE_BENCH_CORPUS_FILE is invalid JSON: {path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("corpus JSON must decode to an object")

    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise RuntimeError("corpus JSON requires non-empty string field 'topic'")

    raw_memories = payload.get("memories")
    if not isinstance(raw_memories, list) or len(raw_memories) == 0:
        raise RuntimeError("corpus JSON requires non-empty array field 'memories'")

    memories: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_memories, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"corpus memory #{idx} must be an object")

        memory_id = item.get("id")
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise RuntimeError(f"corpus memory #{idx} requires non-empty string 'id'")

        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"corpus memory #{idx} id={memory_id!r} requires non-empty string 'text'")

        raw_keywords = item.get("keywords")
        if not isinstance(raw_keywords, list):
            raise RuntimeError(f"corpus memory #{idx} id={memory_id!r} requires array 'keywords'")

        keywords: list[str] = []
        for keyword in raw_keywords:
            if not isinstance(keyword, str):
                raise RuntimeError(
                    f"corpus memory #{idx} id={memory_id!r} has non-string keyword: {keyword!r}"
                )
            cleaned = keyword.strip()
            if cleaned:
                keywords.append(cleaned)

        if not keywords:
            raise RuntimeError(f"corpus memory #{idx} id={memory_id!r} requires at least one keyword")

        raw_stack_hint = item.get("stack_hint")
        if raw_stack_hint is None:
            stack_hint = None
        elif isinstance(raw_stack_hint, str):
            stack_hint = raw_stack_hint.strip() or None
        else:
            raise RuntimeError(f"corpus memory #{idx} id={memory_id!r} has invalid stack_hint type")

        memories.append(
            {
                "id": memory_id.strip(),
                "memory": {
                    "text": text.strip(),
                    "keywords": keywords,
                    "stack_hint": stack_hint,
                },
            }
        )

    return topic.strip(), memories


def _org_count(snapshot: dict[str, int], collection: str | None) -> int:
    if not collection:
        return 0
    return int(snapshot.get(collection, 0))


_DEFAULT_SEED_CHECKPOINT = str(Path(os.environ.get("WEVIBE_BENCH_RUNS_DIR", str(Path(__file__).resolve().parents[1] / "runs"))).expanduser() / "swecb-seed-checkpoint.json")
_RESUME_ENV_EXPORTS = (
    "WEVIBE_BENCH_LEADER_SEED_HEX",
    "WEVIBE_BENCH_CONTRIB_SEED_HEX",
    "WEVIBE_BENCH_CORPUS_FILE",
    "WEVIBE_BENCH_LEADER_WALLET",
    "WEVIBE_BENCH_BUILD_DIST",
    "WEVIBE_BENCH_WEVIBE_ROOT",
    "WEVIBE_BENCH_LEADER_KEYSTORE",
    "WEVIBE_BENCH_CONTRIB_KEYSTORE",
    "WEVIBE_BENCH_QDRANT_URL",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _checkpoint_path() -> Path:
    raw = os.environ.get("WEVIBE_BENCH_SEED_CHECKPOINT", _DEFAULT_SEED_CHECKPOINT).strip()
    if not raw:
        raw = _DEFAULT_SEED_CHECKPOINT
    return Path(raw).expanduser()


def _new_checkpoint(topic: str, total: int) -> dict[str, Any]:
    ts = _utc_now_iso()
    return {
        "org_id": "",
        "topic": topic,
        "total": total,
        "committed": [],
        "created_at": ts,
        "updated_at": ts,
    }


def _load_checkpoint(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None

    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise RuntimeError(f"seed checkpoint exists but is empty: {path}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"seed checkpoint is invalid JSON: {path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"seed checkpoint must decode to an object: {path}")
    return payload


def _validate_checkpoint(
    checkpoint: dict[str, Any],
    *,
    checkpoint_path: Path,
    topic: str,
    total: int,
    corpus_ids: set[str],
) -> None:
    checkpoint_topic = checkpoint.get("topic")
    if not isinstance(checkpoint_topic, str) or not checkpoint_topic.strip():
        raise RuntimeError(f"seed checkpoint missing non-empty topic: {checkpoint_path}")
    if checkpoint_topic.strip() != topic:
        raise RuntimeError(
            f"seed checkpoint topic mismatch at {checkpoint_path}: "
            f"checkpoint={checkpoint_topic!r} corpus={topic!r}"
        )

    checkpoint_total = checkpoint.get("total")
    if not isinstance(checkpoint_total, int) or checkpoint_total <= 0:
        raise RuntimeError(f"seed checkpoint total must be positive integer: {checkpoint_path}")
    if checkpoint_total != total:
        raise RuntimeError(
            f"seed checkpoint total mismatch at {checkpoint_path}: "
            f"checkpoint={checkpoint_total} corpus={total}"
        )

    committed_raw = checkpoint.get("committed")
    if not isinstance(committed_raw, list):
        raise RuntimeError(f"seed checkpoint committed must be an array: {checkpoint_path}")

    committed_clean: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in committed_raw:
        if not isinstance(entry, dict):
            raise RuntimeError(f"seed checkpoint committed entry must be object: {checkpoint_path}")

        idx = entry.get("idx")
        if not isinstance(idx, int) or idx < 1 or idx > total:
            raise RuntimeError(f"seed checkpoint committed entry has invalid idx: {entry!r}")

        memory_id = entry.get("id")
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise RuntimeError(f"seed checkpoint committed entry missing id: {entry!r}")
        memory_id = memory_id.strip()

        submission_hash = entry.get("submission_hash")
        if not isinstance(submission_hash, str) or not submission_hash.strip():
            raise RuntimeError(f"seed checkpoint committed entry missing submission_hash: {entry!r}")
        submission_hash = submission_hash.strip()

        if memory_id not in corpus_ids:
            raise RuntimeError(
                f"seed checkpoint committed id={memory_id!r} not present in corpus: {checkpoint_path}"
            )
        if memory_id in seen_ids:
            raise RuntimeError(f"seed checkpoint duplicates committed id={memory_id!r}: {checkpoint_path}")

        seen_ids.add(memory_id)
        committed_clean.append({
            "idx": idx,
            "id": memory_id,
            "submission_hash": submission_hash,
        })

    committed_clean.sort(key=lambda item: (int(item["idx"]), str(item["id"])))
    checkpoint["committed"] = committed_clean

    org_id_raw = checkpoint.get("org_id")
    if org_id_raw is None:
        checkpoint["org_id"] = ""
    elif not isinstance(org_id_raw, str):
        raise RuntimeError(f"seed checkpoint org_id must be string when present: {checkpoint_path}")
    else:
        checkpoint["org_id"] = org_id_raw.strip()

    checkpoint["topic"] = topic
    checkpoint["total"] = total
    if not isinstance(checkpoint.get("created_at"), str) or not str(checkpoint.get("created_at", "")).strip():
        checkpoint["created_at"] = _utc_now_iso()
    if not isinstance(checkpoint.get("updated_at"), str) or not str(checkpoint.get("updated_at", "")).strip():
        checkpoint["updated_at"] = str(checkpoint["created_at"])


def _save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    now = _utc_now_iso()
    if not isinstance(checkpoint.get("created_at"), str) or not str(checkpoint.get("created_at", "")).strip():
        checkpoint["created_at"] = now
    checkpoint["updated_at"] = now

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_path = parent / f".{path.name}.tmp-{os.getpid()}"
    payload = json.dumps(checkpoint, sort_keys=True, indent=2)
    tmp_path.write_text(f"{payload}\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _committed_ids(checkpoint: dict[str, Any]) -> set[str]:
    committed = checkpoint.get("committed")
    if not isinstance(committed, list):
        return set()

    out: set[str] = set()
    for entry in committed:
        if not isinstance(entry, dict):
            continue
        memory_id = entry.get("id")
        if isinstance(memory_id, str) and memory_id.strip():
            out.add(memory_id.strip())
    return out


def _submission_hashes_from_checkpoint(checkpoint: dict[str, Any]) -> list[str]:
    committed = checkpoint.get("committed")
    if not isinstance(committed, list):
        return []

    indexed_hashes: list[tuple[int, str]] = []
    for entry in committed:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("idx")
        submission_hash = entry.get("submission_hash")
        if isinstance(idx, int) and isinstance(submission_hash, str) and submission_hash.strip():
            indexed_hashes.append((idx, submission_hash.strip()))
    indexed_hashes.sort(key=lambda pair: pair[0])
    return [submission_hash for _, submission_hash in indexed_hashes]


def _resume_command(checkpoint_path: Path) -> str:
    exports: list[str] = []
    for name in _RESUME_ENV_EXPORTS:
        value = os.environ.get(name)
        if value is None:
            continue
        value = value.strip()
        if not value:
            continue
        exports.append(f"{name}={shlex.quote(value)}")
    exports.append("WEVIBE_BENCH_SEED_RESUME=1")
    exports.append(f"WEVIBE_BENCH_SEED_CHECKPOINT={shlex.quote(str(checkpoint_path))}")
    script_path = Path(__file__).resolve()
    exports.append(f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))}")
    return " ".join(exports)


def _pid_listening_on_port(port: int) -> int | None:
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    if not isinstance(result.stdout, str) or not result.stdout.strip():
        return None

    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate.isdigit():
            return int(candidate)
    return None


def _bring_up_for_resume(
    *,
    orchestrator: LifecycleOrchestrator,
    procman: McpProcessManager,
    cfg: LifecycleConfig,
    leader: Identity,
    contributor: Identity,
    leader_keystore: str,
    contributor_keystore: str,
    leader_wallet: str,
    build: bool,
    logger: Any,
) -> tuple[McpInstance, McpInstance, bool]:
    if build:
        procman.build_dist()

    leader_port = orchestrator._port_from_url(cfg.leader_mcp_url)
    contributor_port = orchestrator._port_from_url(cfg.contributor_mcp_url)

    leader_instance: McpInstance | None = None
    contributor_instance: McpInstance | None = None
    spawned_leader_instance: McpInstance | None = None
    leader_reused = False

    existing_leader_pid = _pid_listening_on_port(leader_port)
    if existing_leader_pid is not None:
        candidate = McpInstance(
            name="leader",
            port=leader_port,
            seed_hex=leader.seed_hex,
            keystore_path=leader_keystore,
            log_path=f"<reused-port-{leader_port}>",
            pid=existing_leader_pid,
            url=cfg.leader_mcp_url,
        )
        if procman.wait_healthy(candidate, timeout_s=3):
            leader_instance = candidate
            leader_reused = True
            reuse_msg = (
                f"[seed] resume: reusing healthy leader on port={leader_port} pid={existing_leader_pid}"
            )
            logger.info(reuse_msg)
            print(reuse_msg)
        else:
            logger.warning(
                "op=seed.corpus.resume.leader_unhealthy port=%s pid=%s action=respawn",
                leader_port,
                existing_leader_pid,
            )
    else:
        logger.info("op=seed.corpus.resume.leader_absent port=%s action=spawn", leader_port)

    try:
        with orchestrator._bench_endpoint_flag():
            if leader_instance is None:
                spawned_leader_instance = procman.spawn(
                    name="leader",
                    port=leader_port,
                    seed_hex=leader.seed_hex,
                    keystore_path=leader_keystore,
                    leader_wallet=leader_wallet,
                )
                leader_instance = spawned_leader_instance
            contributor_instance = procman.spawn(
                name="contributor",
                port=contributor_port,
                seed_hex=contributor.seed_hex,
                keystore_path=contributor_keystore,
            )

        if not procman.wait_healthy(leader_instance):
            raise RuntimeError("leader MCP failed health check during resume bring-up")
        if not procman.wait_healthy(contributor_instance):
            raise RuntimeError("contributor MCP failed health check during resume bring-up")
    except Exception:
        if contributor_instance is not None:
            procman.stop(contributor_instance)
        if spawned_leader_instance is not None:
            procman.stop(spawned_leader_instance)
        raise

    setattr(orchestrator, "_leader_instance", leader_instance)
    setattr(orchestrator, "_contributor_instance", contributor_instance)

    contributor_msg = (
        f"[seed] resume: contributor respawned on port={contributor_port} pid={contributor_instance.pid}"
    )
    logger.info(contributor_msg)
    print(contributor_msg)

    return leader_instance, contributor_instance, leader_reused


def main() -> int:
    load_bench_env()
    leader = _load_identity("WEVIBE_BENCH_LEADER_SEED_HEX")
    contributor = _load_identity("WEVIBE_BENCH_CONTRIB_SEED_HEX")

    corpus_path = Path(_required_env("WEVIBE_BENCH_CORPUS_FILE")).expanduser()
    topic, corpus_rows = _load_corpus(corpus_path)
    total_memories = len(corpus_rows)
    corpus_ids = {str(row["id"]) for row in corpus_rows}

    resume_requested = _bool_env("WEVIBE_BENCH_SEED_RESUME")
    checkpoint_path = _checkpoint_path()
    resume_cmd = _resume_command(checkpoint_path)
    build_dist = _bool_env("WEVIBE_BENCH_BUILD_DIST")

    cfg = LifecycleConfig(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4551",
    )
    # HUB-ONLY preflight: the hub must be up before we bring up MCPs + seed.
    # (mcp_recall_url=None: seed_corpus starts its own leader/contributor MCPs.)
    preflight(hub_url=cfg.hub_url, mcp_recall_url=None)
    logger = run_logger("seed-corpus", cfg.runs_dir)
    logfile = getattr(logger, "logfile_path", "")

    wevibe_root = os.environ.get(
        "WEVIBE_BENCH_WEVIBE_ROOT",
        str(Path(__file__).resolve().parents[2]),
    )
    leader_keystore = os.environ.get(
        "WEVIBE_BENCH_LEADER_KEYSTORE",
        "/tmp/wevibe-bench-leader-keystore.json",
    )
    contributor_keystore = os.environ.get(
        "WEVIBE_BENCH_CONTRIB_KEYSTORE",
        "/tmp/wevibe-bench-contrib-keystore.json",
    )
    leader_wallet = _required_env("WEVIBE_BENCH_LEADER_WALLET")
    qdrant_url = os.environ.get("WEVIBE_BENCH_QDRANT_URL", "http://127.0.0.1:6333").strip() or "http://127.0.0.1:6333"

    logger.info(
        (
            "op=seed.corpus.start corpus=%s topic=%s memory_count=%s qdrant_url=%s logfile=%s "
            "checkpoint_path=%s resume_requested=%s"
        ),
        corpus_path,
        topic,
        total_memories,
        qdrant_url,
        logfile,
        checkpoint_path,
        resume_requested,
    )

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
    proof = M2Proof(
        cfg=cfg,
        orchestrator=orchestrator,
        leader=leader,
        contributor=contributor,
        logger=logger,
        direct_memory=None,
    )

    leader_instance = None
    contributor_instance = None
    keep_leader_alive = False
    commit_failed = False
    fatal_error: str | None = None
    resumed = False
    checkpoint = _new_checkpoint(topic, total_memories)

    org_id = ""
    qdrant_before_snapshot: dict[str, int] = {}
    qdrant_after_snapshot: dict[str, int] = {}
    qdrant_before_collection: str | None = None
    qdrant_after_collection: str | None = None
    qdrant_before_count = 0
    qdrant_after_count = 0

    deliveries: list[dict[str, Any]] = []
    fund_script = Path(__file__).resolve().parent / "fund_leader.sh"

    try:
        logger.info(
            "op=seed.corpus.prefund.start script=%s leader_seed_fp=%s leader_signer_dir=%s",
            fund_script,
            leader.seed_fp(),
            cfg.leader_signer_dir,
        )
        subprocess.run(
            ["bash", str(fund_script)],
            env={
                **os.environ,
                "WEVIBE_BENCH_LEADER_SEED_HEX": leader.seed_hex,
                "LEADER_SIGNER_DIR": cfg.leader_signer_dir,
            },
            check=True,
        )
        logger.info(
            "op=seed.corpus.prefund.ok script=%s leader_seed_fp=%s",
            fund_script,
            leader.seed_fp(),
        )

        loaded_checkpoint = _load_checkpoint(checkpoint_path)
        if loaded_checkpoint is not None:
            _validate_checkpoint(
                loaded_checkpoint,
                checkpoint_path=checkpoint_path,
                topic=topic,
                total=total_memories,
                corpus_ids=corpus_ids,
            )

        if resume_requested and loaded_checkpoint is not None and str(loaded_checkpoint.get("org_id", "")).strip():
            checkpoint = loaded_checkpoint
            resumed = True
            org_id = str(checkpoint.get("org_id") or "").strip()
            setattr(orchestrator, "org_id", org_id)
            resume_msg = (
                f"[seed] resume enabled: checkpoint={checkpoint_path} org_id={org_id} "
                f"committed={len(_committed_ids(checkpoint))}/{total_memories}"
            )
            logger.info(resume_msg)
            print(resume_msg)
        else:
            if resume_requested:
                logger.warning(
                    "op=seed.corpus.resume.unavailable checkpoint=%s reason=%s action=fresh_run",
                    checkpoint_path,
                    "missing" if loaded_checkpoint is None else "org_id_missing",
                )
            checkpoint = _new_checkpoint(topic, total_memories)

        if resumed:
            leader_instance, contributor_instance, _ = _bring_up_for_resume(
                orchestrator=orchestrator,
                procman=procman,
                cfg=cfg,
                leader=leader,
                contributor=contributor,
                leader_keystore=leader_keystore,
                contributor_keystore=contributor_keystore,
                leader_wallet=leader_wallet,
                build=build_dist,
                logger=logger,
            )
        else:
            leader_instance, contributor_instance = orchestrator.bring_up(build=build_dist)
            m1 = orchestrator.run_m1()
            logger.info("op=seed.corpus.m1 org_id=%s", m1.get("org_id"))
            org_id = orchestrator.org_id or ""
            if not org_id:
                raise RuntimeError("orchestrator.run_m1 completed without org_id")

            checkpoint["org_id"] = org_id
            _save_checkpoint(checkpoint_path, checkpoint)
            checkpoint_msg = (
                f"[seed] checkpoint write reason=m1_org checkpoint={checkpoint_path} "
                f"org_id={org_id} committed={len(_committed_ids(checkpoint))}/{total_memories}"
            )
            logger.info(checkpoint_msg)
            print(checkpoint_msg)

        keep_leader_alive = True

        qdrant_before_snapshot = snapshot_counts(qdrant_url)
        qdrant_before_collection = find_org_collection(qdrant_url, org_id)
        qdrant_before_count = _org_count(qdrant_before_snapshot, qdrant_before_collection)

        committed_ids = _committed_ids(checkpoint)
        if resumed and committed_ids:
            logger.info(
                "op=seed.corpus.resume.skipset checkpoint=%s committed=%s total=%s",
                checkpoint_path,
                len(committed_ids),
                total_memories,
            )

        for idx, row in enumerate(corpus_rows, start=1):
            memory_id = str(row["id"])

            if memory_id in committed_ids:
                skip_msg = (
                    f"[seed] memory {idx}/{total_memories} id={memory_id} "
                    "already committed in checkpoint -> skipping"
                )
                logger.info(skip_msg)
                print(skip_msg)
                continue

            memory = dict(row["memory"])
            text = str(memory["text"])
            keywords = list(memory["keywords"])
            text_fp = _text_sha256_first8(text)
            text_size = len(text)

            submit_msg = (
                f"[seed] memory {idx}/{total_memories} id={memory_id} "
                f"text_sha256_first8={text_fp} text_size={text_size} keywords={len(keywords)} -> submitting"
            )
            logger.info(submit_msg)
            print(submit_msg)

            try:
                submission_hash = proof.submit_memory(org_id, memory)
                proof.leader_verify_and_commit(org_id, submission_hash, keywords)
            except Exception as exc:
                commit_failed = True
                failure = (
                    f"memory {idx}/{total_memories} id={memory_id} commit failed after "
                    f"{len(committed_ids)} committed checkpoint entries: {exc}"
                )
                logger.exception("[seed] ERROR %s", failure)
                print(f"[seed] ERROR {failure}")

                try:
                    _save_checkpoint(checkpoint_path, checkpoint)
                    logger.info(
                        "op=seed.corpus.checkpoint.write reason=failure checkpoint=%s committed=%s total=%s",
                        checkpoint_path,
                        len(_committed_ids(checkpoint)),
                        total_memories,
                    )
                except Exception as checkpoint_exc:
                    logger.exception("op=seed.corpus.checkpoint.write_failed err=%s", checkpoint_exc)

                if resumed:
                    resume_msg = f"[seed] RESUME_COMMAND {resume_cmd}"
                    logger.error(resume_msg)
                    print(resume_msg)
                raise RuntimeError(failure) from exc

            checkpoint_committed = checkpoint.get("committed")
            if not isinstance(checkpoint_committed, list):
                raise RuntimeError("checkpoint corrupted in-memory: committed is not a list")
            checkpoint_committed.append(
                {
                    "idx": idx,
                    "id": memory_id,
                    "submission_hash": submission_hash,
                }
            )
            committed_ids.add(memory_id)
            _save_checkpoint(checkpoint_path, checkpoint)

            commit_msg = (
                f"[seed] memory {idx}/{total_memories} id={memory_id} "
                f"text_sha256_first8={text_fp} text_size={text_size} keywords={len(keywords)} "
                f"submission_hash={submission_hash} committed=yes"
            )
            logger.info(commit_msg)
            print(commit_msg)

            checkpoint_msg = (
                f"[seed] checkpoint write reason=post_commit checkpoint={checkpoint_path} org_id={org_id} "
                f"idx={idx} id={memory_id} committed={len(committed_ids)}/{total_memories}"
            )
            logger.info(checkpoint_msg)
            print(checkpoint_msg)

        qdrant_after_snapshot = snapshot_counts(qdrant_url)
        qdrant_after_collection = find_org_collection(qdrant_url, org_id)
        qdrant_after_count = _org_count(qdrant_after_snapshot, qdrant_after_collection)

        committed_rows_for_probe = [row for row in corpus_rows if str(row["id"]) in committed_ids]
        if not committed_rows_for_probe:
            raise RuntimeError("no committed memories available for delivery probe")

        for probe_idx, label in ((0, "first"), (len(committed_rows_for_probe) - 1, "last")):
            probe_row = committed_rows_for_probe[probe_idx]
            fragment = str(probe_row["memory"]["text"])[:64]
            delivery = proof.prove_delivery(org_id, fragment)
            deliveries.append(delivery)
            logger.info(
                "[seed] delivery_probe label=%s id=%s delivery=%s n_memories=%s matched=%s",
                label,
                probe_row["id"],
                delivery.get("delivery"),
                delivery.get("n_memories"),
                delivery.get("matched"),
            )
    except Exception as exc:
        fatal_error = str(exc)
        logger.exception("op=seed.corpus.failed err=%s", fatal_error)

        try:
            if org_id:
                checkpoint["org_id"] = org_id
            _save_checkpoint(checkpoint_path, checkpoint)
            logger.info(
                "op=seed.corpus.checkpoint.write reason=exception checkpoint=%s committed=%s total=%s",
                checkpoint_path,
                len(_committed_ids(checkpoint)),
                total_memories,
            )
        except Exception as checkpoint_exc:
            logger.exception("op=seed.corpus.checkpoint.write_failed err=%s", checkpoint_exc)

        if resumed:
            resume_msg = f"[seed] RESUME_COMMAND {resume_cmd}"
            logger.error(resume_msg)
            print(resume_msg)

        if org_id:
            try:
                qdrant_after_snapshot = snapshot_counts(qdrant_url)
                qdrant_after_collection = find_org_collection(qdrant_url, org_id)
                qdrant_after_count = _org_count(qdrant_after_snapshot, qdrant_after_collection)
            except Exception as after_exc:
                logger.exception("op=seed.corpus.qdrant_after_probe_failed err=%s", after_exc)
    finally:
        if contributor_instance is not None:
            procman.stop(contributor_instance)
        if leader_instance is not None and not keep_leader_alive:
            procman.stop(leader_instance)

    submission_hashes = _submission_hashes_from_checkpoint(checkpoint)
    memories_committed = len(_committed_ids(checkpoint))
    count_match = qdrant_after_count == total_memories
    if not count_match:
        mismatch_msg = (
            "[seed] ERROR qdrant points_count mismatch "
            f"org_id={org_id or '<none>'} "
            f"collection={qdrant_after_collection or '<none>'} "
            f"points_count={qdrant_after_count} expected_total={total_memories} "
            f"checkpoint_committed={memories_committed}"
        )
        logger.error(mismatch_msg)
        print(mismatch_msg)

    if leader_instance is not None:
        leader_msg = (
            f"LEADER_CLONE_ALIVE pid={leader_instance.pid} "
            f"port={leader_instance.port} url={leader_instance.url}"
        )
        logger.info(leader_msg)
        print(leader_msg)

    result = {
        "topic": topic,
        "org_id": org_id,
        "memories_committed": memories_committed,
        "qdrant_before_count": qdrant_before_count,
        "qdrant_after_count": qdrant_after_count,
        "count_match": count_match,
        "submission_hashes": submission_hashes,
        "deliveries": deliveries,
        "leader_pid": leader_instance.pid if leader_instance is not None else None,
        "leader_port": leader_instance.port if leader_instance is not None else None,
        "logfile": logfile,
        "resumed": resumed,
        "checkpoint_path": str(checkpoint_path),
    }
    tail = json.dumps(result, sort_keys=True)
    logger.info("SEED_RESULT_JSON %s", tail)
    print(f"SEED_RESULT_JSON {tail}")

    if fatal_error is not None or commit_failed:
        return 1
    if not count_match:
        return 1

    gold_path_raw = os.environ.get("WEVIBE_BENCH_GOLD_FILE", "").strip()
    if gold_path_raw:
        resolve_run_id = org_id.strip() or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        resolve_out_path = Path(cfg.runs_dir) / f"recall-cid-map-{resolve_run_id}.json"
        try:
            resolved_mapping = recall_gold.resolve_from_files(
                gold_path=gold_path_raw,
                corpus_path=corpus_path,
                checkpoint_path=checkpoint_path,
                run_id=resolve_run_id,
                out_path=resolve_out_path,
            )
        except (recall_gold.GoldError, recall_gold.ResolveError) as exc:
            logger.exception(
                (
                    "op=seed.corpus.resolve status=failed gold_path=%s corpus_path=%s "
                    "checkpoint_path=%s err=%s"
                ),
                gold_path_raw,
                corpus_path,
                checkpoint_path,
                exc,
            )
            print(f"[seed] ERROR post-seed slug->CID resolve failed: {exc}")
            return 1

        required_slugs: set[str] = set()
        cases_payload = resolved_mapping.get("cases")
        if isinstance(cases_payload, dict):
            for case_payload in cases_payload.values():
                if not isinstance(case_payload, dict):
                    continue
                expected_slugs = case_payload.get("expected_slugs")
                if not isinstance(expected_slugs, list):
                    continue
                for slug in expected_slugs:
                    if isinstance(slug, str) and slug.strip():
                        required_slugs.add(slug.strip())

        n_required = len(required_slugs)
        logger.info(
            "op=seed.corpus.resolve status=ok n_required=%s n_resolved=%s out_path=%s",
            n_required,
            n_required,
            resolve_out_path,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
