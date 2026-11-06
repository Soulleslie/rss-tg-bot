#!/usr/bin/env bash
# Back up .env (contains the bot token) and the SQLite database.
# Usage: sudo bash scripts/backup.sh [backup-directory]
set -euo pipefail
umask 077  # backups contain the bot token: readable by root only

APP_DIR=/opt/rss-tg-bot
DEST=${1:-/root/rss-tg-bot-backups}
PYTHON="$APP_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON=python3

mkdir -p "$DEST"
chmod 700 "$DEST"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

files=()
if [[ -f "$APP_DIR/.env" ]]; then
  cp -p "$APP_DIR/.env" "$work/.env"
  files+=(.env)
fi
if [[ -f "$APP_DIR/rss_tg_bot.db" ]]; then
  # Use SQLite's online backup API: safe while the bot is running and it also
  # includes data still held in the WAL file, unlike copying the file directly.
  "$PYTHON" - "$APP_DIR/rss_tg_bot.db" "$work/rss_tg_bot.db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1], timeout=30)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
target.close()
source.close()
PY
  files+=(rss_tg_bot.db)
fi
[[ ${#files[@]} -gt 0 ]] || { echo "没有找到可备份的配置或数据库。"; exit 1; }

archive="$DEST/rss-tg-bot-$stamp.tar.gz"
tar -C "$work" -czf "$archive" "${files[@]}"
chmod 600 "$DEST"/rss-tg-bot-*.tar.gz
find "$DEST" -name 'rss-tg-bot-*.tar.gz' -mtime +30 -delete
echo "备份完成：$archive"
