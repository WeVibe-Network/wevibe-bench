"""Lifecycle milestone-2 proof driver (submit -> verify/commit -> delivery)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Callable

from .hub_client import HubClient
from .identity import Identity
from .lconfig import LifecycleConfig
from .logging_util import fp, new_trace_id
from .mcp_rest import McpRest
from .qdrant_probe import find_org_collection, snapshot_counts


McpRestFactory = Callable[[str], Any]
SnapshotFn = Callable[[str], dict[str, int]]
FindCollectionFn = Callable[[str, str], str | None]
Runner = Callable[..., subprocess.CompletedProcess[str]]


class M2Proof:
    _KEYWORD_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
    _MEMORY_FRAGMENT_LIMIT = 64

    def __init__(
        self,
        cfg: LifecycleConfig,
        orchestrator: Any,
        leader: Identity,
        contributor: Identity,
        logger: Any,
        *,
        mcp_rest_factory: McpRestFactory | None = None,
        hub_client: HubClient | None = None,
        snapshot_fn: SnapshotFn | None = None,
        find_collection_fn: FindCollectionFn | None = None,
        qdrant_url: str = "http://127.0.0.1:6333",
        sleep_fn: Callable[[float], None] = time.sleep,
        run_cmd: Runner = subprocess.run,
        direct_memory: dict[str, Any] | None = None,
    ) -> None:
        self._cfg = cfg
        self._orchestrator = orchestrator
        self._leader = leader
        self._contributor = contributor
        self._logger = logger
        self._sleep = sleep_fn
        self._qdrant_url = qdrant_url
        self._run_cmd = run_cmd
        self._direct_memory = direct_memory

        self._mcp_rest_factory = mcp_rest_factory or (
            lambda base_url: McpRest(base_url, self._cfg, self._logger)
        )
        self._hub_client = (
            hub_client
            or getattr(orchestrator, "hub_client", None)
            or HubClient(self._cfg, self._logger)
        )
        self._snapshot_fn = snapshot_fn or (lambda url: snapshot_counts(url))
        self._find_collection_fn = find_collection_fn or (
            lambda url, org_id: find_org_collection(url, org_id)
        )

    @property
    def orchestrator(self) -> Any:
        return self._orchestrator

    @staticmethod
    def _sanitize(value: Any) -> str:
        return " ".join(str(value).split())

    def _log(
        self,
        level: str,
        op: str,
        trace: str,
        status: str,
        dur_ms: int,
        **fields: Any,
    ) -> None:
        details = " ".join(
            f"{key}={self._sanitize(value)}"
            for key, value in fields.items()
            if value is not None
        )
        msg = f"op={op} trace={trace}"
        if details:
            msg = f"{msg} {details}"
        msg = f"{msg} status={status} dur_ms={dur_ms}"
        log_fn = getattr(self._logger, level, self._logger.info)
        log_fn(msg)

    @staticmethod
    def _normalize_keywords(raw: Any) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        return []

    @staticmethod
    def _cap_utf8_bytes(text: str, max_bytes: int) -> str:
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text

        truncated = encoded[:max_bytes]
        while truncated:
            try:
                return truncated.decode("utf-8")
            except UnicodeDecodeError as exc:
                truncated = truncated[: exc.start]
        return ""

    @classmethod
    def _extract_candidate_keywords(cls, raw: Any) -> list[str]:
        if isinstance(raw, dict):
            keywords: list[str] = []
            for bucket in ("classified", "suggestions"):
                bucket_items = raw.get(bucket)
                if not isinstance(bucket_items, list):
                    continue
                for item in bucket_items:
                    keyword = item.get("keyword") if isinstance(item, dict) else item
                    if isinstance(keyword, str) and keyword.strip():
                        keywords.append(keyword.strip())
            if keywords:
                return keywords
            return []
        return cls._normalize_keywords(raw)

    @staticmethod
    def _normalize_candidate_stack(raw: Any) -> list[str]:
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw if str(item).strip()]
            return values
        if isinstance(raw, str):
            values = [part.strip() for part in raw.split(",") if part.strip()]
            return values
        return []

    @classmethod
    def _render_atomic_candidate_text(cls, candidate: dict[str, Any]) -> str | None:
        implement = candidate.get("implement")
        if not isinstance(implement, str) or not implement.strip():
            return None

        context_raw = candidate.get("context")
        context = context_raw.strip() if isinstance(context_raw, str) and context_raw.strip() else "unspecified"
        stack_values = cls._normalize_candidate_stack(candidate.get("stack"))

        lines = [
            f"Implement: {implement.strip()}",
            f"Context: {context}",
            f"Stack: {', '.join(stack_values) if stack_values else 'unknown'}",
        ]

        dnd_raw = candidate.get("dnd")
        if isinstance(dnd_raw, str) and dnd_raw.strip():
            lines.append(f"Avoid: {dnd_raw.strip()}")

        return "\n".join(lines)

    @staticmethod
    def _extract_candidate_text(candidate: dict[str, Any]) -> str | None:
        fallback = (
            candidate.get("text")
            or candidate.get("plaintext")
            or candidate.get("memory")
            or candidate.get("content")
        )
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()
        return M2Proof._render_atomic_candidate_text(candidate)

    @staticmethod
    def _normalize_stack_hint(raw: Any) -> list[str] | None:
        # /v1/submit requires stack_hint as an array of strings (or omitted).
        if raw is None:
            return None
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw if str(item).strip()]
            return values or None
        if isinstance(raw, str):
            values = [part.strip() for part in raw.split(",") if part.strip()]
            return values or None
        return None

    @classmethod
    def _build_classified_keywords(cls, keywords: list[str]) -> list[dict[str, Any]]:
        deduped: list[str] = []
        seen: set[str] = set()

        for raw in keywords:
            keyword = str(raw).strip().lower()
            if not keyword or not cls._KEYWORD_RE.fullmatch(keyword):
                continue
            if keyword in seen:
                continue
            seen.add(keyword)
            deduped.append(keyword)
            if len(deduped) >= 20:
                break

        if not deduped:
            raise RuntimeError(
                "submit_keyword_results requires at least one valid classified keyword after filtering"
            )

        n = len(deduped)
        weight = round(1.0 / n, 6)
        weights = [weight] * n
        if n > 1:
            weights[-1] = 1.0 - sum(weights[:-1])

        return [
            {
                "keyword": keyword,
                "weight": value,
                "base_weight": value,
            }
            for keyword, value in zip(deduped, weights)
        ]

    @staticmethod
    def _require_hub_passed_result(payload: Any, submission_hash: str, op_name: str) -> None:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{op_name} payload must be object for {submission_hash}: {payload}")

        results = payload.get("results")
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"{op_name} payload missing results for {submission_hash}: {payload}")

        entry: dict[str, Any] | None = None
        for item in results:
            if not isinstance(item, dict):
                continue
            item_hash = item.get("submission_hash")
            if not isinstance(item_hash, str):
                item_hash = item.get("hash")
            if isinstance(item_hash, str) and item_hash == submission_hash:
                entry = item
                break

        if entry is None and len(results) == 1 and isinstance(results[0], dict):
            entry = results[0]

        if entry is None:
            raise RuntimeError(f"{op_name} payload missing entry for {submission_hash}: {payload}")

        passed = entry.get("passed")
        code = entry.get("code")
        error = entry.get("error")
        if passed is not True or (isinstance(error, str) and error.strip()):
            raise RuntimeError(f"{op_name} failed submission_hash={submission_hash} code={code!r} error={error!r}")

    @staticmethod
    def _parse_last_json_line(stdout: str, command_name: str) -> dict[str, Any]:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError(f"{command_name} returned empty stdout")

        tail = lines[-1]
        try:
            payload = json.loads(tail)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{command_name} last stdout line is not valid JSON: {tail}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError(f"{command_name} last stdout line must decode to object: {tail}")
        return payload

    @staticmethod
    def _candidate_sources(payload: dict[str, Any]) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        queue: list[Any] = [payload]
        while queue:
            current = queue.pop(0)
            if isinstance(current, dict):
                if any(key in current for key in ("implement", "text", "plaintext", "memory", "content")):
                    sources.append(current)
                for key in ("result", "candidate"):
                    value = current.get(key)
                    if isinstance(value, dict):
                        queue.append(value)
                for key in ("candidates", "memories", "items", "results"):
                    value = current.get(key)
                    if isinstance(value, list):
                        queue.extend(value)
        return sources

    def _hop(self, hops: list[str], hop_name: str, fn: Callable[[], Any]) -> Any:
        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        self._log("info", "lifecycle.m2.hop", trace, "ok", 0, hop=hop_name, phase="start")
        try:
            result = fn()
        except Exception as exc:
            dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
            self._log(
                "error",
                "lifecycle.m2.hop",
                trace,
                "err",
                int(dur_ms),
                hop=hop_name,
                err=str(exc),
            )
            hops.append(hop_name)
            raise

        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        self._log("info", "lifecycle.m2.hop", trace, "ok", int(dur_ms), hop=hop_name)
        hops.append(hop_name)
        return result

    def _require_org_id(self) -> str:
        org_id = getattr(self._orchestrator, "org_id", None)
        if isinstance(org_id, str) and org_id:
            return org_id

        last = getattr(self._orchestrator, "last_m1_result", None)
        if isinstance(last, dict) and isinstance(last.get("org_id"), str):
            return str(last["org_id"])
        raise RuntimeError("M2Proof requires an org_id from run_m1 before run()")

    def _contributor_rest(self) -> Any:
        return self._mcp_rest_factory(self._cfg.contributor_mcp_url)

    def _leader_rest(self) -> Any:
        return self._mcp_rest_factory(self._cfg.leader_mcp_url)

    @classmethod
    def memory_fragment(cls, text: str) -> str:
        compact = " ".join(str(text).split())
        return compact[: cls._MEMORY_FRAGMENT_LIMIT]

    def _memory_from_candidate(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        text = self._extract_candidate_text(candidate)
        if text is None:
            return None

        capped_text = self._cap_utf8_bytes(text, 1800).strip()
        if not capped_text:
            return None

        stack_value = candidate.get("stack")
        stack_hint = (
            stack_value
            if isinstance(stack_value, list)
            else candidate.get("stack_hint") or stack_value
        )

        memory_type = candidate.get("memory_type")
        normalized_memory_type = (
            memory_type.strip()
            if isinstance(memory_type, str) and memory_type.strip()
            else "memory"
        )

        return {
            "text": capped_text,
            "keywords": self._extract_candidate_keywords(candidate.get("keywords")),
            "stack_hint": stack_hint,
            "memory_type": normalized_memory_type,
        }

    def produce_memories(
        self,
        session_db_path: str,
        model: str,
        api_key: str,
        project_context: dict[str, Any],
        org_id: str,
        provider: str = "local-llm-proxy",
        base_url: str | None = None,
        num_ctx: int | None = None,
        extract_timeout_s: float = 900,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(self._direct_memory, dict):
            raw_text = self._direct_memory.get("text")
            if isinstance(raw_text, str) and raw_text.strip():
                text = raw_text.strip()
                keywords = self._normalize_keywords(self._direct_memory.get("keywords"))
                self._log(
                    "info",
                    "lifecycle.m2.direct_memory",
                    new_trace_id(),
                    "ok",
                    0,
                    text_size=len(text),
                    keyword_count=len(keywords),
                    memory_fp=fp(text),
                )
                return [
                    {
                        "text": text,
                        "keywords": keywords,
                        "stack_hint": self._direct_memory.get("stack_hint"),
                        "memory_type": str(self._direct_memory.get("memory_type") or "memory"),
                    }
                ]

        hosted_api_key = api_key.strip() if isinstance(api_key, str) else ""
        if not hosted_api_key:
            raise RuntimeError(
                "produce_memory requires non-empty api_key for hosted extract; local fallback is disabled"
            )

        context = dict(project_context)
        context["api_key_present"] = bool(hosted_api_key)

        client = self._contributor_rest()
        job_id = client.extract(
            session_db_path=session_db_path,
            model=model,
            project_context=context,
            org_id=org_id,
            provider=provider,
            api_key=hosted_api_key,
            base_url=base_url,
            num_ctx=num_ctx,
            session_id=session_id,
        )
        self._log("info", "lifecycle.m2.extract_wait", new_trace_id(), "ok", 0, job_id=job_id, timeout_s=extract_timeout_s)
        status = client.wait_extract(job_id, timeout_s=extract_timeout_s)
        if not isinstance(status, dict):
            raise RuntimeError(f"extract status expected object, got: {status}")

        memories: list[dict[str, Any]] = []
        for candidate in self._candidate_sources(status):
            memory = self._memory_from_candidate(candidate)
            if memory is None:
                continue
            memories.append(memory)

        if memories:
            return memories

        if isinstance(status, dict):
            raise RuntimeError(f"extract produced no usable memory candidate (status_keys={sorted(status)})")
        raise RuntimeError("extract produced no usable memory candidate")

    def produce_memory(
        self,
        session_db_path: str,
        model: str,
        api_key: str,
        project_context: dict[str, Any],
        org_id: str,
        provider: str = "local-llm-proxy",
        base_url: str | None = None,
        num_ctx: int | None = None,
        extract_timeout_s: float = 900,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        memories = self.produce_memories(
            session_db_path=session_db_path,
            model=model,
            api_key=api_key,
            project_context=project_context,
            org_id=org_id,
            provider=provider,
            base_url=base_url,
            num_ctx=num_ctx,
            extract_timeout_s=extract_timeout_s,
            session_id=session_id,
        )
        memory = memories[0]
        return {
            "text": memory.get("text"),
            "keywords": memory.get("keywords"),
            "stack_hint": memory.get("stack_hint"),
        }

    def submit_memory(self, org_id: str, memory: dict[str, Any]) -> str:
        text = memory.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"submit_memory requires non-empty memory.text, got: {memory}")

        payload = self._contributor_rest().submit(
            org_id=org_id,
            plaintext=text,
            memory_type=str(memory.get("memory_type") or "memory"),
            epoch_id=memory.get("epoch_id"),
            stack_hint=self._normalize_stack_hint(memory.get("stack_hint")),
            keywords=self._normalize_keywords(memory.get("keywords")),
            mc_version=int(memory.get("mc_version") or self._cfg.mc_version),
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("submission_hash"), str):
            raise RuntimeError(f"submit response missing submission_hash: {payload}")
        return payload["submission_hash"]

    @staticmethod
    def _queue_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("items", "queue", "entries", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _find_submission(items: list[dict[str, Any]], submission_hash: str) -> dict[str, Any] | None:
        for item in items:
            for key in ("submission_hash", "id", "hash"):
                value = item.get(key)
                if isinstance(value, str) and value == submission_hash:
                    return item
        return None

    @staticmethod
    def _is_committed(payload: Any, submission_hash: str | None = None) -> bool:
        _COMMITTED = {"committed", "complete", "completed", "done"}
        if isinstance(payload, dict):
            if payload.get("committed") is True:
                return True
            for key in ("status", "state", "phase"):
                value = payload.get(key)
                if isinstance(value, str) and value.lower() in _COMMITTED:
                    return True
            # commit-status returns the state nested under submissions[]
            submissions = payload.get("submissions")
            if isinstance(submissions, list):
                for item in submissions:
                    if not isinstance(item, dict):
                        continue
                    if submission_hash and item.get("submission_hash") != submission_hash:
                        continue
                    commit_error = item.get("commit_error")
                    if isinstance(commit_error, str) and commit_error.strip():
                        raise RuntimeError(
                            f"commit failed for {item.get('submission_hash')}: {commit_error}"
                        )
                    status = item.get("status")
                    if isinstance(status, str) and status.lower() in _COMMITTED:
                        return True
        return False

    def _commit_batch(
        self,
        org_id: str,
        batch_payload: Any,
        producer_model_id: str,
    ) -> dict[str, Any]:
        if not isinstance(batch_payload, dict):
            raise RuntimeError(f"batch_submit expected object payload, got: {batch_payload}")

        batch = batch_payload.get("batch")
        if not isinstance(batch, list) or len(batch) == 0:
            raise RuntimeError("nothing to commit — submission/verify leg failed upstream")

        trace = new_trace_id()
        t0 = time.perf_counter_ns()
        self._log("info", "lifecycle.m2.commit_batch", trace, "ok", 0, phase="start", org_id=org_id)

        producer_model_id_value = str(producer_model_id or "").strip()
        if not producer_model_id_value:
            raise RuntimeError("producer_model_id is required for commit provenance")

        signer_dir = os.path.expanduser(self._cfg.leader_signer_dir)
        signer_cli = os.path.join(signer_dir, "dist", "cli.js")
        cmd = [
            "node",
            signer_cli,
            "commit-batch",
            "--org-id",
            org_id,
            "--producer-model-id",
            producer_model_id_value,
        ]
        env = dict(os.environ)
        env.update(
            {
                "WEVIBE_IDENTITY_SEED_HEX": self._leader.seed_hex,
                "WEVIBE_CHAIN_RPC": "http://localhost:26657",
            }
        )
        batch_json = json.dumps(batch_payload)

        result = self._run_cmd(
            cmd,
            cwd=signer_dir,
            env=env,
            input=batch_json,
            capture_output=True,
            text=True,
            check=False,
        )

        dur_ms = (time.perf_counter_ns() - t0) // 1_000_000
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            self._log(
                "error",
                "lifecycle.m2.commit_batch",
                trace,
                "err",
                int(dur_ms),
                rc=result.returncode,
                err=stderr,
            )
            raise RuntimeError(
                f"leader-signer commit-batch failed rc={result.returncode}: {stderr or 'unknown error'}"
            )

        response = self._parse_last_json_line(result.stdout or "", "leader-signer commit-batch")
        code = response.get("code")
        if not isinstance(code, int):
            raise RuntimeError(f"leader-signer commit-batch missing integer code: {response}")
        if code != 0:
            raise RuntimeError(f"leader-signer commit-batch returned nonzero code={code}: {response}")

        tx_hash = response.get("tx_hash")
        if not isinstance(tx_hash, str) or not tx_hash:
            raise RuntimeError(f"leader-signer commit-batch missing tx_hash: {response}")

        self._log(
            "info",
            "lifecycle.m2.commit_batch",
            trace,
            "ok",
            int(dur_ms),
            org_id=org_id,
            tx_hash=tx_hash,
            producer_model_id=producer_model_id_value,
            msg_count=response.get("msg_count"),
        )
        return response

    def leader_verify_and_commit(
        self,
        org_id: str,
        submission_hash: str,
        keywords: list[str],
        producer_model_id: str | None = None,
    ) -> dict[str, Any]:
        hops: list[str] = []
        precheck_payload = self._hop(
            hops,
            "commit_precheck",
            lambda: self._hub_client.commit_status(self._leader, org_id),
        )
        if self._is_committed(precheck_payload, submission_hash):
            return {
                "hops": hops,
                "queue_item": None,
                "embed_card": None,
                "submit_keyword_results": None,
                "verify_keywords": None,
                "batch_submit": None,
                "commit_batch": None,
                "commit_status": precheck_payload,
                "already_committed": True,
            }

        queue_payload = self._hop(
            hops,
            "moderation_queue",
            lambda: self._hub_client.moderation_queue(self._leader, org_id),
        )
        queue_items = self._queue_items(queue_payload)
        pending = self._find_submission(queue_items, submission_hash)
        if pending is None:
            raise RuntimeError(
                f"submission {submission_hash} not found in moderation queue (queue_size={len(queue_items)})"
            )
        for field in ("ciphertext_hex", "wrapped_dek_mod"):
            value = pending.get(field)
            if not isinstance(value, str) or not value:
                raise RuntimeError(f"moderation queue item missing {field}")

        embed_request = {
            "id": submission_hash,
            "ciphertext_hex": pending.get("ciphertext_hex"),
            "wrapped_dek_mod": pending.get("wrapped_dek_mod"),
            "epoch_id": pending.get("epoch_id", self._cfg.epoch_id),
        }
        if pending.get("stack_hint") is not None:
            embed_request["stack_hint"] = pending.get("stack_hint")

        embed_payload = self._hop(
            hops,
            "mod_embed_retrieval_card",
            lambda: self._leader_rest().mod_embed_retrieval_card(
                items=[embed_request],
                org_id=org_id,
            ),
        )
        if not isinstance(embed_payload, list) or not embed_payload or not isinstance(embed_payload[0], dict):
            raise RuntimeError(
                "embed retrieval card invalid payload "
                f"(type={type(embed_payload).__name__}, "
                f"len={len(embed_payload) if isinstance(embed_payload, (list, dict)) else 'n/a'})"
            )

        card = embed_payload[0]
        classified = self._build_classified_keywords(keywords)
        submit_keyword_payload = self._hop(
            hops,
            "submit_keyword_results",
            lambda: self._hub_client.submit_keyword_results(
                self._leader,
                org_id,
                submission_hash,
                classified,
            ),
        )
        verify_entry = {
            "submission_hash": submission_hash,
            "vector": card.get("vector"),
            "embedding_model_id": card.get("embedding_model_id"),
            "embedding_schema_version": card.get("embedding_schema_version"),
            "umbral_capsule": card.get("umbral_capsule"),
            "umbral_ciphertext": card.get("umbral_ciphertext"),
        }
        verify_payload = self._hop(
            hops,
            "verify_keywords",
            lambda: self._hub_client.verify_keywords(self._leader, org_id, entries=[verify_entry]),
        )
        self._require_hub_passed_result(verify_payload, submission_hash, "verify_keywords")
        producer_model_id_value = str(producer_model_id or "").strip()
        if not producer_model_id_value:
            raise RuntimeError(
                "leader_verify_and_commit requires producer_model_id for provenance stamping"
            )
        batch_payload = self._hop(
            hops,
            "batch_submit",
            lambda: self._hub_client.batch_submit(self._leader, org_id),
        )
        commit_batch_payload = self._hop(
            hops,
            "commit_batch",
            lambda: self._commit_batch(org_id, batch_payload, producer_model_id_value),
        )

        deadline = time.time() + 30
        commit_payload: Any = None
        while time.time() < deadline:
            commit_payload = self._hop(
                hops,
                "commit_status",
                lambda: self._hub_client.commit_status(self._leader, org_id),
            )
            if self._is_committed(commit_payload, submission_hash):
                return {
                    "hops": hops,
                    "queue_item": pending,
                    "embed_card": card,
                    "submit_keyword_results": submit_keyword_payload,
                    "verify_keywords": verify_payload,
                    "batch_submit": batch_payload,
                    "commit_batch": commit_batch_payload,
                    "commit_status": commit_payload,
                }
            self._sleep(0.5)

        raise TimeoutError(f"commit_status did not report committed for {submission_hash}")

    def prove_delivery(self, org_id: str, expected_text_fragment: str | list[Any]) -> dict[str, Any]:
        raw_targets: list[Any]
        if isinstance(expected_text_fragment, list):
            raw_targets = expected_text_fragment
        elif isinstance(expected_text_fragment, str):
            raw_targets = [expected_text_fragment] if expected_text_fragment.strip() else []
        else:
            raise RuntimeError(
                "prove_delivery expected a fragment string or list of strings/{fragment,cid} targets"
            )

        targets: list[dict[str, str | None]] = []
        for raw_target in raw_targets:
            fragment_source: Any = None
            cid_source: Any = None

            if isinstance(raw_target, str):
                fragment_source = raw_target
            elif isinstance(raw_target, dict):
                fragment_source = raw_target.get("fragment")
                cid_source = raw_target.get("cid")
            elif isinstance(raw_target, tuple) and len(raw_target) == 2:
                fragment_source, cid_source = raw_target
            elif isinstance(raw_target, list) and len(raw_target) == 2:
                fragment_source, cid_source = raw_target[0], raw_target[1]
            else:
                raise RuntimeError(
                    "prove_delivery list items must be str, {fragment,cid} dict, or (fragment,cid) pair"
                )

            fragment = self.memory_fragment(str(fragment_source or ""))
            if not fragment:
                continue

            cid: str | None = None
            if cid_source is not None:
                cid_value = str(cid_source).strip()
                cid = cid_value or None

            targets.append({"fragment": fragment, "cid": cid})

        if not targets:
            return {
                "delivery": "NO",
                "n_memories": 0,
                "matched": False,
                "any_matched": False,
                "per_memory": [],
            }

        leader_rest = self._leader_rest()
        delivery_trace = new_trace_id()
        per_memory: list[dict[str, Any]] = []
        all_delivered = True
        all_matched = True
        any_matched = False

        for target in targets:
            fragment = str(target["fragment"])
            cid = target["cid"]

            payload = leader_rest.recall(
                query=fragment,
                org_id=org_id,
            )
            memories = payload.get("memories") if isinstance(payload, dict) else None
            memory_items = memories if isinstance(memories, list) else []
            returned_cids = {
                str(item.get("cid")).strip()
                for item in memory_items
                if isinstance(item, dict) and isinstance(item.get("cid"), str) and str(item.get("cid")).strip()
            }

            fragment_lc = fragment.lower()
            matched = False
            for item in memory_items:
                text = item.get("text") if isinstance(item, dict) else None
                if isinstance(text, str) and fragment_lc and fragment_lc in text.lower():
                    matched = True
                    break

            delivered = matched
            delivery_mode = "matched" if matched else "unmatched"
            suppression_entry: dict[str, Any] | None = None

            if not matched and isinstance(cid, str) and cid:
                suppression = payload.get("suppression") if isinstance(payload, dict) else None
                if isinstance(suppression, dict):
                    dropped_raw = suppression.get("dropped_twin_cid")
                    winner_raw = suppression.get("winner_cid")
                    dropped_cid = dropped_raw.strip() if isinstance(dropped_raw, str) else ""
                    winner_cid = winner_raw.strip() if isinstance(winner_raw, str) else ""
                    score_gap_raw = suppression.get("score_gap")
                    score_gap: float | None = None
                    if isinstance(score_gap_raw, (int, float)) and not isinstance(score_gap_raw, bool):
                        score_gap = float(score_gap_raw)

                    if dropped_cid and dropped_cid == cid:
                        suppression_entry = {
                            "winner_cid": fp(winner_cid) if winner_cid else None,
                            "dropped_twin_cid": fp(dropped_cid),
                            "score_gap": score_gap,
                        }
                        if winner_cid and winner_cid in returned_cids:
                            delivered = True
                            delivery_mode = "twin_of_returned"
                            self._log(
                                "info",
                                "lifecycle.m2.delivery_twin",
                                delivery_trace,
                                "ok",
                                0,
                                fragment_fp=fp(fragment),
                                cid_fp=fp(cid),
                                winner_cid_fp=fp(winner_cid),
                                dropped_twin_cid_fp=fp(dropped_cid),
                                score_gap=score_gap,
                            )
                        else:
                            delivered = False
                            delivery_mode = "suppressed_winner_absent"

            memory_entry: dict[str, Any] = {
                "fragment_fp": fp(fragment),
                "cid": fp(cid) if isinstance(cid, str) and cid else None,
                "matched": matched,
                "delivered": delivered,
                "delivery_mode": delivery_mode,
            }
            if suppression_entry is not None:
                memory_entry["suppression"] = suppression_entry
            per_memory.append(memory_entry)

            all_delivered = all_delivered and delivered
            all_matched = all_matched and matched
            any_matched = any_matched or matched

        return {
            "delivery": "YES" if all_delivered else "NO",
            "n_memories": len(per_memory),
            "matched": all_matched,
            "any_matched": any_matched,
            "per_memory": per_memory,
        }

    def _qdrant_delta(
        self,
        org_id: str,
        before: dict[str, int],
        after: dict[str, int],
    ) -> dict[str, Any]:
        names = sorted(set(before) | set(after))
        diff = {name: after.get(name, 0) - before.get(name, 0) for name in names}
        plus_one = [name for name, delta in diff.items() if delta == 1]
        grew = {name: delta for name, delta in diff.items() if delta > 0}
        org_collection = self._find_collection_fn(self._qdrant_url, org_id)
        org_delta = diff.get(org_collection) if org_collection else None
        return {
            "before": before,
            "after": after,
            "diff": diff,
            "grew": grew,
            "plus_one_collections": plus_one,
            "org_collection": org_collection,
            "org_collection_delta": org_delta,
            "saw_plus_one": bool(plus_one),
        }

    def run(
        self,
        session_db_path: str,
        model: str,
        api_key: str,
        project_context: dict[str, Any],
    ) -> dict[str, Any]:
        org_id = self._require_org_id()
        qdrant_before = self._snapshot_fn(self._qdrant_url)

        memory = self.produce_memory(
            session_db_path=session_db_path,
            model=model,
            api_key=api_key,
            project_context=project_context,
            org_id=org_id,
        )
        submission_hash = self.submit_memory(org_id, memory)
        verify_commit = self.leader_verify_and_commit(
            org_id,
            submission_hash,
            memory["keywords"],
            producer_model_id=str(model).strip(),
        )

        qdrant_after = self._snapshot_fn(self._qdrant_url)
        qdrant_delta = self._qdrant_delta(org_id, qdrant_before, qdrant_after)
        if not qdrant_delta["saw_plus_one"]:
            raise RuntimeError(
                "expected +1 Qdrant point delta after commit, got "
                f"saw_plus_one={qdrant_delta.get('saw_plus_one')} grew={qdrant_delta.get('grew')}"
            )

        expected_fragment = self.memory_fragment(str(memory.get("text") or ""))
        delivery = self.prove_delivery(
            org_id,
            [{"fragment": expected_fragment, "cid": submission_hash}],
        )

        memory_text = memory["text"] if isinstance(memory.get("text"), str) else ""
        keywords = memory.get("keywords")
        memory_keywords = keywords if isinstance(keywords, list) else []
        hops = verify_commit.get("hops") if isinstance(verify_commit, dict) else []
        verify_hops: list[Any] = []
        if isinstance(hops, list):
            for hop in hops:
                if isinstance(hop, dict):
                    verify_hops.append(
                        {
                            "hop": hop.get("hop"),
                            "phase": hop.get("phase"),
                            "status": hop.get("status"),
                        }
                    )
                    continue
                verify_hops.append(hop)

        result = {
            "org_id": org_id,
            "submission_hash": submission_hash,
            "memory": {
                "memory_fp": fp(memory_text),
                "text_size": len(memory_text.encode("utf-8")),
                "keyword_count": len(memory_keywords),
            },
            "verify_commit": {
                "committed": True,
                "hop_count": len(verify_hops),
                "hops": verify_hops,
            },
            "qdrant_delta": qdrant_delta,
            "delivery": delivery,
        }
        self._logger.info("M2_RESULT_JSON %s", json.dumps(result, sort_keys=True))
        return result
