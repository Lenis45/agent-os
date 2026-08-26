#!/usr/bin/env bash
# Reclaim only reproducible Docker build cache. Running services and data stay intact.
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

INFRA="${INFRA_DIR:-$HOME/ai-infra}"
DOCKER="${DOCKER:-$(command -v docker || echo /usr/local/bin/docker)}"
CACHE_MAX_AGE="${DOCKER_BUILD_CACHE_MAX_AGE:-168h}"
MIN_FREE_GB="${STORAGE_MIN_FREE_GB:-15}"
STATUS_FILE="${STORAGE_STATUS_FILE:-$INFRA/agents/runtime/storage_maintenance.json}"

mkdir -p "$(dirname "$STATUS_FILE")"

free_gb() {
  df -g "$HOME" 2>/dev/null | awk 'NR == 2 {print $4}'
}

before="$(free_gb)"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[storage] free before: ${before:-unknown}GB"
echo "[storage] pruning Docker build cache unused for $CACHE_MAX_AGE"

"$DOCKER" builder prune -af --filter "until=$CACHE_MAX_AGE"

after="$(free_gb)"
echo "[storage] free after: ${after:-unknown}GB"

cat > "$STATUS_FILE.tmp" <<EOF
{"ts":"$started","status":"ok","free_gb_before":${before:-0},"free_gb_after":${after:-0},"cache_max_age":"$CACHE_MAX_AGE"}
EOF
mv "$STATUS_FILE.tmp" "$STATUS_FILE"
chmod 600 "$STATUS_FILE"

if [ "${after:-0}" -lt "$MIN_FREE_GB" ]; then
  PY="$INFRA/.venv/bin/python"
  [ -x "$PY" ] || PY="/opt/anaconda3/bin/python3"
  "$PY" "$INFRA/agents/notify.py" \
    "Storage maintenance завершён, но свободно только ${after:-0}GB (нужно от ${MIN_FREE_GB}GB)." \
    --level warn >/dev/null 2>&1 || true
fi
