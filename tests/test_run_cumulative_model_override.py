from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from wevibe_bench import config
from wevibe_bench.adapters.backgammon import build_worker_opencode_config


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_override_is_refused_because_the_auto_rung_is_retired(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare invocation must not resolve the auto-resident rung.

    That rung measures whichever model happens to be loaded and records no
    identity. It is retired, and this is the refusal that makes it unreachable
    rather than merely discouraged — every roster path funnels through
    ``_apply_model_override``.
    """
    module = _load_run_cumulative_module()
    with pytest.raises(SystemExit) as excinfo:
        module._build_roster()
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--model is required" in err
    assert "retired" in err
    # The refusal names what CAN be run; a bare "required" would leave the
    # operator guessing at the spelling of an alias.
    assert "qwen3.6-35b-a3b-bench" in err
    # …and never offers the retired alias as one of them.
    assert "wevibe-bench-worker" not in err


def test_retired_alias_is_refused_by_its_retirement_not_as_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_run_cumulative_module()
    with pytest.raises(SystemExit) as excinfo:
        module._build_roster(model_override="wevibe-bench-worker")
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "retired" in err
    # The alias is spelled correctly — calling it unknown would send the
    # operator hunting for a typo that is not there.
    assert "not a known worker model alias" not in err


def test_model_override_rewrites_single_subject_roster() -> None:
    module = _load_run_cumulative_module()
    rung = config.BACKGAMMON_SCORED_LADDER_ROSTER[0]
    roster, roster_hash = module._build_roster(model_override="qwen3.6-35b-a3b-bench")
    assert len(roster) == 1
    entry = roster[0]
    assert entry.model == "local-llm-proxy/qwen3.6-35b-a3b-bench"
    assert entry.provider_pin == "qwen3.6-35b-a3b-bench"
    # Everything except the model id comes from the frozen rung untouched.
    assert entry.role == rung.role
    assert entry.config_identity == {
        "memory_modes": [str(m) for m in rung.memory_modes],
        "recorded_class": rung.recorded_class,
    }
    assert roster_hash


def test_ladder_fingerprint_is_stable_against_the_live_manifest() -> None:
    """The roster fingerprint is frozen into every campaign manifest.

    ``CumulativeSequencer.__init__`` re-computes it on EVERY launch — not only
    on ``resume`` — and refuses on drift. Editing BACKGAMMON_SCORED_LADDER_ROSTER
    therefore invalidates the live campaign and every OFF baseline inside it, at
    a cost of ~3h per cell to re-measure.

    This pins the value so that change is a failing test with an explanation
    rather than a run that dies at launch. If you are deliberately rebuilding the
    campaign (archive runs/cumulative first), update the constant here.
    """
    assert (
        config.backgammon_ladder_roster_fingerprint()
        == "cb832f9b423af5d64c77f2b7019fca3126241ec636bd80df78a0a55438446bc7"
    )


def test_model_override_accepts_provider_prefixed_form() -> None:
    module = _load_run_cumulative_module()
    bare, _ = module._build_roster(model_override="qwen3.6-35b-a3b-bench")
    prefixed, _ = module._build_roster(
        model_override="local-llm-proxy/qwen3.6-35b-a3b-bench"
    )
    assert [entry.model for entry in prefixed] == [entry.model for entry in bare]


def test_model_override_unknown_alias_exits_loud(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_run_cumulative_module()
    with pytest.raises(SystemExit) as excinfo:
        module._build_roster(model_override="not-a-real-alias")
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "not-a-real-alias" in captured.err
    assert "qwen3.6-35b-a3b-bench" in captured.err


def test_model_override_composes_with_roster_filter() -> None:
    module = _load_run_cumulative_module()
    roster, _ = module._build_roster(
        roster_model="wevibe-bench-worker",
        model_override="qwen3.6-35b-a3b-bench",
    )
    assert [entry.model for entry in roster] == ["local-llm-proxy/qwen3.6-35b-a3b-bench"]


def test_overridden_slug_builds_worker_opencode_config() -> None:
    config = build_worker_opencode_config(
        model="local-llm-proxy/qwen3.6-35b-a3b-bench",
        reasoning_effort=None,
        proxy_base_url="http://host.docker.internal:4545/v1",
        gates_dir="/nonexistent-gates",
        golden_dir="/nonexistent-golden",
    )
    assert config["model"] == "local-llm-proxy/qwen3.6-35b-a3b-bench"
    provider = config["provider"]["local-llm-proxy"]
    assert provider["options"]["baseURL"] == "http://host.docker.internal:4545/v1"
    model_block = provider["models"]["qwen3.6-35b-a3b-bench"]
    assert model_block["reasoning"] is True
    assert model_block["tool_call"] is True
    assert model_block["interleaved"] == {"field": "reasoning_content"}
    assert model_block["limit"] == {"context": 262_144, "output": 32_768}
    assert "options" not in model_block


def test_arg_parser_carries_model_flag_default_none() -> None:
    module = _load_run_cumulative_module()
    parser = module._build_arg_parser()
    args = parser.parse_args(["state"])
    assert args.model is None
    args = parser.parse_args(["--model", "qwen3.6-35b-a3b-bench", "state"])
    assert args.model == "qwen3.6-35b-a3b-bench"
