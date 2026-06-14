#!/usr/bin/env bash
# backup.sh — бэкап AI-инфры (v3.0 hardened).
# Закрывает риск #1: сбой внутреннего SSD = потеря CRM/истории/сессий.
#
# Делает: дампы БД (agents+ops_db+n8n), Qdrant-снапшоты, код агентов+.session+токены,
# Obsidian. Проверяет целостность (gzip -t). Копирует OFF-SITE на ВНЕШНИЙ диск
# (отдельный физический носитель — переживает отказ SSD). Контролирует свободное место.
# Алертит в Telegram при сбое/без off-site. Пишет статус и heartbeat в ops_db.
#
# Best-effort: падение компонента не валит прогон, но понижает итоговый статус.
set -uo pipefail

# launchd запускает с урезанным PATH (/usr/bin:/bin:/usr/sbin:/sbin) — без /usr/local/bin,
# где лежит docker. Без этого все pg_dump падали ночью («docker: command not found»).
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

INFRA="${INFRA_DIR:-$HOME/ai-infra}"
DEST_ROOT="${BACKUP_DEST:-$INFRA/backups/local}"
PG_CONTAINER="${PG_CONTAINER:-ai_postgres}"
PG_USER="${PG_USER:-agent_user}"
DOCKER="${DOCKER:-$(command -v docker || echo /usr/local/bin/docker)}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
OBSIDIAN="${OBSIDIAN_VAULT:-$HOME/Knowledge_base}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
MIN_FREE_GB="${MIN_FREE_GB:-5}"
PY="${PY:-/opt/anaconda3/bin/python3}"

STAMP="$(date +%Y-%m-%d_%H%M%S)"
DEST="$DEST_ROOT/$STAMP"
mkdir -p "$DEST"
status=0; offsite="none"; warnings=()
log(){ echo "[backup $(date +%H:%M:%S)] $*"; }
notify(){ "$PY" "$INFRA/agents/notify.py" "$1" --level "${2:-info}" >/dev/null 2>&1 || true; }

log "→ destination: $DEST"

# ── 0. Pre-flight: свободное место на ДАННЫХ (macOS: df / = запечатанный сис-том!) ──
free_gb=$(df -g "$HOME" 2>/dev/null | awk 'NR==2{print $4}')
log "→ свободно на Data-томе: ${free_gb}GB (порог ${MIN_FREE_GB}GB)"
if [ "${free_gb:-0}" -lt "$MIN_FREE_GB" ]; then
  warnings+=("мало места: ${free_gb}GB")
  notify "Backup: мало места на диске — ${free_gb}GB свободно (порог ${MIN_FREE_GB}GB)" warn
fi

# ── 1. PostgreSQL: per-database dump + проверка целостности ──
for db in agents ops_db n8n customer_db; do
  if "$DOCKER" exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$db" --no-owner 2>/dev/null | gzip > "$DEST/pg_${db}.sql.gz"; then
    if gzip -t "$DEST/pg_${db}.sql.gz" 2>/dev/null; then
      log "  ✓ postgres/$db → pg_${db}.sql.gz ($(du -h "$DEST/pg_${db}.sql.gz" | cut -f1))"
    else
      log "  ✗ postgres/$db — повреждён архив"; status=1
    fi
  else
    log "  ✗ postgres/$db FAILED"; status=1
  fi
done

# ── 2. Qdrant: snapshot каждой коллекции ──
mkdir -p "$DEST/qdrant"
cols=$(curl -s "$QDRANT_URL/collections" | "$PY" -c "import sys,json;print(' '.join(c['name'] for c in json.load(sys.stdin)['result']['collections']))" 2>/dev/null)
if [ -n "${cols:-}" ]; then
  for c in $cols; do
    snap=$(curl -s -X POST "$QDRANT_URL/collections/$c/snapshots" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['result']['name'])" 2>/dev/null)
    if [ -n "${snap:-}" ] && curl -s "$QDRANT_URL/collections/$c/snapshots/$snap" -o "$DEST/qdrant/${c}.snapshot" && [ -s "$DEST/qdrant/${c}.snapshot" ]; then
      log "  ✓ qdrant/$c → ${c}.snapshot"
      # чистим снапшот на сервере qdrant, чтобы не копить
      curl -s -X DELETE "$QDRANT_URL/collections/$c/snapshots/$snap" >/dev/null 2>&1 || true
    else
      log "  ✗ qdrant/$c snapshot FAILED"; status=1
    fi
  done
else
  log "  ! qdrant: коллекции не получены"; warnings+=("qdrant недоступен")
fi

# ── 3. Код агентов + критичные секреты/сессии (без гигантских логов) ──
if tar -czf "$DEST/agents_code.tar.gz" -C "$INFRA" \
      --exclude='agents/*.log' --exclude='agents/*.log.*' --exclude='agents/__pycache__' \
      agents 2>/dev/null && gzip -t "$DEST/agents_code.tar.gz" 2>/dev/null; then
  log "  ✓ agents code+sessions → agents_code.tar.gz ($(du -h "$DEST/agents_code.tar.gz" | cut -f1))"
else
  log "  ✗ agents tar FAILED"; status=1
fi

# ── 4. Obsidian vault ──
if [ "${BACKUP_OBSIDIAN:-1}" = "1" ] && [ -d "$OBSIDIAN" ]; then
  if tar -czf "$DEST/obsidian.tar.gz" -C "$(dirname "$OBSIDIAN")" "$(basename "$OBSIDIAN")" 2>/dev/null; then
    log "  ✓ obsidian → obsidian.tar.gz ($(du -h "$DEST/obsidian.tar.gz" | cut -f1))"
  else
    log "  ✗ obsidian tar FAILED"; status=1
  fi
fi

# ── 5. Манифест + контрольные суммы ──
( cd "$DEST" && find . -type f ! -name 'SHA256SUMS' -exec shasum -a 256 {} \; > SHA256SUMS 2>/dev/null ) || true
{
  echo "backup: $STAMP"; echo "host: $(hostname)"
  echo "status: $([ $status -eq 0 ] && echo OK || echo PARTIAL)"
  echo "free_gb_internal: ${free_gb}"
  echo "files:"; ( cd "$DEST" && find . -type f -exec du -h {} \; )
} > "$DEST/MANIFEST.txt"

# ── 6. OFF-SITE #1: внешний физический диск (главная защита от отказа SSD) ──
EXT=""
if [ -n "${EXTERNAL_BACKUP_DIR:-}" ] && [ -w "${EXTERNAL_BACKUP_DIR:-/nonexistent}" ]; then
  EXT="$EXTERNAL_BACKUP_DIR"
else
  for v in /Volumes/*/; do
    [ "$v" = "/Volumes/Macintosh HD/" ] && continue
    if touch "${v}.amori_wtest" 2>/dev/null; then rm -f "${v}.amori_wtest"; EXT="${v}amori-backups"; break; fi
  done
fi
if [ -n "$EXT" ]; then
  mkdir -p "$EXT/$STAMP"
  if cp -R "$DEST/." "$EXT/$STAMP/" 2>/dev/null; then
    # верификация: контрольные суммы на внешнем диске
    if ( cd "$EXT/$STAMP" && shasum -a 256 -c SHA256SUMS >/dev/null 2>&1 ); then
      offsite="external:$EXT"; log "  ✓ off-site → $EXT/$STAMP (checksums OK)"
    else
      offsite="external-unverified"; log "  ! off-site скопирован, но checksum-проверка не прошла"; warnings+=("offsite checksum fail")
    fi
    find "$EXT" -maxdepth 1 -type d -name '20*' -mtime +"$RETENTION_DAYS" -exec rm -rf {} \; 2>/dev/null || true
  else
    log "  ✗ копирование на внешний диск FAILED"; status=1; warnings+=("offsite copy fail")
  fi
else
  log "  ! внешний диск не найден — бэкап ТОЛЬКО на внутреннем SSD (не переживёт отказ диска!)"
  warnings+=("НЕТ off-site: подключи внешний диск или restic/B2")
fi

# ── 7. OFF-SITE #2: Restic → B2 (если настроен) ──
if command -v restic >/dev/null 2>&1 && [ -n "${RESTIC_REPOSITORY:-}" ]; then
  if restic backup "$DEST" --tag amori-infra >/dev/null 2>&1; then
    offsite="${offsite}+restic"; log "  ✓ off-site restic/B2 ok"
    restic forget --keep-daily 30 --keep-weekly 8 --prune >/dev/null 2>&1 || true
  else
    log "  ✗ restic push FAILED"; status=1; warnings+=("restic fail")
  fi
fi

# ── 8. Retention локально ──
find "$DEST_ROOT" -maxdepth 1 -type d -name '20*' -mtime +"$RETENTION_DAYS" -exec rm -rf {} \; 2>/dev/null || true

# ── 9. Ротация логов агентов (>10MB → gzip+truncate, держим 5 архивов) ──
LOG_MAX_BYTES="${LOG_MAX_BYTES:-10485760}"
for lf in "$INFRA"/agents/*.log; do
  [ -f "$lf" ] || continue
  sz=$(wc -c < "$lf" 2>/dev/null || echo 0)
  if [ "${sz:-0}" -gt "$LOG_MAX_BYTES" ]; then
    gzip -c "$lf" > "${lf}.$(date +%Y%m%d).gz" 2>/dev/null && : > "$lf" \
      && log "  ↻ rotated $(basename "$lf") (was $((sz/1024/1024))MB)"
    ls -1t "${lf}".*.gz 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true
  fi
done

# ── 10. Итог: статус, ops_db, алерты ──
final="OK"; [ $status -eq 0 ] || final="PARTIAL"
[ "$offsite" = "none" ] && final="${final}/NO-OFFSITE"
warn_str=$(IFS='; '; echo "${warnings[*]:-}")

# Машиночитаемый статус для монитора
cat > "$DEST_ROOT/status.json" <<EOF
{"ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","stamp":"$STAMP","status":"$final","offsite":"$offsite","free_gb":${free_gb:-0},"warnings":"$warn_str","dest":"$DEST"}
EOF

# Запись в ops_db (heartbeat + run) — best-effort
( cd "$INFRA/agents" && "$PY" -c "
import ops_store
ops_store.record_run('backup', '$([ $status -eq 0 ] && echo ok || echo partial)', {'offsite':'$offsite','free_gb':${free_gb:-0},'warnings':'$warn_str','stamp':'$STAMP'})
ops_store.heartbeat('backup', '$([ $status -eq 0 ] && echo ok || echo partial)', {'offsite':'$offsite'})
" 2>/dev/null ) || true

# Алерты в Telegram
if [ $status -ne 0 ]; then
  notify "Backup PARTIAL/FAIL ($STAMP): ${warn_str:-см. лог}" crit
elif [ "$offsite" = "none" ]; then
  notify "Backup OK, но БЕЗ off-site — данные только на внутреннем SSD. Подключи внешний диск." warn
fi

log "готово. status=$final offsite=$offsite. latest=$DEST"
exit $status
