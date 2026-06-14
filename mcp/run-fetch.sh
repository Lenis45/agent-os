#!/usr/bin/env bash
# Официальный MCP fetch-сервер (web content) из venv. Обёртка, чтобы избежать
# проблем с парсингом `-m mcp_server_fetch` в CLI разных агентов.
exec "$(dirname "$0")/.venv/bin/python" -m mcp_server_fetch
