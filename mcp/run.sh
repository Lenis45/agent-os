#!/usr/bin/env bash
# Запуск amori MCP-сервера (stdio) в общем изолированном runtime проекта.
# Прописывается как MCP-команда в Claude/Codex/Hermes.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${AMORI_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$ROOT/mcp/.venv/bin/python"
fi
exec "$PYTHON" "$ROOT/mcp/server.py"
