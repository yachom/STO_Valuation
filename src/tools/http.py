"""Tiny HTTP helper shared by all tools."""

from __future__ import annotations

import json
from typing import Any

import requests

from .. import config


class ToolError(RuntimeError):
    pass


def request_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> Any:
    merged_headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    try:
        resp = requests.get(url, params=clean, headers=merged_headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ToolError(f"Network error: {exc}") from exc

    if resp.status_code >= 400:
        raise ToolError(
            f"HTTP {resp.status_code} from {url}: {resp.text[:400]}"
        )

    try:
        return resp.json()
    except json.JSONDecodeError:
        return {"raw": resp.text}
