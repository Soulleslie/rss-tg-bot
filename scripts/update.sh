#!/usr/bin/env bash
# Update the bot from GitHub: back up, pull, install dependencies, self-test,
# refresh the systemd unit and restart.
# Usage: sudo bash /opt/rss-tg-bot/scripts/update.sh
set -euo pipefail

APP_DIR=/opt/rss-tg-bot
SERVICE=rss-tg-bot.service
PYTHON="$APP_DIR/.venv/bin/python"

if [[ $(id -u) -ne 0 ]]; then
  echo "请用 root 运行：sudo bash $APP_DIR/scripts/update.sh" >&2
  exit 1
fi
cd "$APP_DIR"

if [[ ! -d .git ]]; then
  echo "当前安装不是 Git 工作副本，无法使用 update.sh 自动更新。"
  echo "请参考 README 重新用 git clone 部署（先运行 scripts/backup.sh 备份 .env 和数据库）。"
  exit 1
fi

echo "==> 1/5 备份 .env 和数据库"
bash "$APP_DIR/scripts/backup.sh"

echo "==> 2/5 拉取最新代码"
before=$(git rev-parse --short HEAD)
git pull --ff-only
after=$(git rev-parse --short HEAD)
echo "版本：$before -> $after"

echo "==> 3/5 安装依赖"
"$APP_DIR/.venv/bin/pip" install -q -r requirements.txt

echo "==> 4/5 自检（使用临时数据库，不会影响正式数据）"
if [[ -d tests ]]; then
  if ! "$PYTHON" -m unittest discover -s tests >/tmp/rss-tg-bot-selftest.log 2>&1; then
    tail -n 40 /tmp/rss-tg-bot-selftest.log
    echo "自检失败：服务未重启，仍在运行旧版本。完整日志：/tmp/rss-tg-bot-selftest.log" >&2
    echo "如需回到旧版本：cd $APP_DIR && git reset --hard $before" >&2
    exit 1
  fi
  echo "自检通过"
else
  "$PYTHON" -m py_compile app.py
fi

echo "==> 5/5 更新 systemd 服务并重启"
install -m 644 "$APP_DIR/systemd/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl restart "$SERVICE"
sleep 5
if ! systemctl is-active --quiet "$SERVICE"; then
  journalctl -u "$SERVICE" -n 50 --no-pager
  echo "服务启动失败，请查看上面的日志。回到旧版本：cd $APP_DIR && git reset --hard $before && systemctl restart $SERVICE" >&2
  exit 1
fi
systemctl --no-pager --full status "$SERVICE" | head -n 15
echo "更新完成。"
