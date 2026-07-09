"""Shared backend contract for benchmark recall operations.

INV-6 is structurally enforced: only `NeedCard.intent` + `NeedCard.task` participate in
the dense/prose digest, while keyword-like fields are isolated to the keyword channel.
BENCHMARK INTEGRITY is preserved through delivery verification semantics and MC-1 envelope
symmetry in `NeedCard.to_wire`.

The live API has no explicit YES/CALLED/NO field; this scaffold maps onto real mechanism:
YES means memories were delivered with non-empty decrypted `text`, CALLED means request hit
recall but delivery failed/was filtered (`reason_code` decrypt_failed/filtered_out), and NO
means unreachable/error/no candidates.
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from wevibe_bench.config import RunConfig


class DeliveryVerdict(str, Enum):
    """Benchmark-integrity verdict derived from transport + delivered plaintext content."""

    YES = "YES"  # memory delivered with real DECRYPTED vector content (memories[].text non-empty)
    CALLED = "CALLED"  # hub matched candidates but nothing delivered (reason_code in decrypt_failed/filtered_out)
    NO = "NO"  # endpoint unreachable / HTTP error / status error / no candidates matched


@dataclass
class RecalledMemory:
    """Single recalled memory item with score breakdown and decrypted plaintext payload."""

    cid: str | None
    score: float | None  # top-level freshness score
    vector_score: float | None  # breakdown.vector_score
    combined_score: float | None  # breakdown.combined_score
    keyword_score: float | None  # breakdown.keyword_score
    matched_keywords: list[str]
    text: str  # DECRYPTED plaintext; empty string if not delivered

    def has_content(self) -> bool:
        """Return True only when decrypted plaintext content is non-empty after trimming."""

        return bool(self.text.strip())


@dataclass
class NeedCard:
    """Need-card payload enforcing INV-6 channel separation for dense vs keyword signals."""

    # --- DENSE / prose channel (INV-6): the ONLY fields that feed prompt_digest ---
    intent: str
    task: str
    # --- KEYWORD channel: never enters the dense query; ride the keyword/boost channel ---
    language: str | None = None
    stack: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    error_strings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    directory: str | None = None
    project_name: str | None = None
    query: str = ""  # raw query string the probe also sends; defaults to task in __post_init__ if empty

    def __post_init__(self) -> None:
        """Set the raw query channel to `task` when query is omitted."""

        if not self.query:
            self.query = self.task

    @property
    def prompt_digest(self) -> str:
        """INV-6 dense digest from ONLY intent and task, whitespace-collapsed, else `unknown`."""

        def collapse_whitespace(value: str) -> str:
            return re.sub(r"\s+", " ", value).strip()

        segments = [collapse_whitespace(self.intent), collapse_whitespace(self.task)]
        dense_segments = [segment for segment in segments if segment]
        if not dense_segments:
            return "unknown"
        return ". ".join(dense_segments)

    def to_wire(self, cfg: RunConfig, session_id: str) -> dict:
        """Build the probe wire body with MC-1 envelope while keeping INV-6 digest client-only.

        NOTE (INV-6): `prompt_digest` is intentionally not a wire field. The server derives its
        dense query from intent+task; this method sends flat harvest fields + envelope only.
        """

        wire: dict[str, Any] = {
            "query": self.query,
            "intent": self.intent,
            "task": self.task,
            "org_id": cfg.org_id,
            "mc_version": cfg.mc_version,
            "session_id": session_id,
            "relevance_floor": cfg.relevance_floor(),
            "surface_budget": cfg.surface_budget,
            "limit": cfg.surface_budget,
        }

        if self.language:
            wire["language"] = self.language
        if self.stack:
            wire["stack"] = list(self.stack)
        if self.frameworks:
            wire["frameworks"] = list(self.frameworks)
        if self.deps:
            wire["deps"] = list(self.deps)
        if self.error_strings:
            wire["errorStrings"] = list(self.error_strings)
        if self.directory:
            wire["directory"] = self.directory
        if self.project_name:
            wire["projectName"] = self.project_name
        if self.files:
            wire["files"] = list(self.files)

        return wire


@dataclass
class RecallResult:
    """Raw recall response normalized for benchmark transport/result interpretation."""

    memories: list[RecalledMemory]
    status: str  # 'ok' | 'error'
    reason_code: str | None  # no_membership|no_keywords|provider_not_allowed|no_memories|decrypt_failed|filtered_out|None
    reachable: bool  # False if endpoint could not be reached
    http_status: int | None


class MemoryBackend(abc.ABC):
    """Backend interface guaranteeing a single recall/verification path for benchmark cells."""

    @abc.abstractmethod
    def prime_session(self, session_id: str) -> None: ...

    @abc.abstractmethod
    def recall(self, need: NeedCard, cfg: RunConfig) -> RecallResult: ...

    @abc.abstractmethod
    def verify_delivery(self, result: RecallResult) -> DeliveryVerdict: ...
