#!/usr/bin/env python3
"""Local OpenAI-compatible image bridge backed by Hermes Codex OAuth."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import sys
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_HERMES_HOME = Path("~/.hermes/hermes-agent").expanduser()
MAX_REQUEST_BYTES = 1024 * 1024
GENERATION_LOCK = threading.Lock()


def _aspect_from_size(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"1:1", "square", "1024x1024"}:
        return "square"
    if normalized in {"9:16", "portrait", "1024x1536"}:
        return "portrait"
    return "landscape"


@lru_cache(maxsize=1)
def _load_provider(hermes_home: Path | None = None):
    root = (hermes_home or Path(os.getenv("HERMES_AGENT_HOME", DEFAULT_HERMES_HOME))).expanduser()
    plugin = root / "plugins" / "image_gen" / "openai-codex" / "__init__.py"
    if not plugin.is_file():
        raise RuntimeError(f"Hermes image plugin is missing: {plugin}")
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    spec = importlib.util.spec_from_file_location("amori_hermes_codex_image", plugin)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load Hermes image plugin")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OpenAICodexImageGenProvider()


def provider_status(provider=None) -> dict[str, Any]:
    try:
        provider = provider or _load_provider()
        available = bool(provider.is_available())
        return {
            "status": "VALID" if available else "AUTH_REQUIRED",
            "authenticated": available,
            "provider": "openai-codex",
            "accounts": [{"status": "OK" if available else "AUTH_REQUIRED"}],
        }
    except Exception as exc:
        return {
            "status": "ERROR",
            "authenticated": False,
            "provider": "openai-codex",
            "accounts": [{"status": "ERROR"}],
            "message": str(exc)[:300],
        }


def generate_payload(payload: dict[str, Any], provider=None) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    provider = provider or _load_provider()
    with GENERATION_LOCK:
        result = provider.generate(prompt, aspect_ratio=_aspect_from_size(str(payload.get("size") or "")))
    if not result.get("success"):
        raise RuntimeError(str(result.get("error") or "image provider failed"))
    image_path = Path(str(result.get("image") or "")).expanduser().resolve()
    if not image_path.is_file():
        raise RuntimeError("image provider returned a missing local file")
    raw = image_path.read_bytes()
    return {
        "created": int(image_path.stat().st_mtime),
        "model": result.get("model"),
        "provider": result.get("provider", "openai-codex"),
        "data": [{"b64_json": base64.b64encode(raw).decode("ascii")}],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AmoriImageBridge/1.0"

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/api/status", "/api/health"}:
            self._write_json(404, {"error": "not found"})
            return
        status = provider_status()
        self._write_json(200 if status["authenticated"] else 503, status)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/images/generations":
            self._write_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            self._write_json(200, generate_payload(payload))
        except ValueError as exc:
            self._write_json(400, {"error": str(exc)[:300]})
        except Exception as exc:
            self._write_json(503, {"error": str(exc)[:500]})

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"image-bridge: {self.address_string()} {fmt % args}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3264)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
