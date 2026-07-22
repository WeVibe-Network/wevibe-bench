"""Focused R-37 + substrate-event regression tests for backgammon SxE.

The canonical SxE driver (`scripts/backgammon_sxe.py`) previously logged a memory
plaintext fragment (`fragment=<first-84-chars>`) into stdout, the run logfile, and the
`BACKGAMMON_SXE_RESULT_JSON` payload (which the ladder persists to its checkpoint on disk).
R-37 requires logs/results emit ONLY a hash fingerprint + size + outcome — never plaintext.
These tests guard `_memory_fingerprint_fields` and substrate-event construction.
No paid model, no network, no docker.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import backgammon_sxe as sx  # noqa: E402


_SECRET = "SYNTHETIC-FIXTURE: add the --frobnicate flag to the launch command when targeting Widget 9.x or the gizmo fails to boot"


def test_fingerprint_fields_emit_only_hash_and_size_never_plaintext() -> None:
    fields = sx._memory_fingerprint_fields(_SECRET)

    # Exactly the R-37-safe keys, no plaintext key.
    assert set(fields) == {"memory_fp", "text_size"}

    # sha256 first-8 hex fingerprint.
    assert fields["memory_fp"] == sx._sha256_first8(_SECRET)
    assert len(fields["memory_fp"]) == 8
    assert all(c in "0123456789abcdef" for c in fields["memory_fp"])

    # Byte/char size of the plaintext (size is not the plaintext).
    assert fields["text_size"] == len(_SECRET)

    # The plaintext (or any distinctive slice of it) must NOT appear anywhere in the
    # serialized fields — this is the property that used to be violated.
    serialized = json.dumps(fields)
    assert _SECRET not in serialized
    assert "frobnicate" not in serialized
    assert "gizmo" not in serialized


def test_fingerprint_fields_distinguish_distinct_plaintext() -> None:
    a = sx._memory_fingerprint_fields("alpha memory text")
    b = sx._memory_fingerprint_fields("bravo memory text")
    assert a["memory_fp"] != b["memory_fp"]


def test_result_payload_construction_carries_no_plaintext_key() -> None:
    # The SxE result payload must not reintroduce a plaintext-bearing field. The success
    # path spreads `_memory_fingerprint_fields(...)` (memory_fp/text_size) in place of the
    # old `memory_fragment`. Guard against a regression reintroducing a fragment/text key.
    src = (SCRIPTS / "backgammon_sxe.py").read_text(encoding="utf-8")
    assert '"memory_fragment":' not in src, "memory_fragment must not be a logged/result field (R-37)"
    assert "fragment={memory_fragment" not in src, "delivery-proof log must not print the plaintext fragment (R-37)"
    assert '"n_memories": len(committed_memories)' in src
    assert '"memories": [' in src


def test_memory_fragment_uses_shared_m2_limit_and_single_line() -> None:
    text = "alpha\n\n beta    gamma " * 8

    fragment = sx._memory_fragment(text)

    assert fragment == sx.M2Proof.memory_fragment(text)
    assert len(fragment) <= 64
    assert "\n" not in fragment


def test_build_substrate_events_maps_user_assistant_reasoning_tool_and_edit(tmp_path: pathlib.Path) -> None:
    user_sidecar = tmp_path / "worktree.user-events.jsonl"
    worker_events = tmp_path / "worktree.events.jsonl"

    user_sidecar.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": 1_700_000_000_000,
                        "attempt": 1,
                        "text": "initial task prompt",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": 1_700_000_000_500,
                        "attempt": 2,
                        "text": "repair feedback",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    worker_events.write_text(
        "\n".join(
            [
                json.dumps({"type": "step_start", "timestamp": 1_700_000_000_001, "part": {}}, separators=(",", ":"), sort_keys=True),
                json.dumps(
                    {
                        "type": "text",
                        "timestamp": 1_700_000_000_100,
                        "part": {
                            "text": "assistant says hello",
                            "time": {"start": 1_700_000_000_101, "end": 1_700_000_000_120},
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1_700_000_000_200,
                        "part": {
                            "tool": "read",
                            "time": {"start": 1_700_000_000_201, "end": 1_700_000_000_260},
                            "state": {
                                "status": "completed",
                                "input": {"offset": 3, "filePath": "src/game.ts"},
                                "output": "file body",
                                "metadata": {"exit": 0},
                            },
                            "metadata": {
                                "openrouter": {
                                    "reasoning_details": [
                                        {"type": "reasoning.text", "text": "reasoning alpha"},
                                        {"type": "other", "text": "ignored"},
                                    ]
                                }
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1_700_000_000_300,
                        "part": {
                            "tool": "edit",
                            "time": {"start": 1_700_000_000_301},
                            "state": {
                                "status": "completed",
                                "input": {
                                    "filePath": "src/game.ts",
                                    "oldString": "old body",
                                    "newString": "new body",
                                },
                                "output": "ok",
                                "metadata": {},
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1_700_000_000_400,
                        "part": {
                            "tool": "write",
                            "time": {"start": 1_700_000_000_401},
                            "state": {
                                "status": "completed",
                                "input": {
                                    "filePath": "src/ai.ts",
                                    "content": "export const x = 1;",
                                },
                                "output": "ok",
                                "metadata": {},
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "timestamp": 1_700_000_000_500,
                        "part": {
                            "tool": "bash",
                            "time": {"start": 1_700_000_000_501},
                            "state": {
                                "status": "error",
                                "input": {"cwd": "/tmp/repo", "command": "npm test"},
                                "output": "boom failed",
                                "metadata": {"exit": 1},
                            },
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    {"type": "step_finish", "timestamp": 1_700_000_000_600, "part": {"tokens": 999}},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    events, stats, events_files = sx._build_substrate_events(session_dir=tmp_path)

    assert events_files == [worker_events.resolve()]
    assert [event["seq"] for event in events] == list(range(len(events)))
    assert [event["kind"] for event in events] == [
        "user",
        "user",
        "assistant",
        "reasoning",
        "tool",
        "edit",
        "edit",
        "tool",
    ]

    assert events[2] == {
        "kind": "assistant",
        "time": 1_700_000_000_101,
        "seq": 2,
        "role": "assistant",
        "text": "assistant says hello",
    }
    assert events[3] == {
        "kind": "reasoning",
        "time": 1_700_000_000_201,
        "seq": 3,
        "role": "assistant",
        "text": "reasoning alpha",
    }
    assert events[4] == {
        "kind": "tool",
        "time": 1_700_000_000_201,
        "seq": 4,
        "name": "read",
        "input": '{"filePath":"src/game.ts","offset":3}',
        "output": "file body",
        "exit": 0,
        "status": "completed",
    }
    assert events[5] == {
        "kind": "edit",
        "time": 1_700_000_000_301,
        "seq": 5,
        "file": "src/game.ts",
        "detail": "new body",
    }
    assert events[6] == {
        "kind": "edit",
        "time": 1_700_000_000_401,
        "seq": 6,
        "file": "src/ai.ts",
        "detail": "export const x = 1;",
    }
    assert events[7] == {
        "kind": "tool",
        "time": 1_700_000_000_501,
        "seq": 7,
        "name": "bash",
        "input": '{"command":"npm test","cwd":"/tmp/repo"}',
        "output": "boom failed",
        "exit": 1,
        "status": "error",
        "error": "boom failed",
    }

    canonical_events = json.dumps(events, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert stats == {
        "event_count": 8,
        "kind_counts": {
            "user": 2,
            "assistant": 1,
            "reasoning": 1,
            "tool": 2,
            "edit": 2,
        },
        "total_chars": len(canonical_events),
        "events_sha256_first8": sx._sha256_first8(canonical_events),
    }


def test_build_substrate_events_requires_user_sidecar_for_each_events_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "worktree.events.jsonl").write_text(
        json.dumps(
            {
                "type": "text",
                "timestamp": 1_700_000_000_000,
                "part": {"text": "assistant", "time": {"start": 1_700_000_000_001}},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="missing user sidecar for worker events file"):
        sx._build_substrate_events(session_dir=tmp_path)
