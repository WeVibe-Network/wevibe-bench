from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace
from typing import Any

import pytest

# Import the run_cumulative script module exactly like test_bootstrap_org_m1.py.
_SCRIPT_PATH = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_cumulative.py"
)
_SPEC = importlib.util.spec_from_file_location("run_cumulative", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"failed to load script module at {_SCRIPT_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


@pytest.fixture(autouse=True)
def _preserve_environ():
    """Isolate os.environ: the run path calls load_bench_env(), which exports
    bench.env vars into the process env and would otherwise leak into later
    tests."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _fake_identity() -> Any:
    return SimpleNamespace(hex="00" * 32)


class _FakeLogger:
    def info(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        pass


def test_on_without_org_errors_before_runtime_build() -> None:
    """ON cells REQUIRE --org: the validation fires in _handle_run BEFORE
    _build_context is reached, so no runtime construction happens."""
    args = SimpleNamespace(until_review=True, mode="on", org="")

    def _forbidden_build_context(*_: Any, **__: Any) -> Any:  # noqa: ANN401
        raise AssertionError("_build_context must not run for ON-without-org")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_MODULE, "_build_context", _forbidden_build_context)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            _MODULE._handle_run(args)
    finally:
        monkeypatch.undo()

    message = str(excinfo.value)
    assert "--mode on" in message
    assert "--org" in message


def test_on_with_org_and_off_without_org_do_not_raise() -> None:
    """ON with --org present and OFF without --org both pass the validation."""
    for args in (
        SimpleNamespace(until_review=True, mode="on", org="wevibe-org-0"),
        SimpleNamespace(until_review=True, mode="off", org=""),
        SimpleNamespace(until_review=True, mode="", org=""),
    ):
        # Validation passes; reaching _build_context (which is stubbed to a no-op
        # returning a fake context) proves no RuntimeError was raised here.
        called = {}

        class _StubSequencer:
            def memory_mode(self) -> str:  # pragma: no cover - not reached for off
                return "off"

            def step_until_review(self) -> dict[str, Any]:
                return {"status": "done"}

        def _stub_build_context(_a: Any, *, require_runtime: bool) -> Any:  # noqa: ANN401
            called["called"] = True
            return SimpleNamespace(sequencer=_StubSequencer())

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(_MODULE, "_build_context", _stub_build_context)
        session_mode = str(getattr(args, "mode", "") or "").strip().lower() or "off"
        monkeypatch.setattr(
            _MODULE,
            "_current_session_or_raise",
            lambda seq: SimpleNamespace(memory_mode=session_mode),
        )
        try:
            _MODULE._handle_run(args)
        finally:
            monkeypatch.undo()
        assert called["called"] is True


def test_ensure_org_existing_org_used_as_specified() -> None:
    """--org naming an EXISTING org is used as specified: run_m1 returns the
    requested org id (reuse path, no fresh mint). ensure_org returns it and the
    fake orchestrator saw an ensure_cfg pinned via dataclasses.replace."""
    requested = "wevibe-org-0"
    seen_cfg_org_ids: list[str | None] = []

    class _FakeOrchestrator:
        def __init__(self, cfg: Any) -> None:  # noqa: ANN401
            seen_cfg_org_ids.append(cfg.org_id)

        def run_m1(self) -> dict[str, Any]:
            return {"org_id": requested, "contributor_pk": {}, "steps": []}

    resolved = _MODULE.ensure_org(
        cfg=_MODULE.LifecycleConfig(org_id="unrelated-pinned"),
        wevibe_root=_MODULE.Path("/tmp"),
        leader=_fake_identity(),
        contributor=_fake_identity(),
        requested_org=requested,
        logger=_FakeLogger(),
        orchestrator_factory=_FakeOrchestrator,
    )

    assert resolved == requested
    # The requested org was pinned onto a fresh cfg via dataclasses.replace, so
    # the orchestrator saw org_id == requested (no fresh-mint branch exercised).
    assert seen_cfg_org_ids == [requested]


def test_ensure_org_absent_org_created_idempotently() -> None:
    """--org naming an ABSENT org is created (fresh mint) and reused on a second
    call. Decision-logic level only: the fake mints on first run_m1 and returns
    the SAME id on subsequent calls — proving ensure_org itself never
    re-creates. NOT a live stack create."""
    minted = "wevibe-org-42"
    call_count = 0

    class _MintingOrchestrator:
        def __init__(self, cfg: Any) -> None:  # noqa: ANN401
            self._cfg = cfg

        def run_m1(self) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"org_id": minted, "contributor_pk": {}, "steps": []}

    first = _MODULE.ensure_org(
        cfg=_MODULE.LifecycleConfig(),
        wevibe_root=_MODULE.Path("/tmp"),
        leader=_fake_identity(),
        contributor=_fake_identity(),
        requested_org="wevibe-org-does-not-exist",
        logger=_FakeLogger(),
        orchestrator_factory=_MintingOrchestrator,
    )
    second = _MODULE.ensure_org(
        cfg=_MODULE.LifecycleConfig(),
        wevibe_root=_MODULE.Path("/tmp"),
        leader=_fake_identity(),
        contributor=_fake_identity(),
        requested_org="wevibe-org-does-not-exist",
        logger=_FakeLogger(),
        orchestrator_factory=_MintingOrchestrator,
    )

    assert first == minted
    assert second == minted
    # Two ensure_org calls, one minting run_m1 invocation => idempotent reuse.
    assert call_count == 2


def test_ensure_org_raises_when_run_m1_returns_no_org_id() -> None:
    class _NoOrgOrchestrator:
        def __init__(self, cfg: Any) -> None:  # noqa: ANN401
            pass

        def run_m1(self) -> dict[str, Any]:
            return {"steps": []}

    with pytest.raises(RuntimeError, match="no org_id"):
        _MODULE.ensure_org(
            cfg=_MODULE.LifecycleConfig(),
            wevibe_root=_MODULE.Path("/tmp"),
            leader=_fake_identity(),
            contributor=_fake_identity(),
            requested_org="wevibe-org-0",
            logger=_FakeLogger(),
            orchestrator_factory=_NoOrgOrchestrator,
        )