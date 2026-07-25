import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wevibe_bench.cumulative.consumer_decision import (
    CONSUMER_DECISION_SCHEMA_VERSION,
    ConsumerCandidateDecision,
    ConsumerDecisionManifest,
    validate_schema,
)
from wevibe_bench.cumulative.types import SessionRecord


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invoke_main(module: Any, argv: list[str]) -> int:
    prior_argv = list(sys.argv)
    try:
        sys.argv = ["run_cumulative.py", *argv]
        return int(module.main())
    finally:
        sys.argv = prior_argv


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_emit_consumer_decision_template_produces_schema_valid_json(capsys: Any) -> None:
    module = _load_run_cumulative_module()

    rc = _invoke_main(module, ["emit-consumer-decision-template"])
    captured = capsys.readouterr()

    assert rc == 0
    payload = json.loads(captured.out)
    manifest = ConsumerDecisionManifest.from_dict(payload)
    validate_schema(manifest)
    assert manifest.default_fate == "accept"
    assert {decision.fate for decision in manifest.decisions} == {
        "accept",
        "deny",
        "block",
        "report",
    }


def test_validate_consumer_decision_passes_good_and_fails_bad_manifest(
    tmp_path: Path,
    capsys: Any,
) -> None:
    module = _load_run_cumulative_module()

    good_manifest = {
        "schema_version": CONSUMER_DECISION_SCHEMA_VERSION,
        "run_id": "run-good",
        "policy_id": "consumer-policy-good",
        "default_fate": "accept",
        "coordinator_trace": "trace-good",
        "decisions": [
            {
                "run_id": "run-good",
                "session_id": "session-good",
                "candidate_cid": "cid-deny",
                "fate": "deny",
                "coordinator_trace": "trace-good",
                "reason": "not useful",
                "note": "",
            }
        ],
    }
    good_path = tmp_path / "consumer-good.json"
    _write_json(good_path, good_manifest)

    rc_good = _invoke_main(
        module,
        [
            "validate-consumer-decision",
            "--file",
            str(good_path),
            "--run-id",
            "run-good",
            "--session-id",
            "session-good",
            "--recalled-cids",
            "cid-deny,cid-accept",
        ],
    )
    captured_good = capsys.readouterr()

    assert rc_good == 0
    assert "PASS:" in captured_good.out

    bad_manifest = {
        "schema_version": CONSUMER_DECISION_SCHEMA_VERSION,
        "run_id": "run-bad",
        "policy_id": "consumer-policy-bad",
        "coordinator_trace": "trace-bad",
        "decisions": [
            {
                "run_id": "run-bad",
                "session_id": "session-bad",
                "candidate_cid": "cid-dup",
                "fate": "deny",
                "coordinator_trace": "trace-bad",
                "reason": "   ",
                "note": "",
            },
            {
                "run_id": "run-bad",
                "session_id": "session-bad",
                "candidate_cid": "cid-dup",
                "fate": "block",
                "coordinator_trace": "trace-bad",
                "reason": "   ",
                "note": "",
            },
        ],
    }
    bad_path = tmp_path / "consumer-bad.json"
    _write_json(bad_path, bad_manifest)

    rc_bad = _invoke_main(
        module,
        [
            "validate-consumer-decision",
            "--file",
            str(bad_path),
        ],
    )
    captured_bad = capsys.readouterr()

    assert rc_bad == 1
    assert "FAIL:" in captured_bad.out


def test_real_session_runner_consumer_gate_outcome_on_and_off(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = _load_run_cumulative_module()
    module._load_sxe_helpers = lambda _repo_root: (
        lambda **_kwargs: ([], {}, []),
        lambda _session_dir: {},
        lambda _session_dir, **_kwargs: "session-on",
    )

    state_dir = tmp_path / "consumer-state"
    monkeypatch.setenv("WEVIBE_BENCH_CONSUMER_STATE_DIR", str(state_dir))
    _write_json(
        state_dir / "wevibe-plugin-queue.json",
        [
            {"id": "cid-accept", "cid": "cid-accept", "text": "alpha", "source": "recall"},
            {"id": "cid-deny", "cid": "cid-deny", "text": "beta", "source": "recall"},
            {"id": "cid-block", "cid": "cid-block", "text": "gamma", "source": "recall"},
            {"id": "cid-report", "cid": "cid-report", "text": "delta", "source": "recall"},
        ],
    )

    served_store_path = tmp_path / "served-memories.json"
    _write_json(
        served_store_path,
        {
            "version": 1,
            "memories": {
                "cid-accept": {
                    "cid": "cid-accept",
                    "text": "accepted memory",
                    "session_ids": ["session-on"],
                    "last_used_at": 1,
                }
            },
        },
    )

    custom_manifest = ConsumerDecisionManifest(
        schema_version=CONSUMER_DECISION_SCHEMA_VERSION,
        run_id="run-on",
        policy_id="consumer-policy-conformance",
        default_fate="accept",
        decisions=(
            ConsumerCandidateDecision(
                run_id="run-on",
                session_id="session-on",
                candidate_cid="cid-deny",
                fate="deny",
                coordinator_trace="trace-consumer-on",
                reason="not useful",
            ),
            ConsumerCandidateDecision(
                run_id="run-on",
                session_id="session-on",
                candidate_cid="cid-block",
                fate="block",
                coordinator_trace="trace-consumer-on",
                reason="unsafe",
            ),
            ConsumerCandidateDecision(
                run_id="run-on",
                session_id="session-on",
                candidate_cid="cid-report",
                fate="report",
                coordinator_trace="trace-consumer-on",
                reason="policy issue",
            ),
        ),
        coordinator_trace="trace-consumer-on",
    )

    runner = module.RealSessionRunner(
        task="backgammon",
        org_id="org-test",
        runs_dir=tmp_path / "runs",
        repo_root=Path(__file__).resolve().parents[1],
        proof=SimpleNamespace(),
        hub_client=SimpleNamespace(),
        leader=SimpleNamespace(),
        contributor_rest=SimpleNamespace(last_job_id=None),
        extract_api_key="extract-key",
        extract_api_key_source="unit-test",
        extract_base_url=None,
        extract_num_ctx=None,
        extract_timeout_s=10,
        consumer_decision_manifest=custom_manifest,
        served_store_host_path=served_store_path,
    )

    on_session = SessionRecord(
        sequence_index=1,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="on",
        phase_group="on",
        phase="RUN_SESSION",
        run_id="run-on",
        session_id="session-on",
    )
    on_record = runner.consumer_gate_outcome(on_session)

    assert on_record is not None
    assert on_record.policy_id == "consumer-policy-conformance"
    assert on_record.consumer_injected_count == 1
    assert on_record.accepted_count == 1
    assert on_record.denied_count == 1
    assert on_record.blocked_count == 1
    assert on_record.reported_count == 1
    assert on_record.served_store_write_confirmed is True
    assert on_record.served_store_missing_accepted == ()
    assert on_record.served_store_nonaccept_leaked == ()

    decisions_payload = json.loads((state_dir / "wevibe-plugin-decisions.json").read_text(encoding="utf-8"))
    assert {entry["memoryID"]: entry["action"] for entry in decisions_payload} == {
        "cid-accept": "accept",
        "cid-deny": "deny",
        "cid-block": "block",
        "cid-report": "report",
    }

    off_session = SessionRecord(
        sequence_index=0,
        model="openrouter/model-a",
        provider_pin="openrouter",
        memory_mode="off",
        phase_group="off_baseline",
        phase="RUN_SESSION",
        run_id="run-off",
        session_id="session-off",
    )
    assert runner.consumer_gate_outcome(off_session) is None
