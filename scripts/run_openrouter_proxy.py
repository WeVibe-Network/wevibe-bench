"""CLI shim for the OpenRouter proxy; see RUNBOOK for full operator workflow."""

from __future__ import annotations

from wevibe_bench.adapters.openrouter_proxy_server import main


if __name__ == "__main__":
    raise SystemExit(main())
