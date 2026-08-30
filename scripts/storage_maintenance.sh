#!/usr/bin/env bash
# Reclaim reproducible caches and report storage conditions that need operator action.
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

INFRA="${INFRA_DIR:-$HOME/ai-infra}"
DOCKER="${DOCKER:-$(command -v docker || echo /usr/local/bin/docker)}"
PY="${PYTHON:-$INFRA/.venv/bin/python}"
[ -x "$PY" ] || PY="/opt/anaconda3/bin/python3"

CACHE_MAX_AGE="${DOCKER_BUILD_CACHE_MAX_AGE:-24h}"
IMAGE_MAX_AGE="${DOCKER_IMAGE_CACHE_MAX_AGE:-168h}"
MIN_FREE_GB="${STORAGE_MIN_FREE_GB:-20}"
CRITICAL_FREE_GB="${STORAGE_CRITICAL_FREE_GB:-5}"
STATUS_FILE="${STORAGE_STATUS_FILE:-$INFRA/agents/runtime/storage_maintenance.json}"
UPDATE_PLIST="${MACOS_UPDATE_PLIST:-/System/Volumes/Update/Update.plist}"
UPDATE_DIR="${MACOS_UPDATE_DIR:-/System/Volumes/Update}"
CRYPTEX_INCOMING_DIR="${MACOS_CRYPTEX_INCOMING_DIR:-/System/Volumes/Preboot/Cryptexes/Incoming}"

mkdir -p "$(dirname "$STATUS_FILE")"

free_gb() {
  df -g "$HOME" 2>/dev/null | awk 'NR == 2 {print $4}'
}

dir_size_gb() {
  local path="$1"
  if [ ! -e "$path" ]; then
    echo 0
    return
  fi
  du -sk "$path" 2>/dev/null | awk '{printf "%d\n", ($1 + 1048575) / 1048576}'
}

clear_reproducible_dir() {
  local path="$1"
  [ -d "$path" ] || return 0
  find "$path" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + 2>/dev/null || true
}

previous_action="$($PY - "$STATUS_FILE" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.exists():
    print(json.loads(path.read_text(encoding="utf-8")).get("action", "none"))
PY
)"
previous_action="${previous_action:-none}"

before="$(free_gb)"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[storage] free before: ${before:-unknown}GB"

docker_status="unavailable"
if [ -x "$DOCKER" ] && "$DOCKER" info >/dev/null 2>&1; then
  docker_status="ok"
  echo "[storage] pruning Docker build cache unused for $CACHE_MAX_AGE"
  "$DOCKER" builder prune -af --filter "until=$CACHE_MAX_AGE" || true
  echo "[storage] pruning dangling Docker images unused for $IMAGE_MAX_AGE"
  "$DOCKER" image prune -f --filter "until=$IMAGE_MAX_AGE" || true
else
  echo "[storage] Docker is unavailable; cache pruning skipped"
fi

after_docker="$(free_gb)"
package_cleanup="skipped"
if [ "${after_docker:-0}" -lt "$MIN_FREE_GB" ]; then
  package_cleanup="completed"
  echo "[storage] low space: cleaning reproducible package-manager caches"
  "$PY" -m pip cache purge >/dev/null 2>&1 || true
  command -v uv >/dev/null 2>&1 && uv cache clean >/dev/null 2>&1 || true
  command -v npm >/dev/null 2>&1 && npm cache clean --force >/dev/null 2>&1 || true
  command -v brew >/dev/null 2>&1 && brew cleanup -s >/dev/null 2>&1 || true
  command -v go >/dev/null 2>&1 && go clean -cache >/dev/null 2>&1 || true
  clear_reproducible_dir "$HOME/.cache/uv"
  clear_reproducible_dir "$HOME/Library/Developer/Xcode/DerivedData"

  # Docker Desktop can leave a complete installer copy after an interrupted update.
  docker_staging="$HOME/Library/Application Support/com.docker.install/in_progress"
  if [ -d "$docker_staging" ] && find "$docker_staging" -maxdepth 0 -mtime +1 -print -quit | grep -q .; then
    rm -rf -- "$docker_staging"
  fi
fi

after="$(free_gb)"
current_build="${MACOS_CURRENT_BUILD:-$(sw_vers -buildVersion 2>/dev/null || true)}"
target_build="${MACOS_TARGET_BUILD:-}"
if [ -z "$target_build" ] && [ -f "$UPDATE_PLIST" ]; then
  target_build="$($PY - "$UPDATE_PLIST" <<'PY' 2>/dev/null || true
import plistlib
import sys

with open(sys.argv[1], "rb") as stream:
    data = plistlib.load(stream)
print(data.get("update-asset-attributes", {}).get("Build", ""))
PY
)"
fi

update_gb="$(( $(dir_size_gb "$UPDATE_DIR") + $(dir_size_gb "$CRYPTEX_INCOMING_DIR") ))"
action="none"
status="ok"
if [ -n "$target_build" ] && [ "$target_build" != "$current_build" ]; then
  action="reboot_required"
  status="warn"
elif [ "${after:-0}" -lt "$MIN_FREE_GB" ]; then
  action="low_space"
  status="warn"
fi

echo "[storage] free after: ${after:-unknown}GB"
echo "[storage] action: $action; staged macOS data: ${update_gb}GB"

"$PY" - "$STATUS_FILE" "$started" "$status" "${before:-0}" "${after:-0}" \
  "$CACHE_MAX_AGE" "$IMAGE_MAX_AGE" "$docker_status" "$package_cleanup" \
  "$action" "$update_gb" "$current_build" "$target_build" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    status_file,
    timestamp,
    status,
    free_before,
    free_after,
    cache_max_age,
    image_max_age,
    docker_status,
    package_cleanup,
    action,
    update_gb,
    current_build,
    target_build,
) = sys.argv[1:]

payload = {
    "ts": timestamp,
    "status": status,
    "free_gb_before": int(free_before),
    "free_gb_after": int(free_after),
    "cache_max_age": cache_max_age,
    "image_max_age": image_max_age,
    "docker": docker_status,
    "package_cleanup": package_cleanup,
    "action": action,
    "macos_update_gb": int(update_gb),
    "current_build": current_build,
    "target_build": target_build,
}
path = Path(status_file)
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
temporary.replace(path)
PY

critical_repeat=0
if [ "${after:-0}" -lt "$CRITICAL_FREE_GB" ]; then
  critical_repeat=1
fi
if [ "$action" != "$previous_action" ] || [ "${STORAGE_NOTIFY_ALWAYS:-0}" = "1" ] || [ "$critical_repeat" = "1" ]; then
  if [ "$action" = "reboot_required" ]; then
    message="На Mac Mini подготовлено обновление macOS (${update_gb}GB системных данных). Свободно ${after:-0}GB. Нужен обычный перезапуск Mac, чтобы завершить обновление и освободить место. Сервисы запустятся автоматически."
  elif [ "$action" = "low_space" ]; then
    message="После безопасной очистки свободно только ${after:-0}GB (целевой минимум ${MIN_FREE_GB}GB). Нужна ручная проверка крупных файлов."
  else
    message="Хранилище Mac Mini восстановлено: свободно ${after:-0}GB, дополнительных действий не требуется."
  fi
  "$PY" "$INFRA/agents/notify.py" "$message" --level "$status" >/dev/null 2>&1 || true
fi
