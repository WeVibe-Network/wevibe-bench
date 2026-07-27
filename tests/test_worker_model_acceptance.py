from __future__ import annotations

from collections.abc import Sequence
import subprocess

import pytest

from wevibe_bench.preflight import (
    PreflightError,
    WorkerModelProbeResult,
    verify_worker_model_acceptance,
)


def _enable_prechecks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wevibe_bench.preflight.docker_available", lambda: (True, "ok"))
    monkeypatch.setattr("wevibe_bench.preflight.image_exists", lambda image: True)


def test_worker_model_acceptance_fails_on_catalog_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_prechecks(monkeypatch)
    calls: list[str] = []

    def fake_probe(image: str, model: str, timeout_s: float) -> WorkerModelProbeResult:
        _ = image, timeout_s
        calls.append(model)
        return WorkerModelProbeResult(
            exit_code=1,
            detection="catalog-rejected",
            decisive_line="ProviderModelNotFoundError: Model not found: orcarouter/kimi/kimi-k3",
            output=(
                'error="ProviderModelNotFoundError: Model not found: orcarouter/kimi/kimi-k3. '
                'Did you mean: moonshotai/kimi-k3?"'
            ),
        )

    with pytest.raises(PreflightError, match="kimi/kimi-k3") as exc:
        verify_worker_model_acceptance(
            models=["orcarouter/kimi/kimi-k3"],
            docker_probe=fake_probe,
        )

    assert str(exc.value).startswith("PREFLIGHT FAILED: worker model acceptance")
    assert calls == ["orcarouter/kimi/kimi-k3"]


def test_worker_model_acceptance_passes_when_catalog_resolution_is_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_prechecks(monkeypatch)
    calls: list[str] = []

    acceptance_by_model = {
        "orcarouter/kimi/kimi-k3": (
            "catalog-accepted",
            "llm.provider=orcarouter llm.model=kimi/kimi-k3",
            "llm.provider=orcarouter llm.model=kimi/kimi-k3",
        ),
        "orcarouter/kimi/kimi-k2.7-code": (
            "catalog-accepted",
            "stream providerID=orcarouter modelID=kimi/kimi-k2.7-code",
            "stream providerID=orcarouter modelID=kimi/kimi-k2.7-code",
        ),
        "orcarouter/tencent/hy3": (
            "catalog-accepted",
            "AI_APICallError: Cannot connect to API",
            "AI_APICallError: Cannot connect to API",
        ),
    }

    def fake_probe(image: str, model: str, timeout_s: float) -> WorkerModelProbeResult:
        _ = image, timeout_s
        calls.append(model)
        detection, decisive_line, output = acceptance_by_model[model]
        return WorkerModelProbeResult(
            exit_code=1,
            output=output,
            detection=detection,
            decisive_line=decisive_line,
        )

    verify_worker_model_acceptance(
        models=["orcarouter/kimi/kimi-k3", "orcarouter/kimi/kimi-k2.7-code", "orcarouter/tencent/hy3"],
        docker_probe=fake_probe,
    )

    assert calls == ["orcarouter/kimi/kimi-k3", "orcarouter/kimi/kimi-k2.7-code", "orcarouter/tencent/hy3"]


@pytest.mark.parametrize(
    ("docker_ok", "image_ok", "match"),
    [
        ((False, "daemon down"), True, "reason=docker-unavailable"),
        ((True, "ok"), False, "reason=image-missing"),
    ],
)
def test_worker_model_acceptance_prechecks_fail_before_probe(
    monkeypatch: pytest.MonkeyPatch,
    docker_ok: tuple[bool, str],
    image_ok: bool,
    match: str,
) -> None:
    monkeypatch.setattr("wevibe_bench.preflight.docker_available", lambda: docker_ok)
    monkeypatch.setattr("wevibe_bench.preflight.image_exists", lambda image: image_ok)

    called = False

    def fake_probe(image: str, model: str, timeout_s: float) -> WorkerModelProbeResult:
        _ = image, model, timeout_s
        nonlocal called
        called = True
        return WorkerModelProbeResult(exit_code=0, output="")

    with pytest.raises(PreflightError, match=match):
        verify_worker_model_acceptance(
            models=["orcarouter/kimi/kimi-k3"],
            docker_probe=fake_probe,
        )

    assert called is False


def test_worker_model_acceptance_fail_fast_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_prechecks(monkeypatch)
    calls: list[str] = []

    def fake_probe(image: str, model: str, timeout_s: float) -> WorkerModelProbeResult:
        _ = image, timeout_s
        calls.append(model)
        if model == "bad-model":
            return WorkerModelProbeResult(
                exit_code=1,
                output=(
                    "ProviderModelNotFoundError: Model not found: bad-model\n"
                    "stream providerID=orcarouter modelID=bad-model"
                ),
                detection="unknown",
            )
        return WorkerModelProbeResult(
            exit_code=1,
            output="stream providerID=orcarouter modelID=ok-model",
            detection="catalog-accepted",
            decisive_line="stream providerID=orcarouter modelID=ok-model",
        )

    with pytest.raises(PreflightError, match="bad-model"):
        verify_worker_model_acceptance(
            models=["ok-model", "bad-model", "never-model"],
            docker_probe=fake_probe,
        )

    assert calls == ["ok-model", "bad-model"]


def test_worker_model_acceptance_dedupes_models_preserving_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_prechecks(monkeypatch)
    calls: list[str] = []

    def fake_probe(image: str, model: str, timeout_s: float) -> WorkerModelProbeResult:
        _ = image, timeout_s
        calls.append(model)
        return WorkerModelProbeResult(
            exit_code=1,
            output="AI_RetryError: Failed after 3 attempts",
            detection="catalog-accepted",
            decisive_line="AI_RetryError: Failed after 3 attempts",
        )

    verify_worker_model_acceptance(
        models=[
            "orcarouter/kimi/kimi-k3",
            "orcarouter/kimi/kimi-k3",
            "orcarouter/tencent/hy3",
            "orcarouter/tencent/hy3",
        ],
        docker_probe=fake_probe,
    )

    assert calls == ["orcarouter/kimi/kimi-k3", "orcarouter/tencent/hy3"]


def test_worker_model_acceptance_timeout_maps_to_preflight_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_prechecks(monkeypatch)

    def fake_probe(image: str, model: str, timeout_s: float) -> WorkerModelProbeResult:
        _ = image, model, timeout_s
        raise subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=timeout_s)

    with pytest.raises(PreflightError, match="reason=probe-timeout"):
        verify_worker_model_acceptance(models=["orcarouter/kimi/kimi-k3"], docker_probe=fake_probe)


def test_worker_model_acceptance_probe_timeout_result_maps_to_preflight_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_prechecks(monkeypatch)

    def fake_probe(image: str, model: str, timeout_s: float) -> WorkerModelProbeResult:
        _ = image, model, timeout_s
        return WorkerModelProbeResult(
            exit_code=-1,
            output="deadline reached before decisive marker",
            detection="probe-timeout",
            decisive_line="deadline reached before decisive marker",
        )

    with pytest.raises(PreflightError, match="reason=probe-timeout"):
        verify_worker_model_acceptance(models=["orcarouter/kimi/kimi-k3"], docker_probe=fake_probe)


def test_worker_model_acceptance_uses_precheck_mocks_without_real_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_prechecks(monkeypatch)

    seen: list[tuple[str, str, float]] = []

    def fake_probe(image: str, model: str, timeout_s: float) -> WorkerModelProbeResult:
        seen.append((image, model, timeout_s))
        return WorkerModelProbeResult(
            exit_code=0,
            output="build · kimi/kimi-k3",
            detection="catalog-accepted",
            decisive_line="build · kimi/kimi-k3",
        )

    models: Sequence[str] = ("orcarouter/kimi/kimi-k3",)
    verify_worker_model_acceptance(models=models, docker_probe=fake_probe)

    assert len(seen) == 1
