#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/rss-tg-bot
SERVICE=rss-tg-bot.service

if [[ $(id -u) -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

echo "This stops $SERVICE, removes /etc/systemd/system/$SERVICE, and permanently deletes $APP_DIR (including .env and the SQLite history)."
read -r -p "Type DELETE to continue: " answer
[[ "$answer" == "DELETE" ]] || { echo "Cancelled."; exit 0; }

systemctl disable --now "$SERVICE" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
rm -rf "$APP_DIR"
echo "RSS Telegram bot has been fully removed. The python3-venv system package was intentionally kept because other software may use it."
