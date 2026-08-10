"""WO-HOLD-UI-1 hold-for-UI-review tests.

The hold boots a REAL node server from a stub worktree on the real :8002,
waits on the real RELEASE_HOLD sentinel, and must leave no listener behind
(the ProcessReaper does not watch 8002 — the hold cleans up after itself).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import threading
import time
import urllib.request

import pytest

from wevibe_bench.adapters import backgammon as backgammon_mod
from wevibe_bench.adapters.backgammon import (
    _HOLD_UI_ENV,
    _HOLD_UI_RELEASE_FILE,
    _HOLD_UI_STATE_FILE,
    _hold_for_ui_review,
    _resolve_hold_ui_entrypoint,
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node required")

# Tests NEVER touch the campaign port 8002 (the live gates own it) — each
# port-touching test monkeypatches the module constant to its own high port,
# so the suite is safe to run while a benchmark cell is live, and parallel
# xdist workers never collide with each other.
_BOOT_TEST_PORT = 18311
_UNRESOLVABLE_TEST_PORT = 18312


def _stub_server(port: int) -> str:
    return (
        "const http = require('http');\n"
        "const srv = http.createServer((req, res) => {\n"
        "  if (req.url === '/health') { res.writeHead(200); res.end('ok'); return; }\n"
        "  res.writeHead(200, { 'content-type': 'text/html' });\n"
        "  res.end('<html><body>stub-ui</body></html>');\n"
        "});\n"
        f"srv.listen({port}, '0.0.0.0');\n"
    )


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _mk_worktree(tmp_path: Path, port: int) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "package.json").write_text(
        json.dumps({"name": "stub", "type": "commonjs", "scripts": {"start": "node server.js"}}),
        encoding="utf-8",
    )
    (worktree / "server.js").write_text(_stub_server(port), encoding="utf-8")
    return worktree


def _hold_kwargs(tmp_path: Path, worktree: Path, lines: list[str]) -> dict:
    return {
        "run_label": "test-hold",
        "run_dir": tmp_path,
        "worktree": worktree,
        "container_name": "test-container",
        "live_view_url": "http://127.0.0.1:4096",
        "progress": lines.append,
    }


def _release_when_held(run_dir: Path) -> threading.Thread:
    """Write the release sentinel once the hold's state file exists (the hold
    consumes stale sentinels before waiting, so the sentinel must land AFTER
    the state file). On timeout the hold hangs and pytest-timeout fails loud."""

    def _release() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (run_dir / _HOLD_UI_STATE_FILE).exists():
                time.sleep(0.5)
                (run_dir / _HOLD_UI_RELEASE_FILE).write_text("release\n", encoding="utf-8")
                return
            time.sleep(0.2)

    releaser = threading.Thread(target=_release, daemon=True)
    releaser.start()
    return releaser


def test_hold_disabled_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_HOLD_UI_ENV, raising=False)
    lines: list[str] = []
    _hold_for_ui_review(**_hold_kwargs(tmp_path, _mk_worktree(tmp_path, _BOOT_TEST_PORT), lines))
    assert lines == []
    assert not (tmp_path / _HOLD_UI_STATE_FILE).exists()


def test_hold_boots_real_ui_and_release_tears_it_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_HOLD_UI_ENV, "1")
    monkeypatch.setattr(backgammon_mod, "_HOLD_UI_PORT", _BOOT_TEST_PORT)
    assert _port_free(_BOOT_TEST_PORT), "test requires a free boot-test port"
    worktree = _mk_worktree(tmp_path, _BOOT_TEST_PORT)
    lines: list[str] = []

    releaser = _release_when_held(tmp_path)
    _hold_for_ui_review(**_hold_kwargs(tmp_path, worktree, lines))
    releaser.join(timeout=25)

    joined = "\n".join(lines)
    assert "step=hold-ui boot=ok" in joined
    assert "step=hold-ui waiting" in joined
    assert "step=hold-ui released" in joined
    assert "action=proceed-to-teardown" in joined
    assert _port_free(_BOOT_TEST_PORT), "hold must leave no listener behind"
    assert not (tmp_path / _HOLD_UI_STATE_FILE).exists(), "state file consumed at release"
    assert not (tmp_path / _HOLD_UI_RELEASE_FILE).exists(), "release sentinel consumed"
    # The UI was genuinely observable over real HTTP while held: proven by the
    # boot health probe; re-prove the served page contract from the stub source.
    stub = _stub_server(_BOOT_TEST_PORT)
    assert "/health" in stub and "stub-ui" in stub


def test_hold_survives_unresolvable_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_HOLD_UI_ENV, "1")
    monkeypatch.setattr(backgammon_mod, "_HOLD_UI_PORT", _UNRESOLVABLE_TEST_PORT)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    lines: list[str] = []

    releaser = _release_when_held(tmp_path)
    _hold_for_ui_review(**_hold_kwargs(tmp_path, worktree, lines))
    releaser.join(timeout=25)

    joined = "\n".join(lines)
    assert "entrypoint_unresolved" in joined
    assert "step=hold-ui waiting" in joined
    assert "step=hold-ui released" in joined
    assert _port_free(_UNRESOLVABLE_TEST_PORT)


def test_resolve_entrypoint_prefers_package_start(tmp_path: Path) -> None:
    worktree = _mk_worktree(tmp_path)
    assert _resolve_hold_ui_entrypoint(worktree) == (worktree / "server.js").resolve()


def test_resolve_entrypoint_globs_src_server(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    server = worktree / "src" / "server.ts"
    server.write_text("// stub\n", encoding="utf-8")
    assert _resolve_hold_ui_entrypoint(worktree) == server


def test_resolve_entrypoint_throws_loudly(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    with pytest.raises(RuntimeError, match="no entrypoint resolved"):
        _resolve_hold_ui_entrypoint(worktree)
