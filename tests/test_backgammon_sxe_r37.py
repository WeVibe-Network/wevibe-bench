"""Focused R-37 + substrate-event regression tests for backgammon SxE.

The canonical SxE driver (`scripts/backgammon_sxe.py`) previously logged a memory
plaintext fragment (`fragment=<first-84-chars>`) into stdout, the run logfile, and the
`BACKGAMMON_SXE_RESULT_JSON` payload (which the ladder persists to its checkpoint on disk).
R-37 requires logs/results emit ONLY a hash fingerprint + size + outcome — never plaintext.
These tests guard `_memory_fingerprint_fields` and substrate-event construction.
No paid model, no network, no docker.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

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
    assert '"delivery_proof": delivery_payload' in src
    assert '"n_memories": len(committed_memories)' in src
    assert '"memories": [' in src


def test_memory_fragment_uses_shared_m2_limit_and_single_line() -> None:
    text = "alpha\n\n beta    gamma " * 8

    fragment = sx._memory_fragment(text)

    assert fragment == sx.M2Proof.memory_fragment(text)
    assert len(fragment) <= 64
    assert "\n" not in fragment

