from scripts.measure_episode_duration import compute_duration_stats, percentile, rho_verdict


def test_percentile_interpolates_distribution() -> None:
    values = [1.0, 2.0, 3.0, 4.0]

    assert percentile(values, 50) == 2.5
    assert percentile(values, 90) == 3.7
    assert percentile(values, 95) == 3.8499999999999996


def test_compute_duration_stats_counts_window_fraction() -> None:
    stats = compute_duration_stats([10, 20, 30, 40, 50], window_minutes=30)

    assert stats.n == 5
    assert stats.min_minutes == 10
    assert stats.p50_minutes == 30
    assert stats.p90_minutes == 46
    assert stats.p95_minutes == 48
    assert stats.max_minutes == 50
    assert stats.within_window_count == 3
    assert stats.within_window_fraction == 0.6
    assert stats.verdict == "NOT ACHIEVABLE"


def test_rho_verdict_boundary_values() -> None:
    assert rho_verdict(0.79, n=100) == "NOT ACHIEVABLE"
    assert rho_verdict(0.80, n=100) == "ACHIEVABLE"
    assert rho_verdict(0.81, n=100) == "ACHIEVABLE"


def test_empty_input_is_undetermined() -> None:
    stats = compute_duration_stats([], window_minutes=1440)

    assert stats.n == 0
    assert stats.min_minutes is None
    assert stats.p50_minutes is None
    assert stats.p90_minutes is None
    assert stats.p95_minutes is None
    assert stats.max_minutes is None
    assert stats.within_window_count == 0
    assert stats.within_window_fraction is None
    assert stats.verdict == "UNDETERMINED-INSUFFICIENT-DATA"


def test_rho_verdict_empty_or_missing_fraction_is_undetermined() -> None:
    assert rho_verdict(None, n=0) == "UNDETERMINED-INSUFFICIENT-DATA"
    assert rho_verdict(0.80, n=0) == "UNDETERMINED-INSUFFICIENT-DATA"
