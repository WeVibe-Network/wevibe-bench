from __future__ import annotations

import pytest

from wevibe_bench.stats import (
    PREREG_MAX_REVERSE_DISCORDANT,
    PREREG_MIN_RISK_DIFFERENCE,
    chapman_estimator,
    clopper_pearson_interval,
    clopper_pearson_upper,
    cluster_bootstrap_ci,
    exact_paired_sign_test,
    intra_class_correlation,
    mcnemar_exact,
    meets_minimum_effect,
    paired_binary_contrast,
    wilson_interval,
)


@pytest.mark.parametrize(
    ("n", "expected_rounded"),
    [(6, 0.3930), (10, 0.2589), (13, 0.2057), (14, 0.1926)],
)
def test_clopper_pearson_upper_zero_success_closed_form(n: int, expected_rounded: float) -> None:
    observed = clopper_pearson_upper(0, n, 0.05)
    assert observed == pytest.approx(1 - 0.05 ** (1 / n), abs=1e-12)
    assert observed == pytest.approx(expected_rounded, abs=0.0002)


def test_clopper_pearson_upper_n14_is_smallest_below_twenty_percent() -> None:
    assert clopper_pearson_upper(0, 13, 0.05) >= 0.20
    assert clopper_pearson_upper(0, 14, 0.05) < 0.20


def test_exact_paired_sign_test_hard_arithmetic_floor() -> None:
    five = exact_paired_sign_test([(float(i), float(i + 1)) for i in range(5)])
    six = exact_paired_sign_test([(float(i), float(i + 1)) for i in range(6)])

    assert five["p_value"] == 2 / 32
    assert six["p_value"] == 2 / 64


def test_exact_paired_sign_test_drops_ties() -> None:
    result = exact_paired_sign_test([(1.0, 2.0), (2.0, 1.0), (3.0, 3.0), (4.0, 4.0)])

    assert result == {
        "n_pairs": 4,
        "n_effective": 2,
        "n_positive": 1,
        "n_negative": 1,
        "n_ties": 2,
        "p_value": 1.0,
    }


@pytest.mark.parametrize(
    ("b", "c", "alternative", "expected"),
    [
        (6, 0, "greater", 1 / 64),
        (8, 1, "greater", 10 / 512),
        (6, 0, "two-sided", 2 / 64),
    ],
)
def test_mcnemar_exact_hand_computed_p_value_anchors(
    b: int, c: int, alternative: str, expected: float
) -> None:
    result = mcnemar_exact(b, c, alternative=alternative)

    assert result["p_value"] == pytest.approx(expected, abs=1e-12)


def test_mcnemar_exact_no_discordant_pairs_returns_no_interval() -> None:
    result = mcnemar_exact(0, 0)

    assert result == {
        "n_discordant": 0,
        "b": 0,
        "c": 0,
        "p_value": 1.0,
        "odds_ratio": 1.0,
        "discordant_prop_b": 0.0,
    }
    assert "ci_low" not in result
    assert "ci_high" not in result


def test_mcnemar_exact_zero_reverse_discordant_odds_ratio_is_infinity() -> None:
    result = mcnemar_exact(6, 0)

    assert result["odds_ratio"] == float("inf")


def test_mcnemar_exact_clopper_pearson_interval_matches_direct_call() -> None:
    result = mcnemar_exact(8, 1)
    expected_low, expected_high = clopper_pearson_interval(8, 9)

    assert result["ci_low"] == pytest.approx(expected_low, abs=1e-12)
    assert result["ci_high"] == pytest.approx(expected_high, abs=1e-12)


def test_paired_binary_contrast_known_table_marginals_and_risk_difference() -> None:
    pairs = [
        (True, True),
        (True, True),
        (True, False),
        (True, False),
        (True, False),
        (False, True),
        (False, False),
        (False, False),
    ]

    result = paired_binary_contrast(pairs)

    assert result["n_pairs"] == 8
    assert result["both_survive"] == 2
    assert result["b"] == 3
    assert result["c"] == 1
    assert result["both_die"] == 2
    assert result["no_outcome_rate"] == pytest.approx(5 / 8, abs=1e-12)
    assert result["shipped_rate"] == pytest.approx(3 / 8, abs=1e-12)
    assert result["risk_difference"] == pytest.approx(2 / 8, abs=1e-12)
    assert result["n_discordant"] == 4
    assert result["p_value"] == pytest.approx(5 / 16, abs=1e-12)


def test_meets_minimum_effect_passes_pre_registered_rule() -> None:
    contrast = paired_binary_contrast([(True, False)] * 8 + [(False, True)] + [(False, False)] * 5)

    result = meets_minimum_effect(contrast)

    assert result["direction_ok"] is True
    assert result["effect_ok"] is True
    assert result["reverse_ok"] is True
    assert result["significant"] is True
    assert result["passes"] is True
    assert result["min_risk_difference"] == PREREG_MIN_RISK_DIFFERENCE
    assert result["max_reverse_discordant"] == PREREG_MAX_REVERSE_DISCORDANT


def test_meets_minimum_effect_fails_direction_independently() -> None:
    contrast = {"b": 1, "c": 1, "risk_difference": 0.50, "p_value": 0.01}

    result = meets_minimum_effect(contrast)

    assert result["direction_ok"] is False
    assert result["effect_ok"] is True
    assert result["reverse_ok"] is True
    assert result["significant"] is True
    assert result["passes"] is False


def test_meets_minimum_effect_fails_effect_independently() -> None:
    contrast = {"b": 8, "c": 1, "risk_difference": 0.49, "p_value": 0.01}

    result = meets_minimum_effect(contrast)

    assert result["direction_ok"] is True
    assert result["effect_ok"] is False
    assert result["reverse_ok"] is True
    assert result["significant"] is True
    assert result["passes"] is False


def test_meets_minimum_effect_fails_reverse_independently() -> None:
    contrast = {"b": 8, "c": 2, "risk_difference": 0.50, "p_value": 0.01}

    result = meets_minimum_effect(contrast)

    assert result["direction_ok"] is True
    assert result["effect_ok"] is True
    assert result["reverse_ok"] is False
    assert result["significant"] is True
    assert result["passes"] is False


def test_meets_minimum_effect_fails_significance_independently() -> None:
    contrast = {"b": 8, "c": 1, "risk_difference": 0.50, "p_value": 0.05}

    result = meets_minimum_effect(contrast)

    assert result["direction_ok"] is True
    assert result["effect_ok"] is True
    assert result["reverse_ok"] is True
    assert result["significant"] is False
    assert result["passes"] is False


def test_wilson_and_clopper_pearson_sanity_at_small_n() -> None:
    successes = 2
    n = 6
    point = successes / n

    wilson = wilson_interval(successes, n)
    cp = clopper_pearson_interval(successes, n)

    assert 0.0 <= wilson[0] <= point <= wilson[1] <= 1.0
    assert 0.0 <= cp[0] <= point <= cp[1] <= 1.0
    assert (cp[1] - cp[0]) >= (wilson[1] - wilson[0])


def test_cluster_bootstrap_ci_is_deterministic_under_fixed_seed() -> None:
    clusters = [[1.0, 2.0], [10.0, 11.0], [20.0, 21.0]]

    first = cluster_bootstrap_ci(clusters, iterations=500, seed=123)
    second = cluster_bootstrap_ci(clusters, iterations=500, seed=123)

    assert first == second


def test_cluster_bootstrap_ci_one_cluster_is_degenerate() -> None:
    interval = cluster_bootstrap_ci([[1.0, 2.0, 3.0]], iterations=100, seed=99)

    assert interval == (2.0, 2.0)


def test_chapman_estimator_textbook_case() -> None:
    assert chapman_estimator(10, 15, 5) == pytest.approx(((11 * 16) / 6) - 1)


def test_intra_class_correlation_degenerate_inputs_return_none() -> None:
    assert intra_class_correlation([]) is None
    assert intra_class_correlation([[1.0], [1.0]]) is None
    assert intra_class_correlation([[2.0, 2.0], [2.0, 2.0]]) is None


def test_intra_class_correlation_perfectly_clustered_is_near_one() -> None:
    assert intra_class_correlation([[1.0, 1.0, 1.0], [10.0, 10.0, 10.0]]) == pytest.approx(1.0)
