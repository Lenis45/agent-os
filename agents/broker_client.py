"""Small authenticated client for the local Amori request broker."""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}
TOKEN_FILE = Path.home() / ".config" / "amori" / "broker_token"


class BrokerError(RuntimeError):
    pass


class BrokerUnavailable(BrokerError):
    pass


def endpoint() -> str:
    return os.getenv("AMORI_BROKER_URL", "http://127.0.0.1:8110").rstrip("/")


def token() -> str:
    configured = os.getenv("AMORI_BROKER_TOKEN", "").strip()
    if configured:
        return configured
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise BrokerUnavailable("Broker token is not configured") from error


def _request(
    method: str, path: str, payload: Optional[dict] = None, *, timeout: float = 30,
) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint()}{path}", data=body, method=method,
        headers={
            "Authorization": f"Bearer {token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise BrokerError(f"Broker HTTP {error.code}: {detail}") from error
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise BrokerUnavailable(f"Broker unavailable: {error}") from error


def submit(**payload: Any) -> dict:
    return _request("POST", "/v1/requests", payload, timeout=60)


def get(request_id: str) -> dict:
    return _request("GET", f"/v1/requests/{urllib.parse.quote(request_id)}")


def latest(source: str, actor_id: str, session_id: str) -> Optional[dict]:
    parts = [urllib.parse.quote(value, safe="") for value in (source, actor_id, session_id)]
    return _request("GET", f"/v1/sessions/{'/'.join(parts)}/latest").get("request")


def confirm(request_id: str, actor_id: str) -> bool:
    response = _request("POST", f"/v1/requests/{request_id}/confirm", {"actor_id": actor_id})
    return bool(response.get("confirmed"))


def cancel(request_id: str) -> bool:
    return bool(_request("POST", f"/v1/requests/{request_id}/cancel", {}).get("cancelled"))


def wait(request_id: str, *, timeout: float = 1800, poll_seconds: float = 1.5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = get(request_id)
        if response.get("request", {}).get("status") in TERMINAL_STATUSES | {"awaiting_confirmation"}:
            return response
        time.sleep(max(0.25, poll_seconds))
    raise BrokerError("Request execution timed out")


def download_artifact(artifact: dict) -> Path:
    url = f"{endpoint()}{artifact['download_url']}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
    suffix = Path(artifact.get("original_name") or "artifact.bin").suffix
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=120) as response, tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False
        ) as output:
            output.write(response.read())
            return Path(output.name)
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise BrokerUnavailable(f"Cannot download artifact: {error}") from error
