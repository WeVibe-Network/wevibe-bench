from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace
from typing import Any

import pytest

# Import the run_cumulative script module by file path (scripts/ is not a package).
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
