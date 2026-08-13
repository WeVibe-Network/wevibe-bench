"""Feedback voice, gradient, and the gate-timeout disposition (WO-FEEDBACK-1).

These pin the three properties that decide what the benchmark actually measures:

* the model hears a PERSON, not a grader (no `[G05]`, no `conformance:`);
* a repeated failure returns NEW information, so the loop has a gradient;
* a killed gate is recorded as a killed gate, never as a model failure.

The artifacts keep the grader ids in every case — the humanising is a property
of the delivered TEXT only, and a test that let it leak into `failed_gates`
would be pinning the wrong thing.
"""

from __future__ import annotations

import json

from wevibe_bench.adapters.backgammon import BackgammonRunner


# ── voice ────────────────────────────────────────────────────────────────────


def test_grader_identity_is_stripped_from_delivered_text() -> None:
    """A user does not say "[G05] REQ-HIGHER-DIE"."""
    assert BackgammonRunner._humanize_check("[G05] REQ-HIGHER-DIE — use higher die") == "use higher die"
    assert (
        BackgammonRunner._humanize_check("[F01] REQ-RENDER — page loads, no console errors")
        == "page loads, no console errors"
    )
    # Conformance carries BOTH a phase namespace and a slashed REQ token.
    assert (
        BackgammonRunner._humanize_check(
            'conformance:REQ-TESTID/checker — board renders 30 data-testid "checker" elements'
        )
        == 'board renders 30 data-testid "checker" elements'
    )
    # Edge gates admitted 2026-08-13 use the same [Enn] token shape.
    assert (
        BackgammonRunner._humanize_check("[E01] REQ-DOUBLES — doubles are played as four plies")
        == "doubles are played as four plies"
    )


def test_humanize_never_empties_a_check() -> None:
    """A gate the model cannot be told about is worse than one told awkwardly."""
    # Nothing to strip.
    assert BackgammonRunner._humanize_check("frontend:boot") == "boot"
    assert BackgammonRunner._humanize_check("something plain") == "something plain"
    # Degenerate input must not yield an empty bullet.
    assert BackgammonRunner._humanize_check("[G01]") == "[G01]"
    assert BackgammonRunner._humanize_check("") == ""


def test_pass_verdict_no_longer_truncates_mid_word() -> None:
    """The old 80-char hard slice cut mid-phrase and left a dangling space —
    a tell that no human wrote the line."""
    long_gate = (
        "[G10] REQ-WINCLASS — classifies backgammon when the loser still has a checker "
        "sitting on the bar at the moment the winner bears off the final checker"
    )
    out = BackgammonRunner._build_pass_verdict(newly_passing=[long_gate])
    assert "[G10]" not in out
    assert "REQ-WINCLASS" not in out
    assert not out.rstrip("…").endswith(" "), "must not end on a dangling mid-word space"
    assert out.startswith("That fixed it —")


# ── gradient ─────────────────────────────────────────────────────────────────


def _problems() -> list[dict[str, str]]:
    return [
        {
            "check": "[G05] REQ-HIGHER-DIE — use higher die",
            "expected": "higher die used",
            "observed": "AssertionError: expected 2 to be 4 // Object.is equality",
        },
        {
            "check": "[F14] REQ-ANIM — animation present",
            "expected": "gate passes",
            "observed": "expected false to be true",
        },
    ]


def test_first_failure_states_the_requirement_only() -> None:
    """Inferring the implementation from the requirement is the thing being
    measured; handing over the assertion answers it for free."""
    text = BackgammonRunner._build_feedback_prompt(problems=_problems(), repeat_checks=set())
    assert "- use higher die: FAILING" in text
    assert "- animation present: FAILING" in text
    assert "still seeing" not in text, "no evidence on a first failure"


def test_repeat_failure_returns_new_information() -> None:
    """THE GRADIENT. Previously attempt N and N+1 produced byte-identical text,
    so a failed fix taught the model nothing and it could not tell 'closer'
    from 'no change' across a 3-attempt ceiling."""
    problems = _problems()
    first = BackgammonRunner._build_feedback_prompt(problems=problems, repeat_checks=set())
    repeat = BackgammonRunner._build_feedback_prompt(
        problems=problems,
        repeat_checks={p["check"] for p in problems},
    )
    assert first != repeat, "a repeated failure must not return identical text"
    assert "I'm still seeing: expected 2 to be 4" in repeat
    # Runner furniture is not evidence.
    assert "Object.is equality" not in repeat
    assert "AssertionError" not in repeat


def test_evidence_never_leaks_host_paths() -> None:
    """Stack frames point at gate files the worker cannot read
    (`external_directory: deny`); leaving them in only invites wasted turns."""
    problems = [
        {
            "check": "[E08] REQ-SEQ-DEDUP — sequences are distinct by resulting board",
            "observed": (
                "expected 4 to be 2 at "
                "/Users/x/wevibe-bench/tasks/backgammon/gates/backend/edge/edge-gates.test.ts:171:23"
            ),
        }
    ]
    text = BackgammonRunner._build_feedback_prompt(
        problems=problems, repeat_checks={problems[0]["check"]}
    )
    assert "/Users/" not in text
    assert "edge-gates.test.ts" not in text
    assert "expected 4 to be 2" in text, "the evidence itself must survive"


def test_repeat_matching_is_keyed_on_the_raw_gate_id() -> None:
    """Humanised labels are lossy; the repeat set must match on the same strings
    `failed_gates` carries or the gradient silently never fires."""
    problems = _problems()
    # The HUMANISED label, deliberately — this must NOT be treated as a repeat.
    text = BackgammonRunner._build_feedback_prompt(
        problems=problems, repeat_checks={"use higher die"}
    )
    assert "still seeing" not in text


def test_empty_checks_never_claims_a_clean_run() -> None:
    text = BackgammonRunner._build_feedback_prompt(problems=[], repeat_checks=set())
    assert "FAILING" in text, "a FAIL verdict with no itemised checks is still a failure"


def test_legacy_checks_kwarg_still_works() -> None:
    """Older callers pass a bare list; they must keep working unchanged."""
    text = BackgammonRunner._build_feedback_prompt(checks=["[G01] REQ-INIT — initial position"])
    assert "- initial position: FAILING" in text


# ── the honesty boundary ─────────────────────────────────────────────────────


def test_gate_timeout_is_a_named_disposition_not_a_model_failure() -> None:
    """A killed gate measured NOTHING. Recording it as an ordinary FAIL would
    attribute a harness death to the model under test.

    `GateTimeoutError` was previously raised and never caught anywhere in the
    repo, so a timed-out gate aborted the campaign and the one genuinely
    'impractical, not impossible' event was the outcome the record could not
    express.
    """
    import inspect

    src = inspect.getsource(BackgammonRunner._run_cell_impl)
    assert "except GateTimeoutError" in src, "the timeout must be caught, not propagated"
    assert 'termination_reason = "gate_timeout"' in src
    assert 'attempts_to_green = "GATE_TIMEOUT"' in src
    # The attempt record must NOT invent failures for gates that never ran.
    assert '"failed_gates": []' in src
    assert '"gate_results": None' in src, "None (not published) ≠ [] (published and empty)"


def test_user_event_sidecar_records_kind_and_verbatim_text(tmp_path) -> None:
    """The sidecar is the ONLY place the exact bytes handed to the model are
    preserved; the PROGRESS log carries a length and a fingerprint, not the
    body."""
    runner = BackgammonRunner.__new__(BackgammonRunner)
    runner._progress = lambda _msg: None

    sidecar = tmp_path / "worktree.user-events.jsonl"
    body = "These are still failing — fix the implementation.\n\n- use higher die: FAILING"
    runner._append_user_event(
        run_label="rl", sidecar_path=sidecar, attempt=2, text=body, kind="feedback"
    )

    record = json.loads(sidecar.read_text(encoding="utf-8").strip())
    assert record["kind"] == "feedback"
    assert record["attempt"] == 2
    assert record["text"] == body, "VERBATIM — never re-wrapped or trimmed"
    assert record["chars"] == len(body)
    assert record["text_fp"]
