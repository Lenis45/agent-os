#!/usr/bin/env bash
# restore_test.sh — проверка восстановимости бэкапа (v3.0).
# «Бэкап без тест-restore не считается рабочим.»
#
# Берёт ПОСЛЕДНИЙ локальный бэкап, поднимает ОДНОРАЗОВЫЙ postgres-контейнер на
# временном порту, восстанавливает дампы обеих БД и проверяет, что таблицы есть
# и схема валидна. Контейнер удаляется в конце. Прод не затрагивается.
set -uo pipefail

INFRA="${INFRA_DIR:-$HOME/ai-infra}"
DEST_ROOT="${BACKUP_DEST:-$INFRA/backups/local}"
TMP_CONT="ai_restore_test_$$"
TMP_PORT="${RESTORE_PORT:-55432}"
PGPASS="restore_test_pwd"
DOCKER="${DOCKER_BIN:-$(command -v docker 2>/dev/null || true)}"
[ -n "$DOCKER" ] || DOCKER="/usr/local/bin/docker"
[ -x "$DOCKER" ] || DOCKER="/opt/homebrew/bin/docker"
log(){ echo "[restore-test $(date +%H:%M:%S)] $*"; }
fail(){ log "✗ FAIL: $*"; cleanup; exit 1; }
cleanup(){ "$DOCKER" rm -f "$TMP_CONT" >/dev/null 2>&1 || true; }
trap cleanup EXIT

[ -x "$DOCKER" ] || fail "docker CLI не найден (проверь DOCKER_BIN/PATH для launchd)"

if lsof -nP -iTCP:"$TMP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  if [ -n "${RESTORE_PORT:-}" ]; then
    fail "порт RESTORE_PORT=$TMP_PORT уже занят"
  fi
  for p in $(seq 55433 55460); do
    if ! lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
      log "  ! порт 55432 занят, использую :$p"
      TMP_PORT="$p"
      break
    fi
  done
  lsof -nP -iTCP:"$TMP_PORT" -sTCP:LISTEN >/dev/null 2>&1 && fail "нет свободного restore-порта в диапазоне 55433-55460"
fi

LATEST="$(ls -1dt "$DEST_ROOT"/20* 2>/dev/null | head -1)"
[ -n "${LATEST:-}" ] || fail "нет ни одного бэкапа в $DEST_ROOT (сначала запусти backup.sh)"
log "→ проверяю бэкап: $LATEST"

log "→ поднимаю одноразовый postgres:16-alpine на :$TMP_PORT"
"$DOCKER" run -d --name "$TMP_CONT" -e POSTGRES_PASSWORD="$PGPASS" \
  -e POSTGRES_USER=agent_user -e POSTGRES_DB=postgres \
  -p "$TMP_PORT:5432" postgres:16-alpine >/dev/null 2>&1 \
  || fail "не удалось поднять временный контейнер"

# ждём готовности
for i in $(seq 1 30); do
  "$DOCKER" exec "$TMP_CONT" pg_isready -U agent_user >/dev/null 2>&1 && break
  sleep 1
  [ "$i" = "30" ] && fail "postgres не поднялся за 30с"
done
log "  ✓ временный postgres готов"

overall=0
for db in agents ops_db n8n customer_db; do
  dump="$LATEST/pg_${db}.sql.gz"
  [ -f "$dump" ] || { log "  ! $dump отсутствует — пропуск"; continue; }
  "$DOCKER" exec "$TMP_CONT" psql -U agent_user -d postgres -c "CREATE DATABASE $db OWNER agent_user" >/dev/null 2>&1
  if gunzip -c "$dump" | "$DOCKER" exec -i "$TMP_CONT" psql -U agent_user -d "$db" >/dev/null 2>&1; then
    tables=$("$DOCKER" exec "$TMP_CONT" psql -U agent_user -d "$db" -tAc \
      "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
    tables="${tables//[[:space:]]/}"
    if [ "${tables:-0}" -gt 0 ]; then
      log "  ✓ $db восстановлена: $tables таблиц"
    else
      log "  ✗ $db восстановлена, но 0 таблиц"; overall=1
    fi
  else
    log "  ✗ восстановление $db FAILED"; overall=1
  fi
done

PY="${PY:-/opt/anaconda3/bin/python3}"
if [ $overall -eq 0 ]; then
  log "✓ PASS — бэкап восстановим"
  ( cd "$INFRA/agents" && "$PY" -c "import ops_store; ops_store.record_run('restore_test','ok',{'backup':'$LATEST'}); ops_store.heartbeat('restore_test','ok')" 2>/dev/null ) || true
else
  log "✗ PARTIAL/FAIL — см. выше"
  ( cd "$INFRA/agents" && "$PY" -c "import ops_store; ops_store.record_run('restore_test','fail',{'backup':'$LATEST'}); ops_store.heartbeat('restore_test','fail')" 2>/dev/null ) || true
  "$PY" "$INFRA/agents/notify.py" "Restore-test FAILED для бэкапа $LATEST — бэкап может быть невосстановим!" --level crit >/dev/null 2>&1 || true
fi
exit $overall
