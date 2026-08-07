"""Benchmark run configuration for WeVibe memory ablation.

Recall-mode launch behavior is process-scoped on the MCP/plugin side (not request-body
fields), so benchmark reproducibility requires explicit config for both primary scored
and diagnostic paths.

Per D-BENCH-CONTRACT-2026-07: the benchmark measures pattern/quantity resilience and
capability-direction safety across ordered waves; it is NOT a fixed strong→weak
distillation script. The schedule schema below is the single active path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Arbitrary schedule schema (replaces old fixed model_ladder)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkWave:
    """One wave of the benchmark schedule.

    A wave groups models that run in parallel (or sequentially within the wave).
    After a wave completes, extraction/commit happens, then the next wave starts.

    ``tier`` is UNKNOWN/UNORDERED until registry evidence establishes bands;
    never invent a tier or ordering. ``models`` are the model slugs in this
    wave (arbitrary interleaving supported). ``memory_modes`` specifies which
    recall modes this wave exercises.
    """

    wave_id: str
    models: tuple[str, ...]
    tier: str = "UNKNOWN"  # UNKNOWN/UNORDERED until registry evidence (D-BENCH-CONTRACT-2026-07 §10)
    memory_modes: tuple[str, ...] = ("off", "on")

    def validate(self) -> None:
        """Validate this wave's structure. Raises RuntimeError on violation."""
        if not str(self.wave_id).strip():
            raise RuntimeError(f"wave_id must be non-empty: {self.wave_id!r}")
        if not self.models:
            raise RuntimeError(f"wave {self.wave_id!r} has no models")
        for model in self.models:
            if not str(model).strip():
                raise RuntimeError(f"wave {self.wave_id!r} has blank model")
        valid_tiers = {"UNKNOWN", "UNORDERED"} | {
            "CEILING", "BRACKET", "FLOOR"
        }  # CEILING/BRACKET/FLOOR from variance policy
        if str(self.tier) not in valid_tiers:
            raise RuntimeError(
                f"wave {self.wave_id!r} tier {self.tier!r} not in "
                f"{{UNKNOWN, UNORDERED, CEILING, BRACKET, FLOOR}}"
            )
        for mode in self.memory_modes:
            if mode not in ("off", "on"):
                raise RuntimeError(
                    f"wave {self.wave_id!r} has unknown memory_mode {mode!r}"
                )


@dataclass(frozen=True)
class BenchmarkSchedule:
    """General pattern/quantity-resilience schedule (replaces old fixed model_ladder).

    Supports arbitrary model-capability interleavings and run lengths per
    D-BENCH-CONTRACT-2026-07. Waves are ordered; within each wave, models
    may run in any order (interleaving supported).

    ``waves`` is the ordered list of waves. ``schema_version`` bumps on
    structural changes.
    """

    waves: tuple[BenchmarkWave, ...] = ()
    schema_version: int = 1  # bump when structure/interpretation changes

    def validate(self) -> None:
        """Validate the entire schedule. Raises RuntimeError on violation."""
        if not self.waves:
            raise RuntimeError("benchmark schedule has no waves")
        seen_wave_ids: set[str] = set()
        for wave in self.waves:
            wave.validate()
            if wave.wave_id in seen_wave_ids:
                raise RuntimeError(
                    f"duplicate wave_id {wave.wave_id!r} in schedule"
                )
            seen_wave_ids.add(wave.wave_id)
        # Ensure at least one wave with "off" mode (baseline required)
        has_off = any("off" in w.memory_modes for w in self.waves)
        if not has_off:
            raise RuntimeError(
                "benchmark schedule must include at least one wave with 'off' memory_mode"
            )

    def all_models(self) -> tuple[str, ...]:
        """Return all model slugs across all waves, deduplicated, preserving first-seen order."""
        seen: set[str] = set()
        models: list[str] = []
        for wave in self.waves:
            for model in wave.models:
                if model not in seen:
                    seen.add(model)
                    models.append(str(model))
        return tuple(models)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-serializable dict for manifest."""
        return {
            "schema_version": self.schema_version,
            "waves": [
                {
                    "wave_id": w.wave_id,
                    "models": list(w.models),
                    "tier": w.tier,
                    "memory_modes": list(w.memory_modes),
                }
                for w in self.waves
            ],
        }


def parse_benchmark_schedule(payload: dict[str, Any]) -> BenchmarkSchedule:
    """Parse and validate a benchmark schedule from a dict.

    Accepts the output of BenchmarkSchedule.to_dict() or a manual dict.
    Raises RuntimeError on validation failure.
    """
    waves_raw = payload.get("waves")
    if not isinstance(waves_raw, list):
        raise RuntimeError("benchmark schedule 'waves' must be an array")

    waves: list[BenchmarkWave] = []
    for w_raw in waves_raw:
        if not isinstance(w_raw, dict):
            raise RuntimeError("each wave must be an object")
        wave = BenchmarkWave(
            wave_id=str(w_raw.get("wave_id", "")).strip(),
            models=tuple(str(m) for m in w_raw.get("models", [])),
            tier=str(w_raw.get("tier", "UNKNOWN")) if w_raw.get("tier") else "UNKNOWN",
            memory_modes=tuple(str(m) for m in w_raw.get("memory_modes", ("off", "on"))),
        )
        wave.validate()  # validates in-place
        waves.append(wave)

    return BenchmarkSchedule(
        waves=tuple(waves),
        schema_version=int(payload.get("schema_version", 1)),
    )


def benchmark_schedule_fingerprint(
    schedule: BenchmarkSchedule | None = None,
) -> str:
    """Return a deterministic fingerprint of the schedule.

    Covers wave_ids, model slugs, tiers, and memory_modes.
    """
    resolved = schedule if schedule is not None else _default_benchmark_schedule()
    canonical_payload = [
        {
            "wave_id": wave.wave_id,
            "models": [str(model) for model in wave.models],
            "tier": wave.tier,
            "memory_modes": [str(mode) for mode in wave.memory_modes],
        }
        for wave in resolved.waves
    ]
    canonical = json.dumps(
        canonical_payload,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Default schedule: current canon roster (UNKNOWN/UNORDERED)
# ---------------------------------------------------------------------------

# Canonical benchmark schedule — current roster with UNKNOWN/UNORDERED tiers
# (D-BENCH-CONTRACT-2026-07 §10, DECISIONS §22.10).
# Prior step-down/distillation framing and superseded rosters (D-BENCH-CONTRACT-2026-07)
# are HISTORY, not current benchmark truth.
#
# Roster:
#   kimi/kimi-k3   (tier UNKNOWN)
#   kimi/kimi-k2.7-code  (tier UNKNOWN)
#   tencent/hy3    (tier UNKNOWN)
#
# OrcaRouter routes upstreams internally; provider pins are void on this substrate.
#
# Tiers remain UNKNOWN/UNORDERED until registry evidence (D-BENCH-CONTRACT-2026-07 §10).
# Prior scored roster (opus-4.8 / moonshotai/kimi-k2.7-code / opencode/big-pickle)
# → history.
_DEFAULT_SCHEDULE: BenchmarkSchedule = BenchmarkSchedule(
    waves=(
        BenchmarkWave(
            wave_id="baseline",
            models=(
                "kimi/kimi-k3",
                "kimi/kimi-k2.7-code",
                "tencent/hy3",
            ),
            tier="UNKNOWN",
            memory_modes=("off", "on"),
        ),
    ),
    schema_version=1,
)


def _default_benchmark_schedule() -> BenchmarkSchedule:
    """Return the canonical default benchmark schedule."""
    return _DEFAULT_SCHEDULE


def _default_served_memories_host_path() -> str:
    """Resolved host path for the shared served-memories store JSON."""

    return str(Path("~/.wevibe/served-memories.json").expanduser().resolve())


# ---------------------------------------------------------------------------
# RunConfig — schedule is the single active path (no model_ladder shim)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    """Immutable benchmark manifest enforcing reproducibility and one-org MC-1 symmetry invariants.

    The single active path is ``schedule`` (a BenchmarkSchedule). There is no
    backward-compat fallback to a fixed model_ladder. New runs must provide an
    explicit schedule; the default schedule uses the current canon roster with
    UNKNOWN/UNORDERED tiers (D-BENCH-CONTRACT-2026-07).
    """

    # Schedule is the single active path (replaces old model_ladder).
    schedule: BenchmarkSchedule = field(default_factory=_default_benchmark_schedule)

    tau: float = 0.68  # relevance floor on COMBINED score (ratified). Sent as relevance_floor on the wire.
    rng_seed: int = 20260709  # FIXED — the live D-9.4 sampler seeds from wall-clock; pin it or Recall@k>1 wobbles.
    surface_budget: int = 3  # prod surface budget / max-k
    max_attempts: int = 3  # max solve attempts per task
    deterministic_topn: bool = True  # client-side reproducible top-N by combined_score
    deterministic_recall_limit: int = 64  # wire limit when deterministic_topn (hub returns full candidate set)
    arm_org_map: dict[str, str] = field(default_factory=dict)  # arm/condition -> org_id override (two-corpora)
    run_label: str = ""  # threaded for per-cell session ids
    recall_mode: str = "test"  # Diagnostic/non-primary WEVIBE_RECALL_MODE seam; retained intentionally.
    # Per DECISIONS.md D-BENCH-CONTRACT §b, primary scored ON path must not rely on
    # hidden test-mode auto-accept. Primary path runs recall in prod mode and uses a
    # declared governor policy (relevance floor + injection budget) via plugin-config.
    primary_recall_mode: str = "prod"
    primary_recall_relevance_floor: float = 0.0
    primary_recall_max_injected: int = 1000
    served_memories_host_path: str = field(default_factory=_default_served_memories_host_path)
    served_memories_container_path: str = "/home/worker/.wevibe/served-memories.json"
    require_delivery_verification: bool = True  # BENCHMARK INTEGRITY: refuse to score an ON cell whose delivery != YES
    org_id: str = ""  # D5a: org MUST be pinned explicitly by the run driver; wevibe-org-0 is never a valid arm target.
    # orchestrator._resolve_owned_org handles empty/None gracefully; do NOT make this required (tests build RunConfig() bare).
    mc_version: int = 1  # MC-1
    hub_url: str = field(default_factory=lambda: os.environ.get("WEVIBE_BENCH_HUB_URL") or "http://127.0.0.1:4440")  # wevibe-hub Docker container `wevibe-hub`; health GET /health (public, no auth). The ONE hub. NOT the mcp.
    mcp_recall_url: str = field(default_factory=lambda: os.environ.get("WEVIBE_BENCH_MCP_RECALL_URL") or "http://127.0.0.1:4450")  # wevibe-mcp recall CLIENT; health GET /v1/health (bearer-gated). :4450 default, :4550 = Option-C bench clone. NOT the hub.
    # Live-view topology: ONE persistent `opencode serve` per cell, published on a fixed
    # host port bound to the container-side serve port. The founder attaches a TUI via
    # `opencode attach http://127.0.0.1:<serve_host_port>`. 4096 is opencode serve's default.
    serve_host_port: int = field(default_factory=lambda: int(os.environ.get("WEVIBE_BENCH_SERVE_HOST_PORT") or "4096"))  # host-published port for the per-cell opencode serve
    serve_container_port: int = field(default_factory=lambda: int(os.environ.get("WEVIBE_BENCH_SERVE_CONTAINER_PORT") or "4096"))  # opencode serve port inside the worker container
    session_token_path: str = "~/.wevibe/mcp-session-token"  # Bearer token source (seam)
    embedding_model: str = "nomic-embed-text:v1.5"  # 768-d local embedding (documentation only)
    harness_version: str = "0.1.0"
    cost_limit_usd: float | None = None
    cost_target_usd: float | None = None
    max_output_tokens: int | None = None
    max_steps_per_attempt: int | None = None
    output_price_per_1m: float | None = None
    reasoning_effort: str | None = None

    def relevance_floor(self) -> float:
        """Return the ratified relevance floor sent as `relevance_floor` on the wire."""

        return self.tau

    def to_dict(self) -> dict:
        """Return all fields as a JSON-serializable manifest for scorecard reproducibility."""

        return {
            "schedule": self.schedule.to_dict(),
            "tau": self.tau,
            "rng_seed": self.rng_seed,
            "surface_budget": self.surface_budget,
            "max_attempts": self.max_attempts,
            "deterministic_topn": self.deterministic_topn,
            "deterministic_recall_limit": self.deterministic_recall_limit,
            "arm_org_map": dict(self.arm_org_map),
            "run_label": self.run_label,
            "recall_mode": self.recall_mode,
            "primary_recall_mode": self.primary_recall_mode,
            "primary_recall_relevance_floor": self.primary_recall_relevance_floor,
            "primary_recall_max_injected": self.primary_recall_max_injected,
            "served_memories_host_path": self.served_memories_host_path,
            "served_memories_container_path": self.served_memories_container_path,
            "require_delivery_verification": self.require_delivery_verification,
            "org_id": self.org_id,
            "mc_version": self.mc_version,
            "hub_url": self.hub_url,
            "mcp_recall_url": self.mcp_recall_url,
            "serve_host_port": self.serve_host_port,
            "serve_container_port": self.serve_container_port,
            "session_token_path": self.session_token_path,
            "embedding_model": self.embedding_model,
            "harness_version": self.harness_version,
            "cost_limit_usd": self.cost_limit_usd,
            "cost_target_usd": self.cost_target_usd,
            "max_output_tokens": self.max_output_tokens,
            "max_steps_per_attempt": self.max_steps_per_attempt,
            "output_price_per_1m": self.output_price_per_1m,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True)
class LadderRung:
    """One rung of the scored backgammon ladder (single source of truth).

    ``role`` is ``"source"`` (knowledge source: session runs feed self-extraction
    into the org pool; not scored for lift) or ``"measure"`` (scored OFF/ON cells;
    consumes the accumulated pool, does NOT extract). ``recorded_class`` is the
    rung's previously recorded CEILING/BRACKET/FLOOR classification used by the
    variance policy's T4 trigger (None = no prior classification on record).
    """

    model: str
    role: str
    memory_modes: tuple[str, ...]
    recorded_class: str | None = None


# Scored-ladder roster — the single source of truth for the ordered rungs.
# CURRENT (2026-08-03, D4): SINGLE SUBJECT. The bench never selects a model —
# it tests whatever is loaded and learns which via API-response observation
# (identity handled separately by the observed-extraction invariant; see D2).
# There is ONE subject = whichever model is resident in LM Studio at run time,
# with two self-lift arms off/on. The `orcarouter/wevibe-bench-worker` slug is a
# neutral marker for "whatever is loaded" — the worker→proxy opencode provider
# selector (already the default) — NOT a specific model identity.
#
# SUPERSEDED history: paid OrcaRouter era 2026-07-24 (kimi-k3 source /
# kimi-k2.7-code BRACKET / tencent-hy3 measure; GLM-5.2 deselected 2026-07-27;
# xiaomi/mimo-v2.5-pro dropped), and the local-model pivot 2026-07-31 that
# enumerated THREE LM Studio aliases (qwen3.6-35b-a3b/40b-deckard/27b-fable).
# Both eras named models; under the one-subject design the roster names none.
BACKGAMMON_SCORED_LADDER_ROSTER: tuple[LadderRung, ...] = (
    LadderRung(
        model="orcarouter/wevibe-bench-worker",
        role="measure",
        memory_modes=("off", "on"),
        recorded_class=None,
    ),
)

# Worker opencode model declarations mirror manager session provider.orcarouter
# model blocks exactly (name/reasoning/tool_call/limit shape). Any worker-only
# additions (interleaved + optional headers) are layered by
# adapters.backgammon.build_worker_opencode_config.
WORKER_MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    # Local LM Studio declarations. These are opencode MODEL BLOCKS used by
    # build_worker_opencode_config — NOT scored-roster rungs (the roster is now a
    # single subject under D4). Model ids are the bench aliases served by the LOCAL
    # llm proxy (config/models.yaml, "Bench aliases"); the worker reaches it
    # via WEVIBE_BENCH_WORKER_SPEND_PROXY_BASE_URL=http://host.docker.internal:4545/v1
    # (or --proxy-base-url). Full native context 262144 per Walter's
    # full-context order; output 32768 so reasoning can never eat the whole
    # completion budget (R2 clamp-guillotine pattern, applied to local
    # thinking models that take no separate reasoning budget).
    "wevibe-bench-worker": {
        "name": "Qwen3.6 35B A3B (local)",
        "reasoning": True,
        "tool_call": True,
        "limit": {
            "context": 262_144,
            "output": 32_768,
        },
        # Pinned so opencode's provider-default temperature (0.55 for Qwen)
        # can never leak into a scored cell invisibly; matches the local
        # proxy's bench-profile default.
        "options": {"temperature": 0.6},
    },
}

# Schema version for the frozen ladder run manifest. Bump whenever the manifest
# structure or the roster's interpretation changes, so that resuming a run frozen
# under an older schema fails loudly instead of being silently reinterpreted.
# v2 = roster-A structured rungs (role/memory_modes/recorded_class) replacing the
# v1 (model_id, run_count) 14-cell tuples.
BACKGAMMON_LADDER_SCHEMA_VERSION: int = 2


def backgammon_scored_ladder_roster() -> tuple[LadderRung, ...]:
    """Return the canonical ordered scored-ladder roster."""

    return BACKGAMMON_SCORED_LADDER_ROSTER


def backgammon_ladder_roster_fingerprint(
    rungs: tuple[LadderRung, ...] | None = None,
) -> str:
    """Return a deterministic fingerprint of the ordered ladder roster.

    The fingerprint covers the resolved model ids, their order, each rung's role,
    its memory modes, and its recorded classification, so that a later change to
    the roster is detectable when validating a run manifest that was frozen
    before the change.
    """

    resolved = tuple(rungs) if rungs is not None else BACKGAMMON_SCORED_LADDER_ROSTER
    canonical = json.dumps(
        [
            [
                str(rung.model),
                str(rung.role),
                [str(mode) for mode in rung.memory_modes],
                rung.recorded_class,
            ]
            for rung in resolved
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
