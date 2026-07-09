"""Benchmark run configuration for WeVibe memory ablation.

Required launch environment for benchmark runs is process-scoped on the MCP side:
WEVIBE_RECALL_MODE=test and WEVIBE_KEYSTORE_TEST=1 must be set before MCP startup.
These are not request-body fields, and the harness cannot toggle them per request.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    recall_mode: str = "test"  # WEVIBE_RECALL_MODE — set on the MCP PROCESS env, not per-request (documented)
    require_delivery_verification: bool = True  # BENCHMARK INTEGRITY: refuse to score an ON cell whose delivery != YES
    org_id: str = "wevibe-org-0"  # the single benchmark org (one-org invariant)
    mc_version: int = 1  # MC-1
    hub_url: str = "http://127.0.0.1:4450"  # wevibe-mcp HTTP API; :4450 + identity-unlock (Touch-ID) is a SEAM
    session_token_path: str = "~/.wevibe/mcp-session-token"  # Bearer token source (seam)
    embedding_model: str = "nomic-embed-text:v1.5"  # 768-d local embedding (documentation only)
    harness_version: str = "0.1.0"

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
            "recall_mode": self.recall_mode,
            "require_delivery_verification": self.require_delivery_verification,
            "org_id": self.org_id,
            "mc_version": self.mc_version,
            "hub_url": self.hub_url,
            "session_token_path": self.session_token_path,
            "embedding_model": self.embedding_model,
            "harness_version": self.harness_version,
        }
