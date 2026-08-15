"""Lifecycle run configuration for WeVibe hub + MCP transport flows."""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from pathlib import Path


_REPO = pathlib.Path(__file__).resolve().parents[2]


DEFAULT_LEADER_KEYSTORE_PATH = str(Path.home() / ".wevibe" / "bench" / "leader-keystore")


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
    # interactive keychain identity. The bench's org membership and recall are
    # bound to THIS seed-derived identity — running under the :4450 keychain
    # identity means the harness never confirms its own membership.
    leader_mcp_url: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_LEADER_MCP_URL")
        or "http://127.0.0.1:4550"
    )
    leader_keystore_path: str = field(
        default_factory=lambda: os.environ.get(
            "WEVIBE_BENCH_LEADER_KEYSTORE", DEFAULT_LEADER_KEYSTORE_PATH
        )
    )
    leader_identity_seed_hex: str = field(
        default_factory=lambda: os.environ.get("WEVIBE_BENCH_LEADER_SEED_HEX", "")
    )
    session_token_path: str = "~/.wevibe/mcp-session-token"
    # Org SELECTOR pin (WEVIBE_BENCH_ORG_ID): identifies the PRE-PROVISIONED org
    # the run targets — created by the production dashboard, never by the bench.
    # The bench consumes this id as data only (no mint/verify/fund). Empty string
    # means no pin; callers resolve the org or fail loud themselves.
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
            "leader_keystore_path": self.leader_keystore_path,
            "leader_identity_seed_hex": self.leader_identity_seed_hex,
            "session_token_path": self.session_token_path,
            "org_id": self.org_id,
            "leader_signer_dir": self.leader_signer_dir,
            "runs_dir": self.runs_dir,
            "mc_version": self.mc_version,
            "epoch_id": self.epoch_id,
        }
