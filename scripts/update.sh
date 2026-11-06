#!/usr/bin/env bash
set -euo pipefail
APP_DIR=/opt/rss-tg-bot
cd "$APP_DIR"

if [[ ! -d .git ]]; then
  echo "当前安装不是 Git 工作副本，无法使用 update.sh 自动更新。"
  echo "请在 GitHub 仓库建立后，按 README 的迁移步骤重新部署。"
  exit 1
fi

git pull --ff-only
"$APP_DIR/.venv/bin/pip" install -r requirements.txt
systemctl restart rss-tg-bot.service
systemctl --no-pager --full status rss-tg-bot.service
