"""Lifecycle run configuration for WeVibe hub + MCP transport flows."""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field


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
    leader_mcp_url: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_LEADER_MCP_URL")
        or "http://127.0.0.1:4450"
    )
    contributor_mcp_url: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_CONTRIB_MCP_URL")
        or "http://127.0.0.1:4451"
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
            "org_name": self.org_name,
            "domain": self.domain,
            "org_description": self.org_description,
            "org_tech_stack": self.org_tech_stack,
            "org_focus_areas": self.org_focus_areas,
            "org_keywords": list(self.org_keywords),
            "session_token_path": self.session_token_path,
            "leader_signer_dir": self.leader_signer_dir,
            "runs_dir": self.runs_dir,
            "mc_version": self.mc_version,
            "epoch_id": self.epoch_id,
        }
