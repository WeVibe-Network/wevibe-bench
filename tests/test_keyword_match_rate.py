from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import keyword_match_rate as kmr

from wevibe_bench.scorecard import Cell


def _write(path: Path, contents: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def test_telemetry_json_report_computes_match_rate_and_vector_only_counts(tmp_path: Path) -> None:
    telemetry = {
        "precision_dilution": [
            {
                "cid": "c1",
                "matched_keywords": ["bar", "re-entry"],
                "keyword_score": 0.6,
                "vector_score": 0.2,
                "combined_score": 0.8,
            },
            {
                "cid": "c2",
                "matched_keywords": [],
                "keyword_score": 0.0,
                "vector_score": 0.8,
                "combined_score": 0.8,
            },
            {
                "cid": "c3",
                "matched_keywords": ["validation"],
                "keyword_score": 0.1,
                "vector_score": 0.5,
                "combined_score": 0.6,
            },
        ]
    }
    path = _write(tmp_path / "telemetry.json", json.dumps(telemetry))

    memories = kmr.parse_telemetry_json(path)
    report = kmr.compute_report(memories, source={"artifact": str(path), "kind": "telemetry_json", "data_completeness": "full"})

    aggregate = report["aggregate"]
    assert aggregate["served_n"] == 3
    assert aggregate["matched_n"] == 2
    assert aggregate["match_rate"] == pytest.approx(2 / 3)
    assert aggregate["vector_only_serve_count"] == 1

    rows = report["memories"]
    assert rows[0]["matched_count"] == 2
    assert rows[1]["vector_only"] is True
    assert rows[2]["matched_keywords"] == ["validation"]


def test_recall_smoke_log_parser_flattens_query_keywords_and_reports_honestly(tmp_path: Path) -> None:
    log = (
        "2026-07-25 INFO recall_smoke needcard dense_digest='dbg' "
        "keyword_channel={'language': 'python', 'stack': ['backgammon', 'move-validation']}\n"
        "2026-07-25 INFO recall_smoke   memory[0] cid=abc123 score=0.80 vector=0.80 combined=0.80 "
        "keyword=0.00 text_len=120 has_content=True preview='x'\n"
        "2026-07-25 INFO recall_smoke   memory[1] cid=def456 score=0.90 vector=0.70 combined=0.70 "
        "keyword=0.50 text_len=121 has_content=True preview='y'\n"
    )
    path = _write(tmp_path / "recall-smoke.log", log)

    memories, query_keywords = kmr.parse_recall_smoke_log(path)
    assert query_keywords == ["python", "backgammon", "move-validation"]

    report = kmr.compute_report(
        memories,
        query_keywords=query_keywords,
        source={"artifact": str(path), "kind": "recall_smoke_log", "data_completeness": "served_only"},
    )

    aggregate = report["aggregate"]
    assert aggregate["served_n"] == 2
    assert aggregate["matched_n"] == 1
    assert aggregate["vector_only_serve_count"] == 1
    assert report["unmatched_query_keywords"] is None


def test_plugin_log_parser_reports_served_only_without_fabricated_keyword_data(tmp_path: Path) -> None:
    log = (
        "2026-07-25 [inject] injected count=1 chars=120 sid=s newly_served=1: "
        "547b5c0b711f(score=0.915, \"preview\")\n"
        "2026-07-25 [inject] injected count=2 chars=220 sid=s2 newly_served=2: "
        "aaaabbbbcccc(score=0.510, \"preview\")\n"
    )
    path = _write(tmp_path / "wevibe-plugin-errors.log", log)

    memories = kmr.parse_plugin_log(path)
    report = kmr.compute_report(
        memories,
        source={"artifact": str(path), "kind": "plugin_log", "data_completeness": "served_only"},
    )

    assert report["source"]["data_completeness"] == "served_only"
    assert report["aggregate"]["served_n"] == 2
    assert report["aggregate"]["matched_n"] == 0
    assert report["aggregate"]["match_rate"] == 0.0
    assert report["memories"][0]["matched_keywords"] == []
    assert report["memories"][0]["keyword_score"] is None


def test_compute_report_empty_memories_has_zero_served_and_none_match_rate() -> None:
    report = kmr.compute_report([], source={"artifact": "x", "kind": "telemetry_json", "data_completeness": "full"})
    assert report["aggregate"]["served_n"] == 0
    assert report["aggregate"]["match_rate"] is None


def test_scorecard_additive_keyword_fields_are_emitted_with_values_and_none_defaults() -> None:
    cell = Cell(
        model="m",
        task_id="t",
        condition="ON",
        resolved=True,
        input_tokens=1,
        output_tokens=2,
        turns=1,
        wall_cost_usd=0.1,
        wall_seconds=0.2,
        delivery="YES",
        scored=True,
        keyword_match_rate=0.5,
        keyword_matched_count=3,
        keyword_served_count=6,
        vector_only_serve_count=2,
    )
    payload = cell.to_dict()
    assert payload["keyword_match_rate"] == 0.5
    assert payload["keyword_matched_count"] == 3
    assert payload["keyword_served_count"] == 6
    assert payload["vector_only_serve_count"] == 2

    default_payload = Cell(
        model="m",
        task_id="t",
        condition="OFF",
        resolved=False,
        input_tokens=1,
        output_tokens=2,
        turns=1,
        wall_cost_usd=0.1,
        wall_seconds=0.2,
        delivery="N/A",
        scored=True,
    ).to_dict()
    assert "keyword_match_rate" in default_payload
    assert "keyword_matched_count" in default_payload
    assert "keyword_served_count" in default_payload
    assert "vector_only_serve_count" in default_payload
    assert default_payload["keyword_match_rate"] is None
    assert default_payload["keyword_matched_count"] is None
    assert default_payload["keyword_served_count"] is None
    assert default_payload["vector_only_serve_count"] is None


def test_cli_main_emits_json_for_tmp_telemetry_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    telemetry = {
        "precision_dilution": [
            {"cid": "c1", "matched_keywords": ["bar"], "keyword_score": 0.2, "vector_score": 0.1, "combined_score": 0.3}
        ]
    }
    _write(tmp_path / "sample.json", json.dumps(telemetry))

    rc = kmr.main([str(tmp_path)])
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 0
    assert payload["aggregate"]["served_n"] == 1
    assert payload["aggregate"]["matched_n"] == 1
