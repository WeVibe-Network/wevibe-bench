from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from wevibe_bench.adapters.backgammon import BackgammonRunner

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from backgammon_sxe import _build_substrate_events  # noqa: E402


def _sync_feedback_patterns() -> tuple[re.Pattern[str], re.Pattern[str]]:
    clone_src = Path(__file__).resolve().parents[1] / "scaffold" / "wevibe-mcp-clone" / "src" / "failure-episodes.ts"
    if not clone_src.is_file():
        raise AssertionError(
            "sync source missing: scaffold/wevibe-mcp-clone/src/failure-episodes.ts; "
            "bench clone must exist for regex-sync tripwire"
        )

    source = clone_src.read_text(encoding="utf-8")
    failure_match = re.search(
        r"USER_FEEDBACK_FAILURE_PATTERN\s*=\s*/((?:[^/\\]|\\.)+)/([a-z]*)",
        source,
    )
    pass_match = re.search(
        r"USER_FEEDBACK_PASS_PATTERN\s*=\s*/((?:[^/\\]|\\.)+)/([a-z]*)",
        source,
    )
    if failure_match is None or pass_match is None:
        raise AssertionError("failed to parse USER_FEEDBACK_*_PATTERN literals from failure-episodes.ts")

    failure_body, failure_flags = failure_match.group(1), failure_match.group(2)
    pass_body, pass_flags = pass_match.group(1), pass_match.group(2)
    if failure_flags != "i" or pass_flags != "i":
        raise AssertionError(
            "expected USER_FEEDBACK_FAILURE/PASS_PATTERN flags to be exactly 'i' "
            f"(got failure={failure_flags!r}, pass={pass_flags!r})"
        )

    return re.compile(failure_body, re.IGNORECASE), re.compile(pass_body, re.IGNORECASE)


def test_verdict_and_feedback_regex_sync_matrix() -> None:
    failure_re, pass_re = _sync_feedback_patterns()

    one_gate = BackgammonRunner._build_pass_verdict(newly_passing=["[G01] API wiring"])
    assert pass_re.search(one_gate)
    assert not failure_re.search(one_gate)

    two_gate = BackgammonRunner._build_pass_verdict(newly_passing=["[G01] A", "[G02] B"])
    assert pass_re.search(two_gate)
    assert not failure_re.search(two_gate)

    five_gate = BackgammonRunner._build_pass_verdict(
        newly_passing=["[G01] A", "[G02] B", "[G03] C", "[G04] D", "[G05] E"]
    )
    assert pass_re.search(five_gate)
    assert not failure_re.search(five_gate)

    no_pass_header = BackgammonRunner._build_feedback_prompt(checks=["[G02] B"], had_pass_verdict=False)
    assert failure_re.search(no_pass_header)
    assert not pass_re.search(no_pass_header)

    had_pass_header = BackgammonRunner._build_feedback_prompt(checks=["[G02] B"], had_pass_verdict=True)
    assert failure_re.search(had_pass_header)
    assert not pass_re.search(had_pass_header)

    full_feedback = BackgammonRunner._build_feedback_prompt(
        checks=["[G02] B", "[G07] websocket reconnect", "[G09] score persist"]
    )
    assert failure_re.search(full_feedback)
    assert not pass_re.search(full_feedback)

    fallback_feedback = BackgammonRunner._build_feedback_prompt(checks=[])
    assert failure_re.search(fallback_feedback)
    assert not pass_re.search(fallback_feedback)


def test_build_pass_verdict_formats_and_sanitizes() -> None:
    assert BackgammonRunner._build_pass_verdict(newly_passing=[]) == ""
    assert (
        BackgammonRunner._build_pass_verdict(newly_passing=["[G01] API"]) == "That fixed it — [G01] API works now."
    )
    assert (
        BackgammonRunner._build_pass_verdict(newly_passing=["[G01] A", "[G02] B"])
        == "That fixed it — [G01] A, [G02] B all pass now."
    )
    assert (
        BackgammonRunner._build_pass_verdict(newly_passing=["[G01] A", "[G02] B", "[G03] C", "[G04] D"])
        == "That fixed it — [G01] A, [G02] B, [G03] C and 1 more all pass now."
    )

    long_label = "X" * 100
    verdict = BackgammonRunner._build_pass_verdict(
        newly_passing=[f"  [G99]    spaced\tname   \nignored second line", long_label]
    )
    assert verdict == f"That fixed it — [G99] spaced name, {'X' * 80} all pass now."


def test_build_feedback_prompt_headers_and_invariants() -> None:
    default_header = BackgammonRunner._build_feedback_prompt(checks=["[G01] A"])
    assert default_header.startswith(
        "These are still failing — fix the implementation so they pass. Do not explain, just edit the code."
    )

    had_pass_header = BackgammonRunner._build_feedback_prompt(checks=["[G01] A"], had_pass_verdict=True)
    assert had_pass_header.startswith(
        "The rest are still failing — fix the implementation so they pass. Do not explain, just edit the code."
    )

    long_check = "Y" * 150
    prompt = BackgammonRunner._build_feedback_prompt(
        checks=[" [G1]  duplicate   check  ", "[G1] duplicate check", "[G2] line1\nline2", long_check],
        had_pass_verdict=False,
    )
    lines = prompt.splitlines()
    assert lines[0] == "These are still failing — fix the implementation so they pass. Do not explain, just edit the code."
    assert lines[1] == ""
    assert lines[2:] == [
        "- [G1] duplicate check: FAILING",
        "- [G2] line1: FAILING",
        f"- {'Y' * 120}: FAILING",
    ]

    fallback = BackgammonRunner._build_feedback_prompt(checks=[])
    assert fallback == (
        "These are still failing — fix the implementation so they pass. Do not explain, just edit the code.\n"
        "\n"
        "- (gate runner reported FAIL with no itemised checks): FAILING"
    )


def test_build_substrate_events_preserves_verdict_texts_verbatim(tmp_path: Path) -> None:
    worker_events = tmp_path / "worktree.events.jsonl"
    sidecar = tmp_path / "worktree.user-events.jsonl"

    pass_verdict = "That fixed it — [G01] A, [G03] C all pass now."
    failure_verdict = (
        "The rest are still failing — fix the implementation so they pass. "
        "Do not explain, just edit the code."
    )
    normal_row = "initial prompt"

    sidecar.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "timestamp": 1_700_100_000_001, "attempt": 1, "text": normal_row}),
                json.dumps({"type": "user", "timestamp": 1_700_100_000_002, "attempt": 2, "text": pass_verdict}),
                json.dumps({"type": "user", "timestamp": 1_700_100_000_003, "attempt": 2, "text": failure_verdict}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    worker_events.write_text(
        json.dumps(
            {
                "type": "text",
                "timestamp": 1_700_100_000_100,
                "part": {"text": "assistant output", "time": {"start": 1_700_100_000_101}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events, _, _ = _build_substrate_events(session_dir=tmp_path)
    user_events = [event for event in events if event.get("kind") == "user"]

    assert [event["text"] for event in user_events] == [normal_row, pass_verdict, failure_verdict]
    assert [event["kind"] for event in user_events] == ["user", "user", "user"]
