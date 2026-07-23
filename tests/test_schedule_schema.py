"""Focused automated tests for the arbitrary schedule schema (Phase A).

Tests cover:
- Arbitrary interleavings and repeated blocks
- Canon provider pins with UNKNOWN/UNORDERED status
- Parser validation (valid and invalid inputs)
- Deterministic schedule fingerprint
- Active-path migration (schedule-only, no backward compat)
- Legacy schema-field removal behavior
"""

from __future__ import annotations

import json
import pytest

from wevibe_bench.config import (
    BACKGAMMON_SCORED_LADDER_ROSTER,
    BACKGAMMON_LADDER_SCHEMA_VERSION,
    BenchmarkSchedule,
    BenchmarkWave,
    
    backgammon_ladder_roster_fingerprint,
    backgammon_scored_ladder_roster,
    benchmark_schedule_fingerprint,
    parse_benchmark_schedule,
    RunConfig,
    _default_benchmark_schedule,
)


# ---------------------------------------------------------------------------
# BenchmarkWave validation
# ---------------------------------------------------------------------------


class TestBenchmarkWave:
    """Tests for BenchmarkWave validation."""

    def test_valid_wave_defaults(self) -> None:
        """A minimal valid wave uses sensible defaults."""
        wave = BenchmarkWave(
            wave_id="test-wave",
            models=("model-a", "model-b"),
        )
        assert wave.tier == "UNKNOWN"
        assert wave.memory_modes == ("off", "on")
        # validate() should not raise
        wave.validate()

    def test_valid_wave_custom_tier(self) -> None:
        """A wave can use CEILING/BRACKET/FLOOR tiers from variance policy."""
        for tier in ("CEILING", "BRACKET", "FLOOR", "UNKNOWN", "UNORDERED"):
            wave = BenchmarkWave(
                wave_id=f"wave-{tier}",
                models=("model-x",),
                tier=tier,
            )
            wave.validate()  # should not raise

    def test_invalid_wave_blank_id(self) -> None:
        """A wave with blank wave_id raises."""
        with pytest.raises(RuntimeError, match="wave_id must be non-empty"):
            BenchmarkWave(wave_id="", models=("model-a",)).validate()

    def test_invalid_wave_no_models(self) -> None:
        """A wave with no models raises."""
        with pytest.raises(RuntimeError, match="has no models"):
            BenchmarkWave(wave_id="w", models=()).validate()

    def test_invalid_wave_blank_model(self) -> None:
        """A wave with a blank model name raises."""
        with pytest.raises(RuntimeError, match="has blank model"):
            BenchmarkWave(wave_id="w", models=("valid", "", "also-valid")).validate()

    def test_invalid_wave_unknown_tier(self) -> None:
        """A wave with an unknown tier raises."""
        with pytest.raises(RuntimeError, match="tier .* not in"):
            BenchmarkWave(wave_id="w", models=("m",), tier="GODLIKE").validate()

    def test_invalid_wave_unknown_memory_mode(self) -> None:
        """A wave with an unknown memory_mode raises."""
        with pytest.raises(RuntimeError, match="has unknown memory_mode"):
            BenchmarkWave(wave_id="w", models=("m",), memory_modes=("on", "off", "experimental")).validate()


# ---------------------------------------------------------------------------
# BenchmarkSchedule validation
# ---------------------------------------------------------------------------


class TestBenchmarkSchedule:
    """Tests for BenchmarkSchedule validation."""

    def test_default_schedule_has_canon_roster(self) -> None:
        """The default schedule uses the current canon roster (UNKNOWN tiers)."""
        sched = _default_benchmark_schedule()
        assert len(sched.waves) == 1
        wave = sched.waves[0]
        assert wave.wave_id == "baseline"
        assert wave.models == ("z-ai/glm-5.2", "xiaomi/mimo-v2.5-pro", "tencent/hy3")
        assert wave.tier == "UNKNOWN"
        assert wave.memory_modes == ("off", "on")
        assert sched.schema_version == 1

    def test_all_models_deduplicates_preserving_order(self) -> None:
        """all_models() deduplicates across waves, preserving first-seen order."""
        sched = BenchmarkSchedule(
            waves=(
                BenchmarkWave(wave_id="w1", models=("a", "b")),
                BenchmarkWave(wave_id="w2", models=("b", "c")),  # b repeated
                BenchmarkWave(wave_id="w3", models=("a", "d")),  # a repeated
            ),
        )
        assert sched.all_models() == ("a", "b", "c", "d")

    def test_empty_schedule_raises(self) -> None:
        """A schedule with no waves raises."""
        with pytest.raises(RuntimeError, match="has no waves"):
            BenchmarkSchedule().validate()

    def test_no_off_wave_raises(self) -> None:
        """A schedule without any 'off' wave raises."""
        sched = BenchmarkSchedule(
            waves=(
                BenchmarkWave(wave_id="w1", models=("a",), memory_modes=("on",)),
            ),
        )
        with pytest.raises(RuntimeError, match="must include at least one wave"):
            sched.validate()

    def test_duplicate_wave_id_raises(self) -> None:
        """A schedule with duplicate wave_id raises."""
        sched = BenchmarkSchedule(
            waves=(
                BenchmarkWave(wave_id="dup", models=("a",)),
                BenchmarkWave(wave_id="dup", models=("b",)),
            ),
        )
        with pytest.raises(RuntimeError, match="duplicate wave_id"):
            sched.validate()

    def test_to_dict_round_trips(self) -> None:
        """to_dict() produces a dict that parse_benchmark_schedule can reconstruct."""
        original = BenchmarkSchedule(
            waves=(
                BenchmarkWave(wave_id="w1", models=("m1", "m2"), tier="BRACKET"),
                BenchmarkWave(wave_id="w2", models=("m3",), memory_modes=("on",)),
            ),
            schema_version=42,
        )
        d = original.to_dict()
        reconstructed = parse_benchmark_schedule(d)
        assert reconstructed.waves[0].wave_id == "w1"
        assert reconstructed.waves[0].models == ("m1", "m2")
        assert reconstructed.waves[0].tier == "BRACKET"
        assert reconstructed.waves[1].wave_id == "w2"
        assert reconstructed.waves[1].memory_modes == ("on",)
        assert reconstructed.schema_version == 42


# ---------------------------------------------------------------------------
# parse_benchmark_schedule validation
# ---------------------------------------------------------------------------


class TestParseBenchmarkSchedule:
    """Tests for parse_benchmark_schedule."""

    def test_parse_valid_dict(self) -> None:
        """A valid dict is parsed and validated."""
        payload = {
            "schema_version": 2,
            "waves": [
                {
                    "wave_id": "phase1",
                    "models": ["z-ai/glm-5.2"],
                    "tier": "UNKNOWN",
                    "memory_modes": ["off"],
                }
            ],
        }
        sched = parse_benchmark_schedule(payload)
        assert sched.schema_version == 2
        assert sched.waves[0].wave_id == "phase1"
        assert sched.waves[0].memory_modes == ("off",)

    def test_parse_missing_waves_raises(self) -> None:
        """Missing 'waves' key raises."""
        with pytest.raises(RuntimeError, match="'waves' must be an array"):
            parse_benchmark_schedule({})

    def test_parse_non_array_waves_raises(self) -> None:
        """Non-array 'waves' raises."""
        with pytest.raises(RuntimeError, match="'waves' must be an array"):
            parse_benchmark_schedule({"waves": "not-array"})

    def test_parse_non_object_wave_raises(self) -> None:
        """Non-object wave in array raises."""
        with pytest.raises(RuntimeError, match="each wave must be an object"):
            parse_benchmark_schedule({"waves": ["not-an-object"]})

    def test_parse_defaults_when_fields_absent(self) -> None:
        """Missing fields use defaults."""
        payload = {
            "waves": [{"wave_id": "w", "models": ["m"]}],
        }
        sched = parse_benchmark_schedule(payload)
        assert sched.waves[0].tier == "UNKNOWN"
        assert sched.waves[0].memory_modes == ("off", "on")


# ---------------------------------------------------------------------------
# benchmark_schedule_fingerprint
# ---------------------------------------------------------------------------


class TestBenchmarkScheduleFingerprint:
    """Tests for deterministic schedule fingerprint."""

    def test_default_fingerprint_is_deterministic(self) -> None:
        """Same schedule produces same fingerprint."""
        fp1 = benchmark_schedule_fingerprint()
        fp2 = benchmark_schedule_fingerprint()
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256 hex digest

    def test_different_schedules_different_fingerprints(self) -> None:
        """Different schedules produce different fingerprints."""
        sched_a = BenchmarkSchedule(
            waves=(BenchmarkWave(wave_id="a", models=("m1",)),),
        )
        sched_b = BenchmarkSchedule(
            waves=(BenchmarkWave(wave_id="b", models=("m2",)),),
        )
        assert benchmark_schedule_fingerprint(sched_a) != benchmark_schedule_fingerprint(sched_b)

    def test_fingerprint_changes_on_structure_change(self) -> None:
        """Changing wave_id changes the fingerprint."""
        sched1 = BenchmarkSchedule(
            waves=(BenchmarkWave(wave_id="w1", models=("m1",)),),
        )
        sched2 = BenchmarkSchedule(
            waves=(BenchmarkWave(wave_id="w2", models=("m1",)),),
        )
        assert benchmark_schedule_fingerprint(sched1) != benchmark_schedule_fingerprint(sched2)


# ---------------------------------------------------------------------------
# Active-path migration (no backward compat)
# ---------------------------------------------------------------------------


class TestActivePathMigration:
    """Tests confirming schedule is the single active path."""

    def test_runconfig_has_schedule_not_model_ladder(self) -> None:
        """RunConfig no longer has model_ladder attribute."""
        cfg = RunConfig()
        assert not hasattr(cfg, "model_ladder")

    def test_runconfig_schedule_is_required(self) -> None:
        """RunConfig requires a schedule (no None default)."""
        cfg = RunConfig()
        assert cfg.schedule is not None
        assert len(cfg.schedule.waves) > 0

    def test_runconfig_to_dict_has_schedule_not_model_ladder(self) -> None:
        """to_dict() outputs schedule, not model_ladder."""
        cfg = RunConfig()
        d = cfg.to_dict()
        assert "schedule" in d
        assert "model_ladder" not in d

    def test_runconfig_schedule_all_models_from_default(self) -> None:
        """Default schedule.all_models() returns canon roster."""
        cfg = RunConfig()
        models = cfg.schedule.all_models()
        assert models == ("z-ai/glm-5.2", "xiaomi/mimo-v2.5-pro", "tencent/hy3")

    def test_runconfig_schedule_all_models_from_custom(self) -> None:
        """Custom schedule.all_models() returns custom models."""
        custom_sched = BenchmarkSchedule(
            waves=(
                BenchmarkWave(wave_id="w1", models=("model-x", "model-y")),
                BenchmarkWave(wave_id="w2", models=("model-z",)),
            ),
        )
        cfg = RunConfig(schedule=custom_sched)
        assert cfg.schedule.all_models() == ("model-x", "model-y", "model-z")


# ---------------------------------------------------------------------------
# Corpus preservation guard
# ---------------------------------------------------------------------------


class TestCorpusPreservationGuard:
    """Tests for legacy schema-field removal."""

    def test_default_schedule_shape(self) -> None:
        """Default schedule serializes to the active schema shape only."""
        sched = _default_benchmark_schedule()
        d = sched.to_dict()
        assert set(d.keys()) == {"schema_version", "waves"}
        assert len(d["waves"]) == 1
        assert set(d["waves"][0].keys()) == {"wave_id", "models", "tier", "memory_modes"}

    def test_runconfig_to_dict_schedule_shape(self) -> None:
        """RunConfig.to_dict() exposes schedule with wave-only schema fields."""
        cfg = RunConfig()
        d = cfg.to_dict()
        assert "schedule" in d
        assert set(d["schedule"].keys()) == {"schema_version", "waves"}
        assert len(d["schedule"]["waves"]) == 1
        assert set(d["schedule"]["waves"][0].keys()) == {"wave_id", "models", "tier", "memory_modes"}


# ---------------------------------------------------------------------------
# Canon roster constants
# ---------------------------------------------------------------------------


class TestCanonRoster:
    """Tests for canon roster constants."""

    def test_backgammon_scored_ladder_roster_exists(self) -> None:
        """BACKGAMMON_SCORED_LADDER_ROSTER is defined and accessible."""
        roster = backgammon_scored_ladder_roster()
        assert len(roster) > 0

    def test_backgammon_ladder_schema_version_exists(self) -> None:
        """BACKGAMMON_LADDER_SCHEMA_VERSION is defined."""
        assert isinstance(BACKGAMMON_LADDER_SCHEMA_VERSION, int)
        assert BACKGAMMON_LADDER_SCHEMA_VERSION >= 1

    def test_backgammon_ladder_roster_fingerprint_deterministic(self) -> None:
        """Fingerprint is deterministic across calls."""
        fp1 = backgammon_ladder_roster_fingerprint()
        fp2 = backgammon_ladder_roster_fingerprint()
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Arbitrary interleavings and repeated blocks
# ---------------------------------------------------------------------------


class TestArbitraryInterleavings:
    """Tests for arbitrary model-capability interleavings and repeated blocks."""

    def test_ascending_then_descending_sequence(self) -> None:
        """strong→weak→strong→weak interleaving is valid."""
        sched = BenchmarkSchedule(
            waves=(
                BenchmarkWave(wave_id="w1", models=("strong-a",)),
                BenchmarkWave(wave_id="w2", models=("weak-b",)),
                BenchmarkWave(wave_id="w3", models=("strong-c",)),
                BenchmarkWave(wave_id="w4", models=("weak-d",)),
            ),
        )
        sched.validate()
        assert sched.all_models() == ("strong-a", "weak-b", "strong-c", "weak-d")

    def test_repeated_blocks(self) -> None:
        """Repeated model blocks (same model in multiple waves) are valid."""
        sched = BenchmarkSchedule(
            waves=(
                BenchmarkWave(wave_id="w1", models=("model-x",)),
                BenchmarkWave(wave_id="w2", models=("model-x",)),
                BenchmarkWave(wave_id="w3", models=("model-x",)),
            ),
        )
        sched.validate()
        # all_models() deduplicates: model-x appears once
        assert sched.all_models() == ("model-x",)

    def test_mixed_memory_modes(self) -> None:
        """Waves can have different memory_modes."""
        sched = BenchmarkSchedule(
            waves=(
                BenchmarkWave(wave_id="off-only", models=("m1",), memory_modes=("off",)),
                BenchmarkWave(wave_id="both", models=("m2",), memory_modes=("off", "on")),
                BenchmarkWave(wave_id="on-only", models=("m3",), memory_modes=("on",)),
            ),
        )
        sched.validate()
        assert sched.waves[0].memory_modes == ("off",)
        assert sched.waves[1].memory_modes == ("off", "on")
        assert sched.waves[2].memory_modes == ("on",)

    def test_large_interleaving(self) -> None:
        """A large interleaving with many waves validates."""
        waves = tuple(
            BenchmarkWave(wave_id=f"w{i}", models=(f"model-{i:03d}",))
            for i in range(50)
        )
        sched = BenchmarkSchedule(waves=waves)
        sched.validate()
        assert len(sched.all_models()) == 50
