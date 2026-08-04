"""Local LM Studio roster declarations (Walter 2026-07-31 local-model pivot).

The three bench aliases must resolve through WORKER_MODEL_REGISTRY so that
build_worker_opencode_config accepts their roster slugs; a missing entry is
the harness_error=model-not-found class that voided cells before. Full native
context (262144) and the 32768 output budget are Walter's bench contract.
"""

from __future__ import annotations

from wevibe_bench.adapters.backgammon import build_worker_opencode_config
from wevibe_bench.config import WORKER_MODEL_REGISTRY

LOCAL_MODEL_IDS = (
    "wevibe-bench-worker",
    "qwen3.6-40b-deckard-bench",
    "qwen3.6-27b-fable-bench",
)


def test_local_models_registered_with_full_context_and_bench_output_budget() -> None:
    for model_id in LOCAL_MODEL_IDS:
        entry = WORKER_MODEL_REGISTRY.get(model_id)
        assert entry is not None, f"missing WORKER_MODEL_REGISTRY entry for {model_id}"
        assert entry["tool_call"] is True
        assert entry.get("reasoning") is True
        assert entry["limit"]["context"] == 262_144
        assert entry["limit"]["output"] == 32_768
        assert entry["options"] == {"temperature": 0.6}


def test_local_model_slug_builds_worker_opencode_config() -> None:
    for model_id in LOCAL_MODEL_IDS:
        config = build_worker_opencode_config(
            model=f"orcarouter/{model_id}",
            reasoning_effort=None,
            proxy_base_url="http://host.docker.internal:4545/v1",
            gates_dir="/gates",
            golden_dir="/golden",
            session_id="sess-local-1",
        )
        assert config["model"] == f"orcarouter/{model_id}"
        assert config["small_model"] == f"orcarouter/{model_id}"
        provider = config["provider"]["orcarouter"]
        assert provider["options"]["baseURL"] == "http://host.docker.internal:4545/v1"
        model_block = provider["models"][model_id]
        assert model_block["interleaved"] == {"field": "reasoning_content"}
        assert model_block["headers"] == {"X-Session-Id": "sess-local-1"}
        assert model_block["limit"] == {"context": 262_144, "output": 32_768}
        assert model_block["options"] == {"temperature": 0.6}
