from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from wevibe_bench.adapters.backgammon import build_worker_opencode_config


def _load_run_cumulative_module() -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_cumulative.py"
    spec = importlib.util.spec_from_file_location("run_cumulative_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_override_preserves_neutral_auto_resident_slug() -> None:
    module = _load_run_cumulative_module()
    roster, roster_hash = module._build_roster()
    assert [entry.model for entry in roster] == ["local-llm-proxy/wevibe-bench-worker"]
    assert roster[0].provider_pin == "wevibe-bench-worker"
    assert roster_hash


def test_model_override_rewrites_single_subject_roster() -> None:
    module = _load_run_cumulative_module()
    default_roster, default_hash = module._build_roster()
    roster, roster_hash = module._build_roster(model_override="qwen3.6-35b-a3b-bench")
    assert len(roster) == len(default_roster) == 1
    entry = roster[0]
    assert entry.model == "local-llm-proxy/qwen3.6-35b-a3b-bench"
    assert entry.provider_pin == "qwen3.6-35b-a3b-bench"
    assert entry.role == default_roster[0].role
    assert entry.config_identity == default_roster[0].config_identity
    assert roster_hash != default_hash


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
