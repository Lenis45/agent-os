#!/usr/bin/env bash
# install_ponytail.sh — разворачивает skill ponytail (DietrichGebert/ponytail)
# на все кодинг-CLI, установленные на машине (Mac Mini / MacBook).
#
# Идемпотентно: ставит только в те CLI, что реально есть (command -v), повторный
# запуск безопасен (marketplace add/install у всех платформ терпят «уже есть»).
# Python-агенты правила ponytail получают отдельно — через agents/ponytail.py.
#
#   bash scripts/install_ponytail.sh
#
set -u
REPO="DietrichGebert/ponytail"
OK=(); SKIP=(); FAIL=()

have() { command -v "$1" >/dev/null 2>&1; }

# run <label> <cmd...> — выполнить и запомнить исход (не валим весь скрипт на одной ошибке)
run() {
  local label="$1"; shift
  echo "→ $label: $*"
  if "$@"; then OK+=("$label"); else FAIL+=("$label"); fi
}

echo "== ponytail install ($REPO) =="

# Claude Code
if have claude; then
  run "claude marketplace" claude plugin marketplace add "$REPO"
  run "claude install"     claude plugin install ponytail@ponytail
else SKIP+=("claude"); fi

# Codex (установка плагина — через UI /plugins после add)
if have codex; then
  run "codex marketplace" codex plugin marketplace add "$REPO"
  echo "  ⓘ codex: доверши установку в UI — /plugins → ponytail → install"
else SKIP+=("codex"); fi

# GitHub Copilot CLI
if have copilot; then
  run "copilot marketplace" copilot plugin marketplace add "$REPO"
  run "copilot install"     copilot plugin install ponytail@ponytail
else SKIP+=("copilot"); fi

# Hermes
if have hermes; then
  run "hermes install" hermes plugins install "$REPO" --enable
else SKIP+=("hermes"); fi

# Gemini / Antigravity CLI
if have gemini; then
  run "gemini install" gemini extensions install "https://github.com/$REPO"
else SKIP+=("gemini"); fi

# OpenCode — конфиг-файл, руками (скрипт чужой json не трогает)
if have opencode; then
  echo "  ⓘ opencode: добавь в opencode.json → {\"plugin\": [\"@dietrichgebert/ponytail\"]}"
  SKIP+=("opencode(manual)")
else SKIP+=("opencode"); fi

echo
echo "== итог =="
echo "  установлено: ${OK[*]:-—}"
echo "  пропущено (нет CLI): ${SKIP[*]:-—}"
echo "  ошибки: ${FAIL[*]:-—}"
[ ${#FAIL[@]} -eq 0 ]
