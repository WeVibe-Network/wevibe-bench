"""Admin helper for local spend-proxy consumer management."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:4480"
DEFAULT_ADMIN_TOKEN = "spend_proxy_admin_dev"
_CONSUMER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


logger = logging.getLogger(__name__)


class SpendProxyAdminError(RuntimeError):
    """Raised when spend-proxy admin operations fail."""


def _json_request(
    *,
    method: str,
    url: str,
    admin_token: str,
    body: dict[str, Any] | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    payload_bytes = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url=url,
        data=payload_bytes,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {admin_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            raw = response.read()
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
            return {"data": data}
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body_text = ""
        detail = f"HTTP {exc.code} {exc.reason}" if exc.reason else f"HTTP {exc.code}"
        if body_text:
            detail = f"{detail}: {body_text}"
        raise SpendProxyAdminError(f"spend-proxy admin request failed for {method} {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SpendProxyAdminError(f"spend-proxy admin request failed for {method} {url}: {exc}") from exc


def create_consumer(*, base_url: str, admin_token: str, consumer_id: str) -> int:
    if not _CONSUMER_ID_RE.fullmatch(consumer_id):
        raise SpendProxyAdminError(
            "invalid --consumer-id. Must match ^[A-Za-z0-9_.-]{1,64}$"
        )

    logger.info("spend_proxy_admin op=create-consumer consumer_id=%s", consumer_id)
    url = f"{base_url.rstrip('/')}/admin/consumers"
    payload = _json_request(
        method="POST",
        url=url,
        admin_token=admin_token,
        body={"consumer_id": consumer_id},
    )
    token = payload.get("token")
    if not isinstance(token, str) or not token.strip():
        raise SpendProxyAdminError(
            f"create-consumer returned no token for consumer_id={consumer_id}. Response: {json.dumps(payload, separators=(',', ':'))}"
        )

    print(f"Consumer created: {consumer_id}")
    print("Token (shown once):")
    print(token)
    print("\nStore it in .env as ORCAROUTER_API_KEY=<paste-token-shown-above>.")
    print("\nDo not commit .env. The proxy stores only token fingerprint/sha256.")
    return 0


def list_consumers(*, base_url: str, admin_token: str) -> int:
    logger.info("spend_proxy_admin op=list-consumers")
    url = f"{base_url.rstrip('/')}/admin/consumers"
    payload = _json_request(method="GET", url=url, admin_token=admin_token)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Spend-proxy base URL (default: {DEFAULT_BASE_URL})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create-consumer", help="Create a spend-proxy consumer token")
    create_parser.add_argument(
        "--consumer-id",
        default="bench",
        help="Consumer identifier (default: bench)",
    )

    subparsers.add_parser("list-consumers", help="List spend-proxy consumers")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    admin_token = os.environ.get("SPEND_PROXY_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN)

    try:
        if args.command == "create-consumer":
            return create_consumer(
                base_url=args.base_url,
                admin_token=admin_token,
                consumer_id=args.consumer_id,
            )
        if args.command == "list-consumers":
            return list_consumers(base_url=args.base_url, admin_token=admin_token)
    except SpendProxyAdminError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"ERROR: unknown command {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
