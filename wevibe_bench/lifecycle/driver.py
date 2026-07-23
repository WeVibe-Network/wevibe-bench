"""Relay driver scaffolding for lifecycle model-rung execution."""

from __future__ import annotations

import subprocess
from typing import Any, Callable

from wevibe_bench.config import RunConfig

from .lconfig import LifecycleConfig


ProducerFn = Callable[[str, str, list[dict[str, Any]]], str]


class RelayDriver:
    def __init__(
        self,
        cfg: LifecycleConfig,
        run_cfg: RunConfig,
        m2_proof: Any,
        logger: Any,
        *,
        exercises: list[str] | None = None,
        producer_fn: ProducerFn | None = None,
    ) -> None:
        self._cfg = cfg
        self._run_cfg = run_cfg
        self._m2 = m2_proof
        self._logger = logger
        self._producer_fn = producer_fn or self._default_producer
        self._exercise_set = list(exercises or ["dry-exercise"])
        self._memory_pool: list[dict[str, Any]] = []
        self._dry_mode = False

    def _org_id(self) -> str:
        orchestrator = getattr(self._m2, "orchestrator", None)
        if orchestrator is not None:
            org_id = getattr(orchestrator, "org_id", None)
            if isinstance(org_id, str) and org_id:
                return org_id
        return "dry-org"

    def _default_producer(self, model: str, exercise: str, recall_pool: list[dict[str, Any]]) -> str:
        recall_lines = [f"- {item.get('text', '')}" for item in recall_pool[-3:] if item.get("text")]
        prompt = exercise
        if recall_lines:
            prompt = f"{exercise}\n\nRecall context:\n" + "\n".join(recall_lines)

        cmd = ["opencode", "run", prompt, "--model", model]
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(f"opencode run failed rc={result.returncode}: {stderr or 'unknown error'}")
        transcript = (result.stdout or "").strip()
        if not transcript:
            raise RuntimeError("producer returned empty transcript")
        return transcript

    def run_rung(self, model: str, exercises: list[str], recall_on: bool) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        org_id = self._org_id()

        for idx, exercise in enumerate(exercises):
            recall_context = list(self._memory_pool) if recall_on else []
            transcript = self._producer_fn(model, exercise, recall_context)
            if not transcript.strip():
                raise RuntimeError(f"producer emitted blank transcript for exercise={exercise}")

            if self._dry_mode:
                memory = {
                    "text": transcript,
                    "keywords": ["dry-pass"],
                    "stack_hint": f"exercise:{exercise}",
                }
                submission_hash = f"dry-{idx + 1}"
                verify_commit: dict[str, Any] = {"status": "skipped", "reason": "dry-pass"}
            else:
                memory = self._m2.produce_memory(
                    transcript=transcript,
                    model=model,
                    api_key="",
                    project_context={
                        "exercise": exercise,
                        "tau": self._run_cfg.relevance_floor(),
                        "surface_budget": self._run_cfg.surface_budget,
                        "recall_on": recall_on,
                        "recall_pool_size": len(recall_context),
                    },
                    org_id=org_id,
                )
                submission_hash = self._m2.submit_memory(org_id, memory)
                verify_commit = self._m2.leader_verify_and_commit(org_id, submission_hash, memory["keywords"])

            self._memory_pool.append(
                {
                    "exercise": exercise,
                    "submission_hash": submission_hash,
                    "text": str(memory.get("text", "")),
                }
            )

            result = {
                "model": model,
                "exercise": exercise,
                "recall_on": recall_on,
                "pool_size": len(self._memory_pool),
                "submission_hash": submission_hash,
                "verify_commit": verify_commit,
            }
            self._logger.info(
                "op=lifecycle.driver.run_rung model=%s exercise=%s recall_on=%s pool_size=%d",
                model,
                exercise,
                recall_on,
                len(self._memory_pool),
            )
            results.append(result)

        return results

    def dry_pass(self, model: str) -> dict[str, Any]:
        exercise = self._exercise_set[0] if self._exercise_set else "dry-exercise"

        original_producer = self._producer_fn
        self._producer_fn = (
            lambda selected_model, selected_exercise, _pool: (
                f"[DRY PASS] transcript for {selected_exercise} via {selected_model}"
            )
        )
        self._dry_mode = True
        try:
            rung_results = self.run_rung(model, [exercise], recall_on=False)
        finally:
            self._dry_mode = False
            self._producer_fn = original_producer

        return {
            "mode": "dry",
            "model": model,
            "exercise": exercise,
            "results": rung_results,
        }
