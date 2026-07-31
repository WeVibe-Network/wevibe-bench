"""Small statistical helpers for benchmark interval reporting.

The benchmark harness intentionally keeps this module stdlib-only: the bench
package currently carries no SciPy, NumPy, or statsmodels dependency, and these
helpers sit on the reporting path where adding a heavy numeric stack would be a
scope expansion.  The implementations below therefore use only Python standard
library arithmetic and deterministic pseudo-random sampling.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import math
import random
import statistics


_BETA_EPS = 3.0e-14
_BETA_FPMIN = 1.0e-300
_BETA_MAX_ITER = 200

# Pre-registered primary endpoint thresholds: the risk-difference floor is the
# lower edge of the sim's own observed paired diffs vs the noOut baseline
# (-0.50...-0.75, RECALL-PIVOT-SPEC.md §2.3/§5), avoiding cherry-picking the
# favorable end; at K=14 archetypes, b=8/c=1 gives one-sided exact p≈0.0195,
# making the reverse-discordant cap satisfiable at the affordable sample size.
PREREG_MIN_RISK_DIFFERENCE = 0.50
PREREG_MAX_REVERSE_DISCORDANT = 1


def _validate_count(successes: int, n: int) -> None:
    if n < 0:
        raise ValueError("n must be non-negative")
    if successes < 0 or successes > n:
        raise ValueError("successes must satisfy 0 <= successes <= n")


def _validate_alpha(alpha: float) -> None:
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must satisfy 0 < alpha < 1")


def _validate_probability(p: float) -> None:
    if not 0.0 <= p <= 1.0:
        raise ValueError("probability must satisfy 0 <= p <= 1")


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - (qab * x / qap)
    if abs(d) < _BETA_FPMIN:
        d = _BETA_FPMIN
    d = 1.0 / d
    h = d

    for m in range(1, _BETA_MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _BETA_FPMIN:
            d = _BETA_FPMIN
        c = 1.0 + aa / c
        if abs(c) < _BETA_FPMIN:
            c = _BETA_FPMIN
        d = 1.0 / d
        h *= d * c

        aa = -((a + m) * (qab + m) * x) / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _BETA_FPMIN:
            d = _BETA_FPMIN
        c = 1.0 + aa / c
        if abs(c) < _BETA_FPMIN:
            c = _BETA_FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _BETA_EPS:
            return h

    raise ArithmeticError("incomplete beta continued fraction did not converge")


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if a <= 0.0 or b <= 0.0:
        raise ValueError("beta parameters must be positive")
    _validate_probability(x)
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0

    log_bt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _beta_continued_fraction(a, b, x) / a
    return 1.0 - (bt * _beta_continued_fraction(b, a, 1.0 - x) / b)


def _beta_quantile(p: float, a: float, b: float) -> float:
    _validate_probability(p)
    if a <= 0.0 or b <= 0.0:
        raise ValueError("beta parameters must be positive")
    if p == 0.0:
        return 0.0
    if p == 1.0:
        return 1.0

    lo = 0.0
    hi = 1.0
    for _ in range(120):
        mid = (lo + hi) / 2.0
        cdf = _regularized_incomplete_beta(a, b, mid)
        if cdf < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def wilson_interval(successes: int, n: int, z: float = 1.959963985) -> tuple[float, float]:
    """Return the two-sided Wilson score interval for a binomial proportion.

    Assumes ``successes`` is a binomial count from ``n`` independent Bernoulli
    trials and ``z`` is the Normal critical value for the desired confidence
    level.  When ``n == 0`` the uninformative interval ``(0.0, 1.0)`` is
    returned.
    """
    _validate_count(successes, n)
    if z <= 0.0:
        raise ValueError("z must be positive")
    if n == 0:
        return (0.0, 1.0)

    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt((phat * (1.0 - phat) / n) + (z2 / (4.0 * n * n)))
    return (max(0.0, center - margin), min(1.0, center + margin))


def clopper_pearson_interval(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Return the exact two-sided Clopper-Pearson binomial proportion interval.

    The statistic is the equal-tailed interval obtained by inverting exact
    binomial tests through Beta quantiles, assuming independent Bernoulli trials.
    Edge cases are handled exactly: zero successes gives lower bound ``0.0``;
    all successes gives upper bound ``1.0``; ``n == 0`` returns ``(0.0, 1.0)``.
    """
    _validate_count(successes, n)
    _validate_alpha(alpha)
    if n == 0:
        return (0.0, 1.0)

    lower = 0.0 if successes == 0 else _beta_quantile(alpha / 2.0, successes, n - successes + 1)
    upper = 1.0 if successes == n else _beta_quantile(1.0 - alpha / 2.0, successes + 1, n - successes)
    return (lower, upper)


def clopper_pearson_upper(successes: int, n: int, alpha: float = 0.05) -> float:
    """Return the exact one-sided Clopper-Pearson upper binomial bound.

    The statistic is the upper confidence limit from the one-sided exact
    binomial inversion, assuming independent Bernoulli trials.  For zero
    successes it reduces to ``1 - alpha ** (1 / n)`` for ``n > 0``.
    """
    _validate_count(successes, n)
    _validate_alpha(alpha)
    if n == 0:
        return 1.0
    if successes == n:
        return 1.0
    return _beta_quantile(1.0 - alpha, successes + 1, n - successes)


# Six non-tied paired observations all in one direction are the hard arithmetic
# floor for two-sided p < 0.05: n=5 gives 2/32 = 0.0625, n=6 gives 2/64 = 0.03125.
def exact_paired_sign_test(pairs: Sequence[tuple[float, float]]) -> dict[str, int | float]:
    """Return a two-sided exact binomial sign test for paired observations.

    The statistic counts positive and negative paired differences, drops exact
    ties, and uses a Binomial(n_effective, 0.5) null with an exact two-sided tail.
    Assumes independent pairs and a symmetric null where positive/negative signs
    are equally likely after ties are removed.
    """
    n_positive = 0
    n_negative = 0
    n_ties = 0
    for before, after in pairs:
        if after > before:
            n_positive += 1
        elif after < before:
            n_negative += 1
        else:
            n_ties += 1

    n_effective = n_positive + n_negative
    if n_effective == 0:
        p_value = 1.0
    else:
        observed = min(n_positive, n_negative)
        tail = sum(math.comb(n_effective, k) for k in range(observed + 1)) / (2**n_effective)
        p_value = min(1.0, 2.0 * tail)

    return {
        "n_pairs": len(pairs),
        "n_effective": n_effective,
        "n_positive": n_positive,
        "n_negative": n_negative,
        "n_ties": n_ties,
        "p_value": p_value,
    }


def mcnemar_exact(b: int, c: int, alternative: str = "greater") -> dict[str, int | float]:
    """Return the exact conditional McNemar binomial test for discordant pairs.

    The statistic conditions on the discordant total ``b + c`` and tests whether
    predicted-direction discordance ``b`` is unusually large under
    Binomial(n_discordant, 0.5).  ``alternative='greater'`` is the pre-registered
    directional test; ``'two-sided'`` returns the exact doubled smaller tail.
    Assumes independent paired binary units.  The odds-ratio convention is
    ``math.inf`` when ``c == 0`` and ``b > 0``, and ``1.0`` when no discordant
    pairs exist; when no discordant pairs exist no Clopper-Pearson interval keys
    are returned.
    """
    if b < 0 or c < 0:
        raise ValueError("discordant counts must be non-negative")
    if alternative not in {"greater", "two-sided"}:
        raise ValueError("alternative must be 'greater' or 'two-sided'")

    n_discordant = b + c
    if n_discordant == 0:
        return {
            "n_discordant": 0,
            "b": b,
            "c": c,
            "p_value": 1.0,
            "odds_ratio": 1.0,
            "discordant_prop_b": 0.0,
        }

    denominator = 2**n_discordant
    if alternative == "greater":
        p_value = sum(math.comb(n_discordant, k) for k in range(b, n_discordant + 1)) / denominator
    else:
        observed = min(b, c)
        tail = sum(math.comb(n_discordant, k) for k in range(observed + 1)) / denominator
        p_value = min(1.0, 2.0 * tail)

    ci_low, ci_high = clopper_pearson_interval(b, n_discordant)
    return {
        "n_discordant": n_discordant,
        "b": b,
        "c": c,
        "p_value": p_value,
        "odds_ratio": math.inf if c == 0 else b / c,
        "discordant_prop_b": b / n_discordant,
        "ci_low": ci_low,
        "ci_high": ci_high,
    }


def paired_binary_contrast(pairs: Sequence[tuple[bool, bool]]) -> dict[str, int | float]:
    """Return a paired binary policy contrast with exact McNemar significance.

    The statistic tabulates paired survival outcomes as ``both_survive``,
    predicted-direction discordance ``b`` (survives under no-outcome policy, dies
    under shipped policy), reverse discordance ``c``, and ``both_die``.  It then
    reports marginal survival rates and the paired risk difference
    ``no_outcome_rate - shipped_rate``.  Assumes one independent pair per unit of
    analysis, with both policies replayed over the same event log.
    """
    both_survive = 0
    b = 0
    c = 0
    both_die = 0
    for survived_no_outcome, survived_shipped in pairs:
        if survived_no_outcome and survived_shipped:
            both_survive += 1
        elif survived_no_outcome and not survived_shipped:
            b += 1
        elif not survived_no_outcome and survived_shipped:
            c += 1
        else:
            both_die += 1

    n_pairs = len(pairs)
    no_outcome_rate = (both_survive + b) / n_pairs if n_pairs else 0.0
    shipped_rate = (both_survive + c) / n_pairs if n_pairs else 0.0
    result = {
        "n_pairs": n_pairs,
        "both_survive": both_survive,
        "b": b,
        "c": c,
        "both_die": both_die,
        "no_outcome_rate": no_outcome_rate,
        "shipped_rate": shipped_rate,
        "risk_difference": no_outcome_rate - shipped_rate,
    }
    result.update(mcnemar_exact(b, c))
    return result


def meets_minimum_effect(
    contrast: dict,
    min_risk_difference: float = PREREG_MIN_RISK_DIFFERENCE,
    max_reverse_discordant: int = PREREG_MAX_REVERSE_DISCORDANT,
    alpha: float = 0.05,
) -> dict[str, bool | float]:
    """Return the pre-registered paired binary endpoint decision rule.

    The rule separately requires the pre-registered direction ``b > c``, a risk
    difference at least ``min_risk_difference``, reverse discordance no larger
    than ``max_reverse_discordant``, and one-sided McNemar significance
    ``p_value < alpha``.  The pre-registered direction licenses the one-sided
    test.  Defaults encode the primary endpoint values documented by
    ``PREREG_MIN_RISK_DIFFERENCE`` and ``PREREG_MAX_REVERSE_DISCORDANT``.
    """
    direction_ok = contrast["b"] > contrast["c"]
    effect_ok = contrast["risk_difference"] >= min_risk_difference
    reverse_ok = contrast["c"] <= max_reverse_discordant
    significant = contrast["p_value"] < alpha
    passes = direction_ok and effect_ok and reverse_ok and significant
    return {
        "direction_ok": direction_ok,
        "effect_ok": effect_ok,
        "reverse_ok": reverse_ok,
        "significant": significant,
        "passes": passes,
        "min_risk_difference": min_risk_difference,
        "max_reverse_discordant": max_reverse_discordant,
        "alpha": alpha,
    }


def _mean(values: list[float]) -> float:
    return statistics.fmean(values)


def _percentile(sorted_values: list[float], p: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = p * (len(sorted_values) - 1)
    lo = math.floor(position)
    hi = math.ceil(position)
    if lo == hi:
        return sorted_values[lo]
    weight = position - lo
    return (sorted_values[lo] * (1.0 - weight)) + (sorted_values[hi] * weight)


def cluster_bootstrap_ci(
    clusters: Sequence[Sequence[float]],
    statistic: Callable[[list[float]], float] = _mean,
    iterations: int = 10000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Return a percentile bootstrap confidence interval over whole clusters.

    The statistic is the requested function evaluated on samples formed by
    resampling entire clusters with replacement, then flattening observations.
    Assumes clusters are independent sessions; resampling whole clusters, rather
    than rows, preserves within-session correlation.
    """
    _validate_alpha(alpha)
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    if not clusters:
        raise ValueError("clusters must not be empty")
    if any(len(cluster) == 0 for cluster in clusters):
        raise ValueError("clusters must not contain empty clusters")

    rng = random.Random(seed)
    cluster_lists = [list(cluster) for cluster in clusters]
    estimates: list[float] = []
    for _ in range(iterations):
        sample: list[float] = []
        for _ in cluster_lists:
            sample.extend(rng.choice(cluster_lists))
        estimates.append(statistic(sample))

    estimates.sort()
    return (_percentile(estimates, alpha / 2.0), _percentile(estimates, 1.0 - (alpha / 2.0)))


def chapman_estimator(n1: int, n2: int, m: int) -> float:
    """Return Chapman's bias-corrected capture-recapture total estimate.

    The statistic is ``((n1 + 1) * (n2 + 1) / (m + 1)) - 1``.  It is preferred
    over Lincoln-Petersen here because our log sources are positively dependent
    (one crash loses several records at once), which biases Lincoln-Petersen LOW;
    use this result as a LOWER BOUND on the true total, i.e. an upper bound on
    undercount.
    """
    if n1 < 0 or n2 < 0 or m < 0:
        raise ValueError("capture counts must be non-negative")
    if m > n1 or m > n2:
        raise ValueError("overlap m must not exceed either capture count")
    return (((n1 + 1) * (n2 + 1)) / (m + 1)) - 1.0


def intra_class_correlation(groups: Sequence[Sequence[float]]) -> float | None:
    """Return one-way ANOVA ICC(1) for grouped observations.

    The statistic estimates within-group clustering from a one-way random-effects
    ANOVA model, for justifying or refusing pooling across archetypes.  Assumes
    independent groups with at least two observations per estimable comparison;
    returns ``None`` when undefined, including fewer than two groups or zero total
    variance.
    """
    non_empty = [list(group) for group in groups if len(group) > 0]
    if len(non_empty) < 2:
        return None

    values = [value for group in non_empty for value in group]
    grand_mean = statistics.fmean(values)
    total_ss = sum((value - grand_mean) ** 2 for value in values)
    if total_ss == 0.0:
        return None

    n_total = len(values)
    k = len(non_empty)
    if n_total <= k:
        return None

    ss_between = sum(len(group) * (statistics.fmean(group) - grand_mean) ** 2 for group in non_empty)
    ss_within = total_ss - ss_between
    df_between = k - 1
    df_within = n_total - k
    ms_between = ss_between / df_between
    ms_within = ss_within / df_within
    mean_group_size = n_total / k
    denom = ms_between + ((mean_group_size - 1.0) * ms_within)
    if denom == 0.0:
        return None
    return (ms_between - ms_within) / denom
