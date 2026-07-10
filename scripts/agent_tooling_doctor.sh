#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${HOME:?HOME is required}"

redact() {
  sed -E \
    -e 's/(sk-[A-Za-z0-9_-]{12,})/<redacted>/g' \
    -e 's/(gh[oprsu]_[A-Za-z0-9_]{12,})/<redacted>/g' \
    -e 's/(xox[baprs]-[A-Za-z0-9-]{12,})/<redacted>/g' \
    -e 's/([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=)[^[:space:]]+/\1<redacted>/g'
}

section() {
  printf '\n== %s ==\n' "$1"
}

check_command() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    printf 'ok   %-16s %s\n' "$cmd" "$(command -v "$cmd")"
  else
    printf 'miss %-16s\n' "$cmd"
    return 1
  fi
}

version_line() {
  local label="$1"
  shift
  printf '%-16s ' "$label"
  ("$@" 2>&1 || true) | head -3 | redact
}

count_skills() {
  local dir="$1"
  if [[ -d "$dir" ]]; then
    printf '%-32s %s\n' "$dir" "$(find "$dir" -maxdepth 2 -name SKILL.md | wc -l | tr -d ' ')"
  else
    printf '%-32s missing\n' "$dir"
  fi
}

secret_scan() {
  local paths=(
    "$ROOT_DIR/agent-tooling"
    "$ROOT_DIR/docs/AGENT_TOOLING_2026.md"
    "$ROOT_DIR/scripts/sync_agent_skills.sh"
    "$ROOT_DIR/scripts/agent_tooling_doctor.sh"
    "$HOME_DIR/.config/opencode/opencode.json"
    "$HOME_DIR/.agents/skills"
  )

  local existing=()
  local path
  for path in "${paths[@]}"; do
    [[ -e "$path" ]] && existing+=("$path")
  done

  if ((${#existing[@]} == 0)); then
    echo "No files to scan."
    return 0
  fi

  if rg -n --hidden --no-ignore-vcs \
    --glob '!**/.system/**' \
    '(PRIVATE KEY|BEGIN OPENSSH|GROQ_API_KEY=|TELEGRAM_BOT_TOKEN=|POSTGRES_PASSWORD=|OPENAI_API_KEY=|ANTHROPIC_API_KEY=|DEEPSEEK_API_KEY=|OPENROUTER_API_KEY=|gh[oprsu]_[A-Za-z0-9_]{12,}|sk-[A-Za-z0-9_-]{12,})' \
    "${existing[@]}" | rg -v 'scripts/agent_tooling_doctor.sh:' >/tmp/agent-tooling-secret-scan.txt; then
    cat /tmp/agent-tooling-secret-scan.txt | redact
    echo "Secret-looking patterns found." >&2
    return 1
  fi
  echo "No secret-looking patterns found in managed agent-tooling files."
}

section "Commands"
check_command codex || true
check_command claude || true
check_command hermes || true
check_command opencode || true
check_command rg || true
check_command git || true

section "Versions"
version_line "codex" codex --version
version_line "claude" claude --version
version_line "hermes" hermes --version
version_line "opencode" opencode --version

section "Skills"
count_skills "$HOME_DIR/.codex/skills"
count_skills "$HOME_DIR/.claude/skills"
count_skills "$HOME_DIR/.agents/skills"
count_skills "$HOME_DIR/.config/opencode/skills"
if rg -n 'external_dirs|~/.agents/skills' "$HOME_DIR/.hermes/config.yaml" >/tmp/hermes-skill-dirs.txt 2>/dev/null; then
  cat /tmp/hermes-skill-dirs.txt | redact
else
  echo "Hermes external skill dir not detected."
fi

section "MCP"
echo "-- codex --"
(codex mcp list 2>&1 || true) | redact | sed -n '1,120p'
echo "-- claude --"
(claude mcp list 2>&1 || true) | redact | sed -n '1,120p'
echo "-- hermes --"
(hermes mcp list 2>&1 || true) | redact | sed -n '1,160p'
echo "-- opencode config --"
(opencode debug config 2>&1 || true) | redact | sed -n '1,220p'

section "Hermes Update Check"
(hermes update --check 2>&1 || true) | redact | sed -n '1,80p'

section "Git State"
echo "-- ai-infra --"
git -C "$ROOT_DIR" status -sb
echo "-- hermes managed checkout --"
git -C "$HOME_DIR/.hermes/hermes-agent" status -sb || true

section "Secret Scan"
secret_scan

section "Done"
echo "Agent tooling doctor finished."
