from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from wevibe_bench.adapters.backgammon import BackgammonCellResult
from wevibe_bench.cumulative.catalog import PrivateCatalog, PrivateReviewCard
from wevibe_bench.cumulative.leader_client import LeaderClient
from wevibe_bench.cumulative.prerun import (
    CachedSessionRunner,
    cell_result_to_dict,
    prerun_off_cells,
)
from wevibe_bench.cumulative.progress import progress_from_cell_result
from wevibe_bench.cumulative.sequencer import CumulativeSequencer
from wevibe_bench.cumulative.types import RosterEntry, SessionPhase


class _FakeM2Proof:
    pass


class _FakeHubClient:
    pass


class _FakeLeader:
    ed_pubkey_hex = "f00df00d"

    @staticmethod
    def ed_pub_fp() -> str:
        return "leaderfp8"


def _result(sequence_index: int, *, memory_mode: str = "off") -> BackgammonCellResult:
    return BackgammonCellResult(
        verdict="PASS",
        attempts_to_green=1,
        termination_reason="green",
        conformed=True,
        input_tokens=100 + sequence_index,
        output_tokens=20,
        turns=2,
        wall_seconds=0.25,
        delivery="ok",
        failed_gates=[],
        problems_final=[],
        attempt_reports=[],
        worktree=f"/tmp/worktree-{sequence_index}",
        session_id=f"sess-{sequence_index}",
        memory_mode=memory_mode,
        model=f"openrouter/model-{sequence_index}",
        wall_cost_usd=0.01,
        problems_before=3,
        injected_count=0,
        tool_calls=4,
        test_invocations=1,
        agentic_cycles=2,
    )


class _SequencerRunner:
    def __init__(self, calls: list[tuple[str, int]], active: dict[str, int], lock: threading.Lock) -> None:
        self.calls = calls
        self.active = active
        self.lock = lock

    def prepare_fixture(self, session: Any) -> None:
        self.calls.append(("prepare", int(session.sequence_index)))

    def run_session(self, session: Any) -> BackgammonCellResult:
        sequence_index = int(session.sequence_index)
        self.calls.append(("run", sequence_index))
        with self.lock:
            self.active["current"] += 1
            self.active["max"] = max(self.active["max"], self.active["current"])
        try:
            time.sleep(0.02)
            return _result(sequence_index, memory_mode=str(session.memory_mode))
        finally:
            with self.lock:
                self.active["current"] -= 1

    def extract(self, session: Any) -> dict[str, Any]:
        sequence_index = int(session.sequence_index)
        self.calls.append(("extract", sequence_index))
        return {
            "candidate_refs": [],
            "extraction_job_id": f"job-{sequence_index}",
            "session_id": f"sess-{sequence_index}",
            "extraction_candidate_count": 0,
        }

    def index_ready(self, session: Any) -> bool:
        return True


def _leader_client(tmp_path: Path, review_card: PrivateReviewCard) -> LeaderClient:
    return LeaderClient(
        _FakeM2Proof(),
        _FakeHubClient(),
        _FakeLeader(),
        PrivateCatalog(str(tmp_path / "private.catalog.jsonl")),
        safe_ledger_path=str(tmp_path / "safe-ledger.jsonl"),
        idempotency_ledger_path=str(tmp_path / "idempotency.json"),
        review_card=review_card,
    )


def _sequencer(tmp_path: Path, runner: Any) -> CumulativeSequencer:
    review_card = PrivateReviewCard(str(tmp_path / "private.review.jsonl"))
    roster = [
        RosterEntry(
            model=f"openrouter/model-{index}",
            role="assistant",
            provider_pin="openrouter",
            config_identity={"slot": index},
        )
        for index in range(3)
    ]
    return CumulativeSequencer(
        tmp_path / "manifest.json",
        runner=runner,
        leader_client=_leader_client(tmp_path, review_card),
        review_card=review_card,
        roster=roster,
        seed=17,
        task="backgammon",
        org_id="org-prerun-wiring-test",
        config_fingerprint="cfg-prerun-wiring-test",
        on_budget=1,
    )


def test_off_prerun_replays_through_real_sequencer_and_leaves_on_serial(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []
    active = {"current": 0, "max": 0}
    lock = threading.Lock()
    runner = _SequencerRunner(calls, active, lock)
    sequencer = _sequencer(tmp_path, runner)
    manifest = getattr(sequencer, "_manifest")
    pending_off = [
        session
        for session in manifest.session_records
        if session.phase_group == "off_baseline" and session.phase in {"PREPARE_FIXTURE", "RUN_SESSION"}
    ]

    results = prerun_off_cells(
        pending_off,
        lambda _session: _SequencerRunner(calls, active, lock),
        tmp_path / "prerun",
        concurrency=3,
    )
    sequencer._runner = CachedSessionRunner(runner, tmp_path / "prerun")
    review = sequencer.step_until_review()

    assert [item["status"] for item in results] == ["done", "done", "done"]
    assert active["max"] > 1
    assert review["status"] == "awaiting_coordinator_review"
    assert review["sequence_index"] == 0
    assert calls.count(("extract", 0)) == 1
    assert ("prepare", 0) in calls
    assert ("run", 0) in calls
    assert calls.count(("prepare", 0)) == 1
    assert calls.count(("run", 0)) == 1

    manifest_payload = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    first_record = manifest_payload["session_records"][0]
    assert manifest_payload["current_index"] == 0
    assert first_record["phase"] == SessionPhase.AWAIT_COORDINATOR_REVIEW.value
    expected_progress = progress_from_cell_result(_result(0)).to_dict()
    expected_progress["extraction_candidate_count"] = 0
    assert first_record["progress"] == expected_progress
    assert first_record["extraction_job_id"] == "job-0"
    assert manifest_payload["session_records"][3]["phase_group"] == "on"
    assert ("prepare", 3) not in calls
    assert ("run", 3) not in calls


def test_prerun_wiring_resume_done_checkpoint_skips_cell(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []
    active = {"current": 0, "max": 0}
    lock = threading.Lock()
    runner = _SequencerRunner(calls, active, lock)
    sequencer = _sequencer(tmp_path, runner)
    manifest = getattr(sequencer, "_manifest")
    checkpoint_dir = tmp_path / "prerun"
    checkpoint_dir.mkdir()

    done_result = _result(1)
    checkpoint = {
        "sequence_index": 1,
        "model": "openrouter/model-1",
        "run_label": "",
        "run_id": "",
        "telemetry": cell_result_to_dict(done_result),
        "progress": progress_from_cell_result(done_result).to_dict(),
        "status": "done",
        "error": "",
        "started_at": "2026-07-30T00:00:00Z",
        "finished_at": "2026-07-30T00:00:01Z",
    }
    (checkpoint_dir / "prerun-1.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    pending_off = [session for session in manifest.session_records if session.phase_group == "off_baseline"]

    results = prerun_off_cells(
        pending_off,
        lambda _session: _SequencerRunner(calls, active, lock),
        checkpoint_dir,
        concurrency=3,
    )

    assert [item["status"] for item in results] == ["done", "done", "done"]
    assert ("prepare", 1) not in calls
    assert ("run", 1) not in calls
    assert ("prepare", 0) in calls
    assert ("run", 0) in calls
    assert ("prepare", 2) in calls
    assert ("run", 2) in calls
