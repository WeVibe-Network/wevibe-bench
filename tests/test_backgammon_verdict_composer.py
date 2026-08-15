from __future__ import annotations

from wevibe_bench.adapters.backgammon import BackgammonRunner


def test_build_pass_verdict_formats_and_sanitizes() -> None:
    # WO-FEEDBACK-1: grader identity is STRIPPED from delivered text. A user
    # does not say "[G01]". The tokens survive in `failed_gates` and the roster,
    # which is where anything that needs to address a gate precisely reads them.
    assert BackgammonRunner._build_pass_verdict(newly_passing=[]) == ""
    assert (
        BackgammonRunner._build_pass_verdict(newly_passing=["[G01] API"]) == "That fixed it — API works now."
    )
    assert (
        BackgammonRunner._build_pass_verdict(newly_passing=["[G01] A", "[G02] B"])
        == "That fixed it — A, B all pass now."
    )
    assert (
        BackgammonRunner._build_pass_verdict(newly_passing=["[G01] A", "[G02] B", "[G03] C", "[G04] D"])
        == "That fixed it — A, B, C and 1 more all pass now."
    )

    long_label = "X" * 100
    verdict = BackgammonRunner._build_pass_verdict(
        newly_passing=[f"  [G99]    spaced\tname   \nignored second line", long_label]
    )
    # Over-length labels now trim at a WORD boundary and mark the cut; the old
    # hard slice ended mid-word on a dangling space, which is a tell that no
    # human wrote the line.
    assert verdict == f"That fixed it — spaced name, {'X' * 80}… all pass now."


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
    # Gate tokens stripped; dedup, first-line-only and length capping all still
    # apply — they are what this test has always really guarded.
    assert lines[2:] == [
        "- duplicate check: FAILING",
        "- line1: FAILING",
        f"- {'Y' * 120}…: FAILING",
    ]

    fallback = BackgammonRunner._build_feedback_prompt(checks=[])
    assert fallback == (
        "These are still failing — fix the implementation so they pass. Do not explain, just edit the code.\n"
        "\n"
        "- something is still broken but the checks came back empty: FAILING"
    )
