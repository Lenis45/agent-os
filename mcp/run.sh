#!/usr/bin/env bash
# Запуск amori MCP-сервера (stdio) в изолированном venv.
# Прописывается как MCP-команда в Claude/Codex/Hermes.
exec "$(dirname "$0")/.venv/bin/python" "$(dirname "$0")/server.py"
