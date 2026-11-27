#!/usr/bin/env bash
# Online SQLite backup (safe while the bot is running, WAL mode).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${BMS_DB_FILE:-$ROOT/data/bmsbot.db}"
DEST="${BMS_BACKUP_DIR:-$HOME/bms-backups}"
KEEP_DAYS="${BMS_BACKUP_KEEP_DAYS:-7}"

mkdir -p "$DEST"
STAMP="$(date +%F-%H%M)"
OUT="$DEST/bmsbot-$STAMP.db"

if [ ! -f "$DB" ]; then
  echo "database not found: $DB" >&2
  exit 1
fi

sqlite3 "$DB" ".backup '$OUT'"
sqlite3 "$OUT" "pragma quick_check;" >/dev/null
find "$DEST" -name 'bmsbot-*.db' -mtime +"$KEEP_DAYS" -delete

echo "backup written: $OUT ($(du -h "$OUT" | cut -f1))"
