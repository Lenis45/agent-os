#!/usr/bin/env bash
# Keep heavyweight product-development stacks on demand on a 16 GB Mac Mini.
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ACTION="${1:-status}"
PROJECTS=(amori-new amori-local hypothesis_hub)

if [[ ! "$ACTION" =~ ^(start|stop|status)$ ]]; then
  echo "usage: $0 start|stop|status" >&2
  exit 2
fi

for project in "${PROJECTS[@]}"; do
  ids=()
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    service="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$id")"
    [[ "$service" == *setup ]] || ids+=("$id")
  done < <(docker ps -aq --filter "label=com.docker.compose.project=$project")

  if [ "${#ids[@]}" -eq 0 ]; then
    echo "[dev-stacks] $project: containers not found"
    continue
  fi

  case "$ACTION" in
    start)
      docker update --restart unless-stopped "${ids[@]}" >/dev/null
      docker start "${ids[@]}" >/dev/null
      echo "[dev-stacks] $project: started (${#ids[@]})"
      ;;
    stop)
      docker update --restart no "${ids[@]}" >/dev/null
      docker stop -t 30 "${ids[@]}" >/dev/null
      echo "[dev-stacks] $project: stopped (${#ids[@]}); autostart disabled; volumes preserved"
      ;;
    status)
      running="$(docker ps -q --filter "label=com.docker.compose.project=$project" | wc -l | tr -d ' ')"
      echo "[dev-stacks] $project: $running/${#ids[@]} running"
      ;;
  esac
done
