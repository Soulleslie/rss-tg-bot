#!/usr/bin/env bash
# Update the bot from GitHub: back up, pull, install dependencies, self-test,
# refresh the systemd unit and management command, then restart.
# Usage: rss-tg-bot update   (or: sudo bash /opt/rss-tg-bot/scripts/update.sh)
set -euo pipefail

APP_DIR=/opt/rss-tg-bot
SERVICE=rss-tg-bot.service
PYTHON="$APP_DIR/.venv/bin/python"

info() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }
# safe.directory: tolerate a checkout owned by another user (e.g. copied from a Mac).
g() { git -C "$APP_DIR" -c safe.directory="$APP_DIR" "$@"; }

[[ $(id -u) -eq 0 ]] || die "请用 root 运行：sudo rss-tg-bot update"
[[ -d "$APP_DIR/.git" ]] || die "$APP_DIR 不是 Git 工作副本，无法自动更新。请先运行 scripts/backup.sh 备份，再用一键安装命令重新部署。"
cd "$APP_DIR"

changed=$(g status --porcelain --untracked-files=no)
if [[ -n $changed ]]; then
  echo "$changed" >&2
  die "服务器上的代码文件被修改过（见上面的列表），为避免覆盖，已停止更新。配置请只改 .env；如果确认这些改动不需要，运行 cd $APP_DIR && git checkout -- . 后再更新。"
fi
branch=$(g rev-parse --abbrev-ref HEAD)
remote=$(g remote | head -n 1)
[[ -n $remote ]] || die "没有配置远程仓库。请运行：git -C $APP_DIR remote add origin https://github.com/Soulleslie/rss-tg-bot.git"

info "1/5 备份 .env 和数据库"
bash "$APP_DIR/scripts/backup.sh"

info "2/5 拉取最新代码"
before=$(g rev-parse --short HEAD)
g fetch -q "$remote" "$branch"
g merge -q --ff-only FETCH_HEAD || die "无法快进到最新版本（本地历史和 GitHub 不一致）。请把这条输出发给开发者。"
after=$(g rev-parse --short HEAD)
if [[ $before == "$after" ]]; then
  echo "已经是最新版本：$after"
else
  echo "版本：$before -> $after"
  g log -n 20 --no-merges --format='  · %s' "$before..$after"
fi

info "3/5 安装依赖"
"$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r requirements.txt

info "4/5 自检（使用临时数据库，不会影响正式数据）"
if [[ -d tests ]]; then
  if ! "$PYTHON" -m unittest discover -s tests >/tmp/rss-tg-bot-selftest.log 2>&1; then
    tail -n 40 /tmp/rss-tg-bot-selftest.log
    echo "如需回到旧版本：cd $APP_DIR && git reset --hard $before" >&2
    die "自检失败：服务没有重启，仍在运行旧版本。完整日志：/tmp/rss-tg-bot-selftest.log"
  fi
  echo "自检通过"
else
  "$PYTHON" -m py_compile app.py
fi

info "5/5 更新服务和管理命令，并重启"
install -m 644 "$APP_DIR/systemd/$SERVICE" "/etc/systemd/system/$SERVICE"
if [[ -f "$APP_DIR/scripts/rss-tg-bot" ]]; then
  chmod 755 "$APP_DIR/scripts/rss-tg-bot"
  ln -sf "$APP_DIR/scripts/rss-tg-bot" /usr/local/bin/rss-tg-bot
fi
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"
sleep 5
if ! systemctl is-active --quiet "$SERVICE"; then
  journalctl -u "$SERVICE" -n 50 --no-pager || true
  die "服务启动失败，请查看上面的日志。回到旧版本：cd $APP_DIR && git reset --hard $before && systemctl restart $SERVICE"
fi
info "更新完成，机器人运行正常。以后更新只需运行：rss-tg-bot update"
