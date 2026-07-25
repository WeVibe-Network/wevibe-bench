"""Adapters that implement ``AgentRunner`` against concrete coding substrates."""

from __future__ import annotations

from .backgammon import BackgammonCellResult, BackgammonRunner
from .swecontextbench import SWEContextBenchRunner, SolveResult, write_prediction

__all__ = [
    "BackgammonCellResult",
    "BackgammonRunner",
    "SolveResult",
    "SWEContextBenchRunner",
    "write_prediction",
]
