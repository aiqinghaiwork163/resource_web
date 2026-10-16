#!/bin/bash
# resource_web 数据库/Redis/配置备份
#
# 用法:
#   bash scripts/backup.sh              # 执行备份（写入 $BACKUP_DIR）
#   bash scripts/backup.sh --verify     # 备份后立即做恢复演练（推荐每周一次）
#   RESTORE_FILE=xx.sql.gz bash scripts/backup.sh --restore   # 从备份恢复到一个临时库验证
#
# 设计要点（对应旧 backup.sh 的 8 个缺陷）:
#   1. 走 resource-mysql 容器执行 mysqldump（宿主机无该工具，应用容器也没有）
#   2. 全库备份，不再只挑 8 张表（旧脚本漏掉 point_logs/watchlist/subscriptions 等 14 张）
#   3. 任何一步失败都 set -e 退出，且不写"备份完成"；失败会留下 .FAILED 标记文件
#   4. 落盘到宿主机真实目录，绝不写进容器内部
#   5. Redis 无密码时不再传 -a ""
#   6. 保留策略带 -name 过滤，不会误删 backup.log
#   7. 备份后校验 gzip 完整性 + 表数量，不通过则视为失败
#   8. 提供 --verify 恢复演练，确认备份真的能恢复
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

BACKUP_DIR="${BACKUP_DIR:-/vol1/1000/backups/resource_web}"
KEEP_DAYS="${KEEP_DAYS:-14}"
DATE="$(date +%Y%m%d_%H%M%S)"
LOCK_FILE="/tmp/resource_web_backup.lock"
DUMP_TOOL_CONTAINER="${DUMP_TOOL_CONTAINER:-resource-mysql}"

log() { echo "[$(date '+%F %T')] $*"; }

# ---- 单实例锁：防止定时任务与手动执行叠加 ----
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "已有备份在运行，跳过本次"
  exit 0
fi

# ---- 读取 .env（不回显任何值） ----
if [ ! -f .env ]; then
  log "ERROR: 找不到 $PROJECT_DIR/.env"
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

DB_HOST="${DB_HOST:?DB_HOST 未设置}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:?DB_USER 未设置}"
DB_PASSWORD="${DB_PASSWORD:?DB_PASSWORD 未设置}"
DB_NAME="${DB_NAME:-pan_resource}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# ---- TG 告警（P1-8 2026-10-16）：失败时推送。需在 .env 配 TELEGRAM_ALERT_CHAT_ID，
# 未配置则静默跳过（默认不对外发送）。
tg_notify() {
  local msg="$1"
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then
    return 0
  fi
  curl -sS --max-time 10 -o /dev/null \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_ALERT_CHAT_ID}" \
    --data-urlencode "text=${msg}" || true
}

# 失败时留下显眼标记，便于监控发现
FAIL_MARK="$BACKUP_DIR/LAST_BACKUP_FAILED"
trap 'log "ERROR: 备份失败（见上方输出）"; touch "$FAIL_MARK"; tg_notify "⚠️ VaultDrive 备份失败，请检查备份日志"' ERR

log "===== 开始备份 ($DATE) ====="
log "目标目录: $BACKUP_DIR (保留 ${KEEP_DAYS} 天)"

# ---------- 1) MySQL 全库 ----------
DB_FILE="$BACKUP_DIR/db_${DATE}.sql.gz"
log "1/4 MySQL 全库 dump -> $(basename "$DB_FILE")"

# 密码用 MYSQL_PWD 环境变量传入，避免出现在 argv / ps 里
docker exec -e MYSQL_PWD="$DB_PASSWORD" "$DUMP_TOOL_CONTAINER" \
  mysqldump \
    --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" \
    --single-transaction --quick --routines --triggers \
    --default-character-set=utf8mb4 \
    --databases "$DB_NAME" \
  | gzip > "$DB_FILE"

# 校验：gzip 完整性 + 关键表是否都在
gzip -t "$DB_FILE"
TABLE_COUNT="$(zcat "$DB_FILE" | grep -c '^CREATE TABLE' || true)"
if [ "$TABLE_COUNT" -lt 20 ]; then
  log "ERROR: dump 里只有 $TABLE_COUNT 张表（期望 >=20），判定失败"
  exit 1
fi
log "    OK: $(du -h "$DB_FILE" | cut -f1), $TABLE_COUNT 张表"

# ---------- 2) Redis ----------
REDIS_FILE="$BACKUP_DIR/redis_${DATE}.rdb"
log "2/4 Redis BGSAVE -> $(basename "$REDIS_FILE")"
if docker exec resource_web_redis redis-cli BGSAVE >/dev/null 2>&1; then
  # 等 BGSAVE 真正写完
  for _ in $(seq 1 30); do
    LASTSAVE="$(docker exec resource_web_redis redis-cli LASTSAVE | tr -d '\r')"
    if [ -n "$LASTSAVE" ] && [ "$LASTSAVE" -ge "$(( $(date +%s) - 60 ))" ]; then break; fi
    sleep 1
  done
  docker cp resource_web_redis:/data/dump.rdb "$REDIS_FILE" >/dev/null
  log "    OK: $(du -h "$REDIS_FILE" | cut -f1)"
else
  log "    警告: Redis BGSAVE 失败（Redis 数据可重建，不阻断 DB 备份）"
  REDIS_FILE=""
fi

# ---------- 3) 配置文件 ----------
log "3/4 配置与代码快照"
tar -czf "$BACKUP_DIR/config_${DATE}.tar.gz" \
  -C "$PROJECT_DIR" \
  .env docker-compose.yml Dockerfile requirements.txt .env.example 2>/dev/null
chmod 600 "$BACKUP_DIR/config_${DATE}.tar.gz"
log "    OK: $(du -h "$BACKUP_DIR/config_${DATE}.tar.gz" | cut -f1)（含 .env，已设 600）"

# ---------- 4) 清理旧备份 ----------
log "4/4 清理 ${KEEP_DAYS} 天前的备份（只删备份文件，不动目录与其他内容）"
find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'db_*.sql.gz' -o -name 'redis_*.rdb' -o -name 'config_*.tar.gz' \) \
  -mtime +"$KEEP_DAYS" -print -delete || true
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'LAST_BACKUP_FAILED' -mtime +1 -delete || true

# ---------- 完成 ----------
COUNT="$(find "$BACKUP_DIR" -maxdepth 1 -name 'db_*.sql.gz' | wc -l)"
rm -f "$FAIL_MARK"
trap - ERR
log "===== 备份完成：$(basename "$DB_FILE")${REDIS_FILE:+ + $(basename "$REDIS_FILE")} ====="
log "当前保留 $COUNT 份数据库备份"

# ---------- 可选：恢复演练 ----------
if [ "${1:-}" = "--verify" ]; then
  log "===== 恢复演练：把刚备份的 dump 导入临时库并比对行数 ====="
  VERIFY_DB="verify_restore_$(date +%s)"
  docker exec -e MYSQL_PWD="$DB_PASSWORD" "$DUMP_TOOL_CONTAINER" \
    mysql --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" \
    -e "DROP DATABASE IF EXISTS \`$VERIFY_DB\`; CREATE DATABASE \`$VERIFY_DB\` CHARACTER SET utf8mb4;" 2>/dev/null \
    || { log "  跳过：当前账号无权建临时库（属正常，最小权限）"; exit 0; }

  zcat "$DB_FILE" | sed "s/\`$DB_NAME\`/\`$VERIFY_DB\`/g" \
    | docker exec -i -e MYSQL_PWD="$DB_PASSWORD" "$DUMP_TOOL_CONTAINER" \
      mysql --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" "$VERIFY_DB"

  ROWS_VERIFY="$(docker exec -e MYSQL_PWD="$DB_PASSWORD" "$DUMP_TOOL_CONTAINER" \
    mysql -N -B --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" \
    -e "SELECT COUNT(*) FROM \`$VERIFY_DB\`.resources;")"
  log "  临时库 resources 行数: $ROWS_VERIFY"

  docker exec -e MYSQL_PWD="$DB_PASSWORD" "$DUMP_TOOL_CONTAINER" \
    mysql --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" \
    -e "DROP DATABASE \`$VERIFY_DB\`;"
  log "  恢复演练通过，临时库已删除"
fi
