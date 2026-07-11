"""Qdrant REST probes for lifecycle INV-10 point-count checks."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


Transport = Callable[[str], tuple[int, Any, bool]]


def _qdrant_api_key() -> str:
    # Qdrant dev instance is api-key gated; default matches the dev compose value.
    return os.environ.get("QDRANT_API_KEY", "wevibe_dev_qdrant_key_32chars_minimum").strip()


def _json_value(payload: bytes) -> Any:
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _urllib_get(url: str) -> tuple[int, Any, bool]:
    try:
        request = urllib.request.Request(url=url, method="GET")
        api_key = _qdrant_api_key()
        if api_key:
            request.add_header("api-key", api_key)
        with urllib.request.urlopen(request, timeout=5.0) as response:
            status = response.getcode()
            payload = response.read()
        return status, _json_value(payload), True
    except urllib.error.HTTPError as exc:
        try:
            error_payload = exc.read()
        except OSError:
            error_payload = b""
        return exc.code, _json_value(error_payload), True
    except (urllib.error.URLError, OSError, socket.timeout):
        return 0, {}, False
    except Exception:
        return 0, {}, False


def list_collections(
    qdrant_url: str,
    transport: Transport | None = None,
) -> list[str]:
    """Return all collection names from Qdrant ``/collections``."""

    send = transport or _urllib_get
    url = f"{qdrant_url.rstrip('/')}/collections"
    status, payload, reachable = send(url)
    if not reachable:
        raise RuntimeError(f"qdrant unreachable for {url}")
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"qdrant list collections failed status={status} payload={payload}")

    result = payload.get("result") if isinstance(payload, dict) else None
    collections = result.get("collections") if isinstance(result, dict) else None
    if not isinstance(collections, list):
        return []

    names: list[str] = []
    for item in collections:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def collection_point_count(
    qdrant_url: str,
    name: str,
    transport: Transport | None = None,
) -> int | None:
    """Return ``points_count`` for a collection, or ``None`` if missing."""

    send = transport or _urllib_get
    encoded = urllib.parse.quote(name, safe="")
    url = f"{qdrant_url.rstrip('/')}/collections/{encoded}"
    status, payload, reachable = send(url)
    if not reachable:
        raise RuntimeError(f"qdrant unreachable for {url}")
    if status == 404:
        return None
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"qdrant collection fetch failed status={status} name={name} payload={payload}")

    result = payload.get("result") if isinstance(payload, dict) else None
    points_count = result.get("points_count") if isinstance(result, dict) else None
    if isinstance(points_count, bool):
        return int(points_count)
    if isinstance(points_count, (int, float)):
        return int(points_count)
    return None


def _match_score(name: str, org_id: str) -> int:
    score = 0
    if name == org_id:
        score += 100
    if name.startswith(org_id) or name.endswith(org_id):
        score += 40
    if org_id in name:
        score += 20
    return score


def find_org_collection(
    qdrant_url: str,
    org_id: str,
    transport: Transport | None = None,
) -> str | None:
    """Best-effort collection lookup by org id.

    Returns ``None`` when match quality is ambiguous; callers should then rely on
    ``snapshot_counts`` diffing to determine which collection changed.
    """

    names = list_collections(qdrant_url, transport=transport)
    if not names or not org_id:
        return None

    exact = [name for name in names if name == org_id]
    if len(exact) == 1:
        return exact[0]

    contains = [name for name in names if org_id in name]
    if len(contains) == 1:
        return contains[0]
    if not contains:
        return None

    ranked = sorted(
        contains,
        key=lambda candidate: (_match_score(candidate, org_id), -len(candidate), candidate),
        reverse=True,
    )
    top_score = _match_score(ranked[0], org_id)
    second_score = _match_score(ranked[1], org_id) if len(ranked) > 1 else -1
    if top_score > second_score:
        return ranked[0]
    return None


def snapshot_counts(
    qdrant_url: str,
    transport: Transport | None = None,
) -> dict[str, int]:
    """Return a point-count snapshot for every currently-listed collection."""

    names = list_collections(qdrant_url, transport=transport)
    snapshot: dict[str, int] = {}
    for name in names:
        count = collection_point_count(qdrant_url, name, transport=transport)
        if count is not None:
            snapshot[name] = count
    return snapshot
