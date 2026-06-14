#!/usr/bin/env bash
# run_and_alert.sh — прогон тест-набора инфры; алерт в Telegram ТОЛЬКО при падении.
# Запускается еженедельно (launchd ai.tests) и вручную перед/после правок агентов.
set -uo pipefail
AGENTS="${INFRA_DIR:-$HOME/ai-infra}/agents"
PY="${PY:-/opt/anaconda3/bin/python3}"
cd "$AGENTS"

out="$("$PY" -m pytest tests/ -q 2>&1)"
summary="$(echo "$out" | grep -E '[0-9]+ (passed|failed|error)' | tail -1)"

if echo "$out" | grep -qE '[0-9]+ (failed|error)'; then
  "$PY" notify.py "🧪 Тесты инфры УПАЛИ: ${summary:-см. лог}" --level crit >/dev/null 2>&1 || true
  echo "FAIL: $summary"
  echo "$out" | grep -E "FAILED|ERROR" | head -10
  exit 1
fi
echo "OK: $summary"
