"""Lifecycle run configuration for WeVibe hub + MCP transport flows."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleConfig:
    """Immutable lifecycle manifest for canonical-message and signing runs."""

    hub_url: str = "http://127.0.0.1:4440"
    leader_mcp_url: str = "http://127.0.0.1:4450"
    contributor_mcp_url: str = "http://127.0.0.1:4451"
    org_name: str = "wevibe-bench-lifecycle"
    domain: str = "bench.wevibe.local"
    session_token_path: str = "~/.wevibe/mcp-session-token"
    leader_signer_dir: str = "/Users/jerrysmith/Desktop/benchmark/scaffold/leader-signer"
    runs_dir: str = "~/Desktop/benchmark/runs"
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
            "session_token_path": self.session_token_path,
            "leader_signer_dir": self.leader_signer_dir,
            "runs_dir": self.runs_dir,
            "mc_version": self.mc_version,
            "epoch_id": self.epoch_id,
        }
