#!/usr/bin/env bash
set -euo pipefail
APP_DIR=/opt/rss-tg-bot
DEST=${1:-/root/rss-tg-bot-backups}
mkdir -p "$DEST"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
files=()
[[ -f "$APP_DIR/.env" ]] && files+=(.env)
[[ -f "$APP_DIR/rss_tg_bot.db" ]] && files+=(rss_tg_bot.db)
[[ ${#files[@]} -gt 0 ]] || { echo "没有找到可备份的配置或数据库。"; exit 1; }
tar -C "$APP_DIR" -czf "$DEST/rss-tg-bot-$stamp.tar.gz" "${files[@]}"
find "$DEST" -name 'rss-tg-bot-*.tar.gz' -mtime +30 -delete
echo "Backup written to $DEST/rss-tg-bot-$stamp.tar.gz"
