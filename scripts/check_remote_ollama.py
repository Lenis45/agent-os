#!/usr/bin/env python3
"""Check denis-k Ollama reachability and required model inventory.

The Mac VPN can steal the Tailscale IPv4 range, so the default endpoint is the
Windows Tailscale IPv6 address stored in agents/.env.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "agents" / ".env"


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def get_json(url: str, timeout: float) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return int(resp.status), json.loads(resp.read().decode("utf-8") or "{}")


def main() -> int:
    env = load_env(ENV_FILE)
    base = env.get("OLLAMA_API_BASE", "http://[fd7a:115c:a1e0::b43b:954]:11434").rstrip("/")
    required = [
        item.strip()
        for item in env.get(
            "OLLAMA_REQUIRED_MODELS",
            "qwen3.6:35b-a3b-q4_K_M,qwen3.6:27b-q4_K_M,gemma4:12b-it-qat",
        ).split(",")
        if item.strip()
    ]
    timeout = float(env.get("OLLAMA_CHECK_TIMEOUT", "8"))

    print(f"Ollama endpoint: {base}")
    print(f"Required models: {', '.join(required) if required else 'none'}")

    try:
        status, version = get_json(base + "/api/version", timeout)
        print(f"API version: HTTP {status} · {version.get('version', 'unknown')}")
        status, tags = get_json(base + "/api/tags", timeout)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        print(f"API status: unavailable ({str(exc)[:160]})")
        print("Result: agents will use Groq fallback for Ollama-routed work.")
        return 2

    models = sorted(
        str(item.get("name") or "").strip()
        for item in tags.get("models", [])
        if isinstance(item, dict) and item.get("name")
    )
    print(f"Installed models ({len(models)}):")
    for model in models:
        print(f"  - {model}")
    if not models:
        print("  - none")

    missing = [model for model in required if model not in models]
    if missing:
        print(f"Missing required models: {', '.join(missing)}")
        for model in missing:
            print(f"Install on Windows: ollama pull {model}")
        print("Result: agents will use Groq fallback for missing local models.")
        return 1

    print("Result: Ollama is ready for local routed agents.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
