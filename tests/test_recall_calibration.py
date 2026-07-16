from __future__ import annotations

import json
import re
from pathlib import Path


ARTIFACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "recall"
    / "calibration"
    / "go-concurrency-v1.floor-sweep.json"
)

LIVE_GATE_ARTIFACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "recall"
    / "calibration"
    / "go-concurrency-v1.live-gate.json"
)


def _load_artifact() -> tuple[str, dict]:
    raw = ARTIFACT_PATH.read_text(encoding="utf-8")
    return raw, json.loads(raw)


def _load_livegate_artifact() -> tuple[str, dict]:
    raw = LIVE_GATE_ARTIFACT_PATH.read_text(encoding="utf-8")
    return raw, json.loads(raw)


def test_schema_and_provenance() -> None:
    _, artifact = _load_artifact()

    assert artifact["schema"] == "rb1a-floor-sweep/v1"

    provenance = artifact["provenance"]
    assert provenance["embedding_model"] == "nomic-embed-text:v1.5"
    assert provenance["embedding_dim"] == 768
    assert provenance["fixture_version"]

    frozen = provenance["frozen_constants"]
    assert frozen["gamma"] == 0.1
    assert frozen["delta"] == 0.15
    assert frozen["contested_threshold"] == 0.20


def test_threshold_inclusivity() -> None:
    _, artifact = _load_artifact()
    floors = artifact["floors"]
    f_values = [row["f"] for row in floors]

    assert len(f_values) == 19
    assert f_values[0] == 0.00
    assert f_values[-1] == 0.90
    assert f_values == sorted(f_values)

    for left, right in zip(f_values, f_values[1:]):
        assert abs((right - left) - 0.05) < 1e-9


def test_metric_fields_present() -> None:
    _, artifact = _load_artifact()
    required = {
        "recall_at_1",
        "recall_at_5",
        "precision_at_5",
        "mrr",
        "ndcg_at_5",
        "mean_separation",
        "zero_injection_overall",
        "zero_injection_positive",
        "zero_injection_empty",
        "expected_empty_correct",
        "per_category",
    }

    for row in artifact["floors"]:
        missing = required.difference(row.keys())
        assert not missing, f"row f={row.get('f')} missing fields: {sorted(missing)}"


def test_denominators() -> None:
    _, artifact = _load_artifact()
    den = artifact["denominators"]

    assert den["positive"] == 16
    assert den["expected_empty"] == 7
    assert den["total"] == 23


def test_near_tie_gate_recorded_pass() -> None:
    _, artifact = _load_artifact()
    gate = artifact["near_tie_gate"]

    assert gate["status"] == "PASS"
    assert gate["cases"]
    for case in gate["cases"]:
        assert case["gap"] < 0.20


def test_expected_empty_strictness_monotonic() -> None:
    _, artifact = _load_artifact()
    values = [row["expected_empty_correct"] for row in artifact["floors"]]

    for left, right in zip(values, values[1:]):
        assert right >= left
    assert values[-1] == 7


def test_knee_not_autoselected() -> None:
    _, artifact = _load_artifact()

    assert artifact["knee_selected"] is None

    candidates = artifact["knee_candidates"]
    assert candidates
    distinct_f_stars = {entry.get("f_star") for entry in candidates}
    assert len(distinct_f_stars) >= 2


def test_artifact_content_free() -> None:
    raw, _ = _load_artifact()
    lowered = raw.lower()

    assert "doc_vector" not in raw
    assert "query_vector" not in raw
    assert '"text"' not in raw
    assert re.search(r"\b[a-fA-F0-9]{64}\b", raw) is None

    forbidden = {
        "/users/",
        "jerrysmith",
        "orchestrator",
        "walter",
        "opencode",
    }
    for needle in forbidden:
        assert needle not in lowered

    assert re.search(r"\b[\w.-]+\.md\b", raw, flags=re.IGNORECASE) is None


def test_livegate_artifact_schema() -> None:
    _, artifact = _load_livegate_artifact()

    required_top_level = {
        "schema",
        "version",
        "fixture_version",
        "provisional_floor",
        "generated_at",
        "pipeline_fingerprint",
        "denominators",
        "sim_positive_binary_recall5",
        "live_positive_binary_recall5",
        "live_expected_empty_correct",
        "gates",
        "pass",
        "production_floor",
        "root_cause_class",
        "scale_mismatch_summary",
        "per_category",
        "disclaimer",
    }

    missing = required_top_level.difference(artifact.keys())
    assert not missing, f"missing live-gate keys: {sorted(missing)}"

    assert artifact["schema"] == "rb1a-live-gate/v1"
    assert isinstance(artifact["version"], str)
    assert artifact["version"].strip()
    assert artifact["provisional_floor"] == 0.75

    den = artifact["denominators"]
    assert den == {"positive": 16, "expected_empty": 7, "total": 23}

    assert artifact["sim_positive_binary_recall5"] == 14
    assert artifact["live_positive_binary_recall5"] == 16
    assert artifact["live_expected_empty_correct"] == 0


def test_livegate_gates_failed() -> None:
    _, artifact = _load_livegate_artifact()
    gates = artifact["gates"]

    assert gates["positive_within_one_of_sim"]["pass"] is False
    assert gates["expected_empty_strict"]["pass"] is False
    assert artifact["pass"] is False


def test_livegate_production_floor_unchanged() -> None:
    _, artifact = _load_livegate_artifact()
    production_floor = artifact["production_floor"]

    assert production_floor["value"] == 0.55
    assert production_floor["changed"] is False


def test_livegate_root_cause_class() -> None:
    _, artifact = _load_livegate_artifact()

    assert artifact["root_cause_class"] == "score_scale_mismatch"
    assert isinstance(artifact["scale_mismatch_summary"], str)
    assert artifact["scale_mismatch_summary"].strip()


def test_livegate_content_safe() -> None:
    raw, _ = _load_livegate_artifact()
    lowered = raw.lower()

    assert re.search(r"\b[a-fA-F0-9]{64}\b", raw) is None
    assert "doc_vector" not in raw
    assert "query_vector" not in raw
    assert '"text"' not in raw

    assert "/users/" not in lowered
    assert re.search(r":[0-9]{4,5}\b", raw) is None
    assert re.search(r"\b[\w.-]+\.md\b", raw, flags=re.IGNORECASE) is None

    forbidden = {
        "orchestrator",
        "walter",
        "opencode",
        "gather",
        "sessioncontinuance",
    }
    for needle in forbidden:
        assert needle not in lowered


def test_livegate_readme_consistency() -> None:
    readme = (
        Path(__file__).resolve().parents[1]
        / "recall"
        / "calibration"
        / "README.md"
    ).read_text(encoding="utf-8")
    lowered = readme.lower()

    assert "provisional" in lowered
    assert "0.75" in lowered
    assert "fail" in lowered
    assert "0.55" in lowered
    assert (
        "no production calibration" in lowered
        or "production calibration is not claimed" in lowered
    )


def test_livegate_sweep_consistency() -> None:
    _, sweep = _load_artifact()
    _, livegate = _load_livegate_artifact()

    assert livegate["provisional_floor"] == 0.75
    assert livegate["sim_positive_binary_recall5"] == 14

    row_075 = next(
        (row for row in sweep["floors"] if abs(row["f"] - 0.75) < 1e-9),
        None,
    )
    assert row_075 is not None
    assert row_075["recall_at_5_binary_hits"] == 14
    assert row_075["recall_at_5_binary_hits"] == livegate["sim_positive_binary_recall5"]
    assert sweep["knee_selected"] is None
