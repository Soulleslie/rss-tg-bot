#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/rss-tg-bot
SERVICE=rss-tg-bot.service

if [[ $(id -u) -ne 0 ]]; then
  echo "Run this installer as root (for example: sudo bash scripts/install.sh)." >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "Created $APP_DIR/.env. Fill TG_BOT_TOKEN before enabling the service." >&2
  exit 0
fi

apt-get update
apt-get install -y python3-venv
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
install -m 644 "$APP_DIR/systemd/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable --now "$SERVICE"
systemctl --no-pager --full status "$SERVICE"
