from __future__ import annotations

import time
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException, status

from .security import Settings, truncate, validate_url

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def http_request(
    settings: Settings,
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
) -> Dict[str, Any]:
    """Make an HTTP request to any external URL."""
    method = method.upper()
    if method not in _ALLOWED_METHODS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Method must be one of: {', '.join(sorted(_ALLOWED_METHODS))}")
    validate_url(settings, url)
    headers = headers or {}
    start = time.perf_counter()

    try:
        with httpx.Client(timeout=httpx.Timeout(settings.http_timeout_s), follow_redirects=True) as client:
            with client.stream(method, url, headers=headers,
                               content=body.encode() if isinstance(body, str) else body) as resp:
                chunks, total, truncated = [], 0, False
                limit = settings.http_max_response_bytes
                for chunk in resp.iter_bytes():
                    remaining = limit - total
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                content_bytes = b"".join(chunks)
    except httpx.RequestError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"HTTP request failed: {e}") from e

    duration_ms = int((time.perf_counter() - start) * 1000)
    text = content_bytes.decode(resp.encoding or "utf-8", errors="replace")
    bounded, _ = truncate(text, settings.max_output_chars)

    return {
        "ok": 200 <= resp.status_code < 400,
        "status": resp.status_code,
        "headers": dict(resp.headers),
        "text": bounded,
        "truncated": truncated,
        "duration_ms": duration_ms,
        "url": str(resp.url),
        "method": method,
    }
