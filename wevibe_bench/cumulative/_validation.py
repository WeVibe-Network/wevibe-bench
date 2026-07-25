"""Shared validation helpers for cumulative benchmark modules."""

from __future__ import annotations

from typing import Any, Mapping


def _require_non_empty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _require_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(f"{field_name} must be a mapping")


def _coerce_mapping_like(value: Any, *, field_name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        mapped = to_dict()
        if isinstance(mapped, Mapping):
            return dict(mapped)

    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, Mapping):
        return dict(attrs)

    raise ValueError(f"{field_name} must be mapping-like")
