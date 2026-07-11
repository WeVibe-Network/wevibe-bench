"""MCP root resolution for lifecycle subprocess wrappers."""

from __future__ import annotations

import os


def resolve_mcp_root(wevibe_root: str, override: str | None = None) -> str:
    """Resolve MCP root from explicit override, env override, or canonical default."""

    explicit = (override or "").strip()
    if explicit:
        return os.path.expanduser(explicit)

    env_override = os.environ.get("WEVIBE_BENCH_MCP_ROOT", "").strip()
    if env_override:
        return os.path.expanduser(env_override)

    return os.path.join(wevibe_root, "wevibe-mcp")
