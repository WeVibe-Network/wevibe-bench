"""Adapters that implement ``AgentRunner`` against concrete coding substrates."""

from __future__ import annotations

from .aider_polyglot import (
    AiderPolyglotRunner,
    ExecResult,
    Executor,
    Exercise,
    MockExecutor,
    PolyglotRepoNotFound,
    SubprocessExecutor,
)
from .swecontextbench import SWEContextBenchRunner, SolveResult, write_prediction

__all__ = [
    "AiderPolyglotRunner",
    "ExecResult",
    "Executor",
    "Exercise",
    "MockExecutor",
    "PolyglotRepoNotFound",
    "SolveResult",
    "SWEContextBenchRunner",
    "SubprocessExecutor",
    "write_prediction",
]
