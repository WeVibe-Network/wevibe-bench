"""Lifecycle run configuration for WeVibe hub + MCP transport flows."""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from pathlib import Path


_REPO = pathlib.Path(__file__).resolve().parents[2]


DEFAULT_ORG_DESCRIPTION = (
    "WeVibe benchmark org for the coding-agent memory-ablation harness. Its memory corpus holds reusable engineering knowledge extracted while an agent builds a TypeScript/Node backgammon game server: board and movement rules (bar entry, hitting, bearing off, use-max-dice), pip counting, doubling-cube strategy, and AI move selection. The corpus exists to measure how org memory recall affects agent correctness across OFF/ON ablation runs."
)
DEFAULT_ORG_TECH_STACK = (
    "TypeScript, Node.js, HTTP/REST JSON API (port 8002), npm, Vitest, Playwright"
)
DEFAULT_ORG_FOCUS_AREAS = (
    "Backgammon game-server engineering: board state and move legality (bar entry, hitting, bearing off, use-max rules), pip counting, doubling-cube decisions, AI difficulty levels, HTTP API design"
)
DEFAULT_ORG_KEYWORDS = (
    "backgammon",
    "game_state",
    "board_state",
    "checker",
    "blot",
    "hitting",
    "bar_entry",
    "bearing_off",
    "use_max_dice",
    "higher_die",
    "pip_count",
    "home_board",
    "doubling_cube",
    "cube_decision",
    "take_point",
    "gammon",
    "win_probability",
    "legal_moves",
    "ai_difficulty",
    "http_api",
)
DEFAULT_LEADER_KEYSTORE_PATH = str(Path.home() / ".wevibe" / "bench" / "leader-keystore")
DEFAULT_CONTRIB_KEYSTORE_PATH = str(Path.home() / ".wevibe" / "bench" / "contrib-keystore")


def _parse_org_keywords(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_ORG_KEYWORDS
    parsed = tuple(part.strip() for part in raw.split(",") if part.strip())
    return parsed or DEFAULT_ORG_KEYWORDS


@dataclass(frozen=True)
class LifecycleConfig:
    """Immutable lifecycle manifest for canonical-message and signing runs."""

    hub_url: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_HUB_URL")
        or "http://127.0.0.1:4440"
    )
    # The bench leader MCP is the Option-C bench clone on :4550 — the ONLY MCP
    # whose identity is seed-derived (WEVIBE_IDENTITY_SEED_HEX) and therefore
    # equal to the harness leader. NEVER default this to :4450: that is the real
    # host wevibe-mcp, which has no seed support at all and always loads the
    # interactive keychain identity. `create_org` hands this URL to leader-signer
    # as WEVIBE_MCP_URL, and /v1/org-setup stamps THAT MCP's pubkey as the org's
    # leader — so a :4450 default silently mints every fresh org under the wrong
    # identity, and the harness then never confirms its own membership.
    leader_mcp_url: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_LEADER_MCP_URL")
        or "http://127.0.0.1:4550"
    )
    contributor_mcp_url: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_CONTRIB_MCP_URL")
        or "http://127.0.0.1:4451"
    )
    leader_keystore_path: str = field(
        default_factory=lambda: os.environ.get(
            "WEVIBE_BENCH_LEADER_KEYSTORE", DEFAULT_LEADER_KEYSTORE_PATH
        )
    )
    contributor_keystore_path: str = field(
        default_factory=lambda: os.environ.get(
            "WEVIBE_BENCH_CONTRIB_KEYSTORE", DEFAULT_CONTRIB_KEYSTORE_PATH
        )
    )
    leader_identity_seed_hex: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_LEADER_SEED_HEX", "")
    )
    contributor_identity_seed_hex: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_CONTRIB_SEED_HEX", "")
    )
    org_name: str = "wevibe-bench-lifecycle"
    domain: str = "bench.wevibe.local"
    org_description: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_ORG_DESCRIPTION")
        or DEFAULT_ORG_DESCRIPTION
    )
    org_tech_stack: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_ORG_TECH_STACK")
        or DEFAULT_ORG_TECH_STACK
    )
    org_focus_areas: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_ORG_FOCUS_AREAS")
        or DEFAULT_ORG_FOCUS_AREAS
    )
    org_keywords: tuple[str, ...] = field(
        default_factory=lambda: _parse_org_keywords(
            os.environ.get("WEVIBE_BENCH_ORG_KEYWORDS")
        )
    )
    session_token_path: str = "~/.wevibe/mcp-session-token"
    # Optional explicit org pin for the m1 bring-up (WEVIBE_BENCH_ORG_ID).
    # Without it, create_org's owned-org resolution picks sorted-first when the
    # leader belongs to several orgs — wrong target on re-runs. With it set and
    # present in the leader's memberships, that org is reused; absent from
    # memberships, a fresh org is created and the chain-assigned id returned.
    org_id: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_ORG_ID") or ""
    )
    leader_signer_dir: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_LEADER_SIGNER_DIR")
        or str(_REPO / "scaffold" / "leader-signer")
    )
    runs_dir: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_RUNS_DIR") or str(_REPO / "runs")
    )
    mc_version: int = 1
    epoch_id: int = 0

    def expanded_session_token_path(self) -> str:
        return os.path.expanduser(self.session_token_path)

    def to_dict(self) -> dict:
        """Return all fields as a JSON-serializable lifecycle manifest."""

        return {
            "hub_url": self.hub_url,
            "leader_mcp_url": self.leader_mcp_url,
            "contributor_mcp_url": self.contributor_mcp_url,
            "leader_keystore_path": self.leader_keystore_path,
            "contributor_keystore_path": self.contributor_keystore_path,
            "leader_identity_seed_hex": self.leader_identity_seed_hex,
            "contributor_identity_seed_hex": self.contributor_identity_seed_hex,
            "org_name": self.org_name,
            "domain": self.domain,
            "org_description": self.org_description,
            "org_tech_stack": self.org_tech_stack,
            "org_focus_areas": self.org_focus_areas,
            "org_keywords": list(self.org_keywords),
            "session_token_path": self.session_token_path,
            "org_id": self.org_id,
            "leader_signer_dir": self.leader_signer_dir,
            "runs_dir": self.runs_dir,
            "mc_version": self.mc_version,
            "epoch_id": self.epoch_id,
        }
