from __future__ import annotations

import pytest

from wevibe_bench.stats import (
    chapman_estimator,
    clopper_pearson_interval,
    clopper_pearson_upper,
    cluster_bootstrap_ci,
    exact_paired_sign_test,
    intra_class_correlation,
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
