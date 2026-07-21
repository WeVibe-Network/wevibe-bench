"""Benchmark run configuration for WeVibe memory ablation.

Required launch environment for benchmark runs is process-scoped on the MCP side:
WEVIBE_RECALL_MODE=test and WEVIBE_KEYSTORE_TEST=1 must be set before MCP startup.
These are not request-body fields, and the harness cannot toggle them per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json


@dataclass(frozen=True)
class RunConfig:
    """Immutable benchmark manifest enforcing reproducibility and one-org MC-1 symmetry invariants."""

    # model ladder (capability step-down relay)
    model_ladder: tuple[str, ...] = (
        "opus-4.8",
        "glm-5.2",
        "kimi-k2.6",
        "minimax-m3",
        "minimax-M2.7-LOCAL",
    )
    tau: float = 0.68  # relevance floor on COMBINED score (ratified). Sent as relevance_floor on the wire.
    rng_seed: int = 20260709  # FIXED — the live D-9.4 sampler seeds from wall-clock; pin it or Recall@k>1 wobbles.
    surface_budget: int = 3  # prod surface budget / max-k
    max_attempts: int = 3  # max solve attempts per task
    deterministic_topn: bool = True  # client-side reproducible top-N by combined_score
    deterministic_recall_limit: int = 64  # wire limit when deterministic_topn (hub returns full candidate set)
    arm_org_map: dict[str, str] = field(default_factory=dict)  # arm/condition -> org_id override (two-corpora)
    run_label: str = ""  # threaded for per-cell session ids
    recall_mode: str = "test"  # WEVIBE_RECALL_MODE — set on the MCP PROCESS env, not per-request (documented)
    require_delivery_verification: bool = True  # BENCHMARK INTEGRITY: refuse to score an ON cell whose delivery != YES
    org_id: str = "wevibe-org-0"  # the single benchmark org (one-org invariant)
    mc_version: int = 1  # MC-1
    hub_url: str = "http://127.0.0.1:4440"  # wevibe-hub Docker container `wevibe-hub`; health GET /health (public, no auth). The ONE hub. NOT the mcp.
    mcp_recall_url: str = "http://127.0.0.1:4450"  # wevibe-mcp recall CLIENT; health GET /v1/health (bearer-gated). :4450 default, :4550 = Option-C bench clone. NOT the hub.
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
            "model_ladder": list(self.model_ladder),
            "tau": self.tau,
            "rng_seed": self.rng_seed,
            "surface_budget": self.surface_budget,
            "max_attempts": self.max_attempts,
            "deterministic_topn": self.deterministic_topn,
            "deterministic_recall_limit": self.deterministic_recall_limit,
            "arm_org_map": dict(self.arm_org_map),
            "run_label": self.run_label,
            "recall_mode": self.recall_mode,
            "require_delivery_verification": self.require_delivery_verification,
            "org_id": self.org_id,
            "mc_version": self.mc_version,
            "hub_url": self.hub_url,
            "mcp_recall_url": self.mcp_recall_url,
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


# Canonical Stage-7 scored-ladder roster A (Walter, 21-07-26) — the single source
# of truth for the ordered rungs. Knowledge SOURCE = Opus-4.8 (acing model; its
# fail→fix iteration arcs are the extraction substrate); MEASURE rungs top→bottom:
# kimi-k2.7-code (pin siliconflow/fp8) → big-pickle (opencode/big-pickle via zen
# upstream, free/free — served as xiaomi/mimo-v2.5, identity-checked per cell).
# Paid xiaomi/mimo-v2.5 is DROPPED from the measured roster but RETAINED as the
# qualified fallback profile ("mimo25") — usable only on Walter's explicit
# in-thread approval if big-pickle's identity check trips.
BACKGAMMON_SCORED_LADDER_ROSTER: tuple[LadderRung, ...] = (
    LadderRung(
        model="openrouter/anthropic/claude-opus-4.8",
        role="source",
        memory_modes=("off",),
    ),
    LadderRung(
        model="openrouter/moonshotai/kimi-k2.7-code",
        role="measure",
        memory_modes=("off", "on"),
        recorded_class="BRACKET",
    ),
    LadderRung(
        model="openrouter/opencode/big-pickle",
        role="measure",
        memory_modes=("off", "on"),
        recorded_class=None,
    ),
)

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
