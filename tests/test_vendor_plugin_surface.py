"""WO-TRIGGER-BUILD A10 / C2: vendored plugin surface guard.

Asserts the vendored `docker/worker/vendor/wevibe-opencode-plugin` working tree
carries the A8 per-session funnel seam counters and the C1 funnel-snapshot
export that must reach the worker image.

Deliberately NOT a commit-hash assertion: the vendored tree also carries the
uncommitted C1 change, and the worker image is not rebuilt by this chunk (the
A3 follow-on re-establishes hash-match). We assert only the on-disk surface
that this revendor produced.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_PLUGIN = REPO_ROOT / "docker" / "worker" / "vendor" / "wevibe-opencode-plugin"
FUNNEL_COUNTERS = VENDOR_PLUGIN / "plugins" / "funnel-counters.ts"
WEVIBE_PLUGIN = VENDOR_PLUGIN / "plugins" / "wevibe-plugin.ts"


def _read(path: Path) -> str:
    assert path.is_file(), f"vendored file missing: {path}"
    return path.read_text(encoding="utf-8")


def test_vendored_funnel_counters_file_present() -> None:
    """The C1 funnel counters module exists in the vendored tree (was absent
    in the stale eb79c89 vendor)."""
    assert FUNNEL_COUNTERS.is_file(), (
        f"vendored funnel-counters.ts missing at {FUNNEL_COUNTERS} — revendor first"
    )


def test_vendored_a8_surface_present() -> None:
    """A8 per-session funnel seam counters surface is present in the vendor."""
    src = _read(FUNNEL_COUNTERS)
    assert "snapshotAll" in src
    assert "serializeFunnelSnapshot" in src


def test_vendored_c1_wiring_present() -> None:
    """The C1 funnel-snapshot export wiring reaches the vendored wevibe-plugin.ts."""
    plugin = _read(WEVIBE_PLUGIN)
    assert "serializeFunnelSnapshot" in plugin
    assert "writeFunnelSnapshot" in plugin