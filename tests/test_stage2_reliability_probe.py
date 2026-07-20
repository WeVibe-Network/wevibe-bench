from __future__ import annotations

import importlib.util
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "stage2_reliability_probe.py"
_SPEC = importlib.util.spec_from_file_location("stage2_reliability_probe", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"failed to load script module at {_SCRIPT_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
STAGE2_MAIN = _MODULE.main


@dataclass(slots=True)
class _ResponseSpec:
    status: int
    payload: dict[str, Any]


@dataclass(slots=True)
class _ServerState:
    plans: dict[str, list[_ResponseSpec]]
    request_count: int = 0
    request_by_slug: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _extract_slug(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/v1/models/"
    suffix = "/endpoints"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    encoded_slug = parsed.path[len(prefix) : -len(suffix)]
    return unquote(encoded_slug)


def _handler_for(state: _ServerState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            slug = _extract_slug(self.path)
            if slug is None:
                state.errors.append(f"unexpected path: {self.path}")
                payload = {"error": {"message": "Not Found", "code": 404}}
                body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            state.request_count += 1
            request_idx = state.request_by_slug.get(slug, 0)
            state.request_by_slug[slug] = request_idx + 1

            plan = state.plans.get(slug)
            if not plan:
                state.errors.append(f"missing plan for slug={slug}")
                spec = _ResponseSpec(status=404, payload={"error": {"message": "Not Found", "code": 404}})
            else:
                spec = plan[request_idx] if request_idx < len(plan) else plan[-1]

            body = json.dumps(spec.payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.send_response(spec.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


@contextmanager
def _serve(state: _ServerState) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _load_evidence(out_dir: Path, slug: str) -> dict[str, Any]:
    for path in sorted(out_dir.glob("stage2-*.json")):
        if path.name == "stage2-checkpoint.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("slug") == slug:
            return payload
    raise AssertionError(f"evidence not found for slug={slug}")


def _provider_map(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers = evidence.get("providers")
    assert isinstance(providers, list)
    rows = [row for row in providers if isinstance(row, dict)]
    return {str(row["provider_slug"]): row for row in rows}


def _ok_payload(slug: str, alpha_uptime: float, beta_uptime: float) -> dict[str, Any]:
    return {
        "data": {
            "id": slug,
            "name": slug,
            "endpoints": [
                {
                    "provider_name": "Alpha",
                    "tag": "alpha/fp8",
                    "quantization": "fp8",
                    "uptime_last_30m": alpha_uptime,
                    "max_completion_tokens": 65536,
                    "pricing": {"prompt": "0.00000042", "completion": "0.00000132"},
                },
                {
                    "provider_name": "Beta",
                    "tag": "beta/fp4",
                    "quantization": "fp4",
                    "uptime_last_30m": beta_uptime,
                    "max_completion_tokens": 16384,
                    "pricing": {"prompt": "0.00000039", "completion": "0.00000110"},
                },
            ],
        }
    }


def test_stage2_reliability_probe_collects_evidence_and_error_taxonomy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    good_slug = "z-ai/glm-5.2"
    missing_slug = "missing/model-404"
    failing_slug = "provider/down-500"

    state = _ServerState(
        plans={
            good_slug: [
                _ResponseSpec(status=200, payload=_ok_payload(good_slug, alpha_uptime=99.5, beta_uptime=94.0)),
                _ResponseSpec(status=200, payload=_ok_payload(good_slug, alpha_uptime=99.1, beta_uptime=96.0)),
            ],
            missing_slug: [
                _ResponseSpec(status=404, payload={"error": {"message": "Not Found", "code": 404}}),
            ],
            failing_slug: [
                _ResponseSpec(status=500, payload={"error": {"message": "Upstream failure", "code": 500}}),
            ],
        }
    )

    with _serve(state) as server:
        port = int(server.server_address[1])
        exit_code = STAGE2_MAIN(
            [
                "--slug",
                good_slug,
                "--slug",
                missing_slug,
                "--slug",
                failing_slug,
                "--window-seconds",
                "0.2",
                "--interval-seconds",
                "0.1",
                "--base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--out-dir",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert state.errors == []
    assert state.request_by_slug[good_slug] == 2
    assert state.request_by_slug[missing_slug] == 2
    assert state.request_by_slug[failing_slug] == 2

    stdout_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(stdout_lines) >= 6
    assert all("PROGRESS trace=" in line for line in stdout_lines)
    assert any(f"slug={good_slug}" in line for line in stdout_lines)
    assert any(f"slug={missing_slug}" in line for line in stdout_lines)
    assert any(f"slug={failing_slug}" in line for line in stdout_lines)

    log_files = sorted(tmp_path.glob("stage2-*.log"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert log_text.count("PROGRESS trace=") >= 6

    checkpoint_path = tmp_path / "stage2-checkpoint.json"
    assert checkpoint_path.is_file()
    checkpoint_payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint_payload["schema_version"] == 1
    assert len(checkpoint_payload["completed_pairs"]) == 6

    good_evidence = _load_evidence(tmp_path, good_slug)
    assert good_evidence["schema_version"] == 1
    assert good_evidence["stage"] == 2
    assert good_evidence["window"]["ticks_planned"] == 2
    assert good_evidence["window"]["ticks_done"] == 2

    good_providers = _provider_map(good_evidence)
    alpha = good_providers["alpha/fp8"]
    beta = good_providers["beta/fp4"]

    assert alpha["uptime_samples"] == [99.5, 99.1]
    assert alpha["uptime_observed_min"] == pytest.approx(99.1)
    assert alpha["uptime_observed_mean"] == pytest.approx(99.3)
    assert alpha["routing_tier_observed"] == ["Normal", "Normal"]

    assert beta["uptime_samples"] == [94.0, 96.0]
    assert beta["uptime_observed_min"] == pytest.approx(94.0)
    assert beta["uptime_observed_mean"] == pytest.approx(95.0)
    assert beta["routing_tier_observed"] == ["Down", "Degraded"]

    alpha_latency = alpha["fetch_latency_ms"]
    assert alpha_latency["p50"] is not None
    assert alpha_latency["p90"] is not None
    assert alpha_latency["p90"] >= alpha_latency["p50"] >= 0.0

    recommended_pin = good_evidence["recommended_pin"]
    assert recommended_pin is not None
    assert recommended_pin["provider_slug"] == "alpha/fp8"
    assert recommended_pin["quantization"] == "fp8"
    assert "Normal tier" in recommended_pin["reason"]

    notes = good_evidence["notes"]
    assert any("uptime_last_30m >= 99" in note for note in notes)
    assert any("not model TTFT" in note for note in notes)

    missing_evidence = _load_evidence(tmp_path, missing_slug)
    assert missing_evidence["provider_level_errors"] == []
    assert len(missing_evidence["model_level_errors"]) == 1
    missing_error = missing_evidence["model_level_errors"][0]
    assert missing_error["class"] == "HTTPError"
    assert missing_error["count"] == 2
    assert len(missing_error["timestamps"]) == 2

    failing_evidence = _load_evidence(tmp_path, failing_slug)
    assert failing_evidence["model_level_errors"] == []
    assert len(failing_evidence["provider_level_errors"]) == 1
    failing_error = failing_evidence["provider_level_errors"][0]
    assert failing_error["class"] == "HTTPError"
    assert failing_error["count"] == 2
    assert len(failing_error["timestamps"]) == 2


def test_stage2_reliability_probe_resume_skips_completed_ticks(tmp_path: Path) -> None:
    slug = "z-ai/glm-5.2"
    state = _ServerState(
        plans={
            slug: [
                _ResponseSpec(status=200, payload=_ok_payload(slug, alpha_uptime=99.8, beta_uptime=95.1)),
                _ResponseSpec(status=200, payload=_ok_payload(slug, alpha_uptime=99.7, beta_uptime=95.2)),
            ]
        }
    )

    with _serve(state) as server:
        port = int(server.server_address[1])
        args = [
            "--slug",
            slug,
            "--window-seconds",
            "0.2",
            "--interval-seconds",
            "0.1",
            "--base-url",
            f"http://127.0.0.1:{port}/api/v1",
            "--out-dir",
            str(tmp_path),
        ]

        first_exit = STAGE2_MAIN(args)
        assert first_exit == 0
        assert state.request_by_slug[slug] == 2

        second_exit = STAGE2_MAIN(args + ["--resume"])
        assert second_exit == 0

    assert state.request_by_slug[slug] == 2

    evidence = _load_evidence(tmp_path, slug)
    assert evidence["window"]["ticks_planned"] == 2
    assert evidence["window"]["ticks_done"] == 2


def test_stage2_reliability_probe_dry_run_fetches_once_per_slug(tmp_path: Path) -> None:
    slug_a = "z-ai/glm-5.2"
    slug_b = "xiaomi/mimo-v2.5-pro"

    state = _ServerState(
        plans={
            slug_a: [
                _ResponseSpec(status=200, payload=_ok_payload(slug_a, alpha_uptime=99.6, beta_uptime=96.3)),
            ],
            slug_b: [
                _ResponseSpec(status=200, payload=_ok_payload(slug_b, alpha_uptime=99.4, beta_uptime=96.1)),
            ],
        }
    )

    with _serve(state) as server:
        port = int(server.server_address[1])
        exit_code = STAGE2_MAIN(
            [
                "--slug",
                slug_a,
                "--slug",
                slug_b,
                "--window-seconds",
                "9999",
                "--interval-seconds",
                "0.1",
                "--dry-run",
                "--base-url",
                f"http://127.0.0.1:{port}/api/v1",
                "--out-dir",
                str(tmp_path),
            ]
        )

    assert exit_code == 0
    assert state.request_by_slug[slug_a] == 1
    assert state.request_by_slug[slug_b] == 1

    evidence_a = _load_evidence(tmp_path, slug_a)
    evidence_b = _load_evidence(tmp_path, slug_b)
    assert evidence_a["window"]["ticks_planned"] == 1
    assert evidence_a["window"]["ticks_done"] == 1
    assert evidence_b["window"]["ticks_planned"] == 1
    assert evidence_b["window"]["ticks_done"] == 1
