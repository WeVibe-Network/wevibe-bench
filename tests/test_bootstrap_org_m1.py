from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _preserve_environ():
    """Isolate os.environ: main() -> load_bench_env() exports bench.env vars into
    the process env, which otherwise leaks into later tests (test_lifecycle_io)."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_org_m1.py"
_SPEC = importlib.util.spec_from_file_location("bootstrap_org_m1", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"failed to load script module at {_SCRIPT_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def test_argparse_env_fallback_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_MCP_URL", "http://127.0.0.1:5000")
    monkeypatch.setenv("WEVIBE_BENCH_CONTRIB_MCP_URL", "http://127.0.0.1:5001")
    monkeypatch.setenv("WEVIBE_BENCH_HUB_URL", "http://127.0.0.1:5002")
    monkeypatch.setenv("WEVIBE_BENCH_ORG_NAME", "org-from-env")
    monkeypatch.setenv("WEVIBE_BENCH_ORG_DOMAIN", "env.example")
    monkeypatch.setenv("WEVIBE_BENCH_BOOTSTRAP_ORG_M1_LOG_FILE", "runs/custom.log")

    resolved = _MODULE._resolve_args([])
    assert resolved.leader_mcp_url == "http://127.0.0.1:5000"
    assert resolved.contributor_mcp_url == "http://127.0.0.1:5001"
    assert resolved.hub_url == "http://127.0.0.1:5002"
    assert resolved.org_name == "org-from-env"
    assert resolved.domain == "env.example"
    assert resolved.log_file == "runs/custom.log"

    monkeypatch.delenv("WEVIBE_BENCH_LEADER_MCP_URL", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_CONTRIB_MCP_URL", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_HUB_URL", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_ORG_NAME", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_ORG_DOMAIN", raising=False)
    monkeypatch.delenv("WEVIBE_BENCH_BOOTSTRAP_ORG_M1_LOG_FILE", raising=False)

    defaults = _MODULE._resolve_args([])
    assert defaults.leader_mcp_url == "http://127.0.0.1:4550"
    assert defaults.contributor_mcp_url == "http://127.0.0.1:4451"
    assert defaults.hub_url == "http://127.0.0.1:4440"
    assert defaults.org_name == "wevibe-bench-lifecycle"
    assert defaults.domain == "bench.wevibe.local"
    assert defaults.log_file.endswith(".log")


def test_dry_run_prints_plan_and_does_not_invoke_run_m1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_SEED_HEX", "11" * 32)
    monkeypatch.setenv("WEVIBE_BENCH_CONTRIB_SEED_HEX", "22" * 32)
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_WALLET", "wallet-abc")
    token_path = tmp_path / "token"
    token_path.write_text("token-abc", encoding="utf-8")
    monkeypatch.setenv("WEVIBE_MCP_SESSION_TOKEN", "")

    class _ForbiddenOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            raise AssertionError("orchestrator should not be constructed in --dry-run")

    fetch_calls: list[str] = []

    def _fake_fetch(url: str, headers: dict[str, str]) -> tuple[int, bool]:
        fetch_calls.append(url)
        return 200, True

    resolved = _MODULE.ResolvedArgs(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4451",
        hub_url="http://127.0.0.1:4440",
        org_name="wevibe-bench-lifecycle",
        domain="bench.wevibe.local",
        dry_run=True,
        log_file=str(tmp_path / "dry-run.log"),
    )

    # Use explicit config token path by patching LifecycleConfig default expansion through env.
    # Script reads cfg.session_token_path (~/.wevibe/mcp-session-token), so place file there via monkeypatch HOME.
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    session_dir = fake_home / ".wevibe"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "mcp-session-token"
    session_file.write_text("token-abc", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    exit_code = _MODULE._bootstrap(
        resolved,
        fetch_health=_fake_fetch,
        orchestrator_factory=_ForbiddenOrchestrator,
    )
    assert exit_code == 0
    assert fetch_calls == [
        "http://127.0.0.1:4550/v1/health",
        "http://127.0.0.1:4451/v1/health",
        "http://127.0.0.1:4440/health",
    ]

    out = capsys.readouterr().out
    assert "BOOTSTRAP_M1_DRY_RUN" in out
    assert "\"plan_steps\"" in out
    assert "create_org" in out
    assert "poll_membership" in out


def test_preflight_failure_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_SEED_HEX", "11" * 32)
    monkeypatch.setenv("WEVIBE_BENCH_CONTRIB_SEED_HEX", "22" * 32)
    monkeypatch.setenv("WEVIBE_BENCH_LEADER_WALLET", "wallet-abc")

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    session_dir = fake_home / ".wevibe"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "mcp-session-token").write_text("token-abc", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    def _failing_fetch(url: str, headers: dict[str, str]) -> tuple[int, bool]:
        if url.endswith("/v1/health"):
            return 503, True
        return 200, True

    resolved = _MODULE.ResolvedArgs(
        leader_mcp_url="http://127.0.0.1:4550",
        contributor_mcp_url="http://127.0.0.1:4451",
        hub_url="http://127.0.0.1:4440",
        org_name="wevibe-bench-lifecycle",
        domain="bench.wevibe.local",
        dry_run=False,
        log_file=str(tmp_path / "preflight-fail.log"),
    )

    with pytest.raises(_MODULE.PreflightError):
        _MODULE._preflight(
            _MODULE.LifecycleConfig(
                leader_mcp_url=resolved.leader_mcp_url,
                contributor_mcp_url=resolved.contributor_mcp_url,
                hub_url=resolved.hub_url,
                org_name=resolved.org_name,
                domain=resolved.domain,
            ),
            fetch_health=_failing_fetch,
        )

    # also assert CLI main exits non-zero and emits fail marker (without network)
    def _raise_preflight(_resolved: Any) -> int:  # noqa: ANN401
        raise _MODULE.PreflightError("mocked preflight failure")

    monkeypatch.setattr(_MODULE, "_bootstrap", _raise_preflight)

    exit_code = _MODULE.main(["--log-file", str(tmp_path / "main-fail.log")])
    assert exit_code != 0
    out = capsys.readouterr().out
    assert "BOOTSTRAP_M1_FAIL" in out
