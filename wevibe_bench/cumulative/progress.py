"""ProgressVector mapping for cumulative benchmark cell telemetry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .types import MISSING_TELEMETRY_SEAMS, ProgressVector

_MISSING = object()


def _field(source: Any, name: str) -> Any:
    if source is None:
        return _MISSING
    if isinstance(source, Mapping):
        return source.get(name, _MISSING)
    return getattr(source, name, _MISSING)


def _optional_int(value: Any) -> int | None:
    if value is _MISSING or value is None or isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        signless = text[1:] if text[0] in "+-" else text
        if not signless.isdigit():
            return None
        return int(text)

    if isinstance(value, float):
        if not value.is_integer():
            return None
        return int(value)

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or_default(value: Any, *, default: int) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        return default
    return parsed


def _float_or_default(value: Any, *, default: float) -> float:
    if value is _MISSING or value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_or_default(value: Any, *, default: bool) -> bool:
    if value is _MISSING or value is None:
        return default
    if isinstance(value, bool):
        return value
    return bool(value)


def _str_or_default(value: Any, *, default: str) -> str:
    if value is _MISSING or value is None:
        return default
    return str(value)


def _str_list(value: Any) -> list[str]:
    if value is _MISSING or not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _problems_after_from_result(result: Any) -> int | None:
    problems_final = _field(result, "problems_final")
    if problems_final is _MISSING or problems_final is None:
        return None
    if isinstance(problems_final, list):
        return len(problems_final)
    return _optional_int(problems_final)


def progress_from_cell_result(result: Any, *, cell: Any | None = None) -> ProgressVector:
    """Map backgammon cell telemetry into a cumulative ProgressVector.

    Notes:
    - This accepts either real runtime objects or plain mappings for lightweight tests.
    - Any seam without a real source stays None so it is surfaced as missing telemetry.
    """

    input_tokens = _int_or_default(_field(result, "input_tokens"), default=0)
    output_tokens = _int_or_default(_field(result, "output_tokens"), default=0)

    total_tokens_value = _field(result, "total_tokens")
    if total_tokens_value is _MISSING and cell is not None:
        total_tokens_value = _field(cell, "total_tokens")
    total_tokens = _int_or_default(total_tokens_value, default=input_tokens + output_tokens)

    problems_before = _optional_int(_field(result, "problems_before"))
    problems_after = _problems_after_from_result(result)
    remaining_count = problems_after if problems_after is not None else None
    resolved_count = (
        problems_before - problems_after
        if problems_before is not None and problems_after is not None
        else None
    )

    return ProgressVector(
        problems_before=problems_before,
        problems_after=problems_after,
        resolved_count=resolved_count,
        remaining_count=remaining_count,
        full_green=_bool_or_default(_field(result, "conformed"), default=False),
        attempts_to_green=_optional_int(_field(result, "attempts_to_green")),
        turns=_int_or_default(_field(result, "turns"), default=0),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        wall_seconds=_float_or_default(_field(result, "wall_seconds"), default=0.0),
        wall_cost_usd=_float_or_default(_field(result, "wall_cost_usd"), default=0.0),
        injected_count=_optional_int(_field(cell, "injection_count")),
        consumer_injected_count=None,
        tool_calls=_optional_int(_field(result, "tool_calls")),
        test_invocations=_optional_int(_field(result, "test_invocations")),
        agentic_cycles=_optional_int(_field(result, "agentic_cycles")),
        termination_reason=_str_or_default(_field(result, "termination_reason"), default=""),
        failed_gates=_str_list(_field(result, "failed_gates")),
        missing_telemetry_seams=list(MISSING_TELEMETRY_SEAMS),
    )


__all__ = ["progress_from_cell_result"]
