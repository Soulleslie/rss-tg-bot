#!/usr/bin/env bash
# Update the bot from GitHub: back up, pull, install dependencies, self-test,
# refresh the systemd unit and management command, then restart.
# Usage: rss-tg-bot update   (or: sudo bash /opt/rss-tg-bot/scripts/update.sh)
set -euo pipefail

APP_DIR=/opt/rss-tg-bot
SERVICE=rss-tg-bot.service
PYTHON="$APP_DIR/.venv/bin/python"

info() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[注意]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }
g() { git -C "$APP_DIR" -c safe.directory="$APP_DIR" "$@"; }

[[ $(id -u) -eq 0 ]] || die "请用 root 运行：sudo rss-tg-bot update"
[[ -d "$APP_DIR/.git" ]] || die "$APP_DIR 不是 Git 工作副本，无法自动更新。请先运行 scripts/backup.sh 备份，再用一键安装命令重新部署。"
cd "$APP_DIR"

# A checkout owned by another user (e.g. files copied from a Mac keep UID 501)
# makes git refuse to work ("dubious ownership").  Older git releases, such as
# the one in Ubuntu 22.04, ignore `-c safe.directory`, so trust this one
# directory in the system git config, which every git version honours.
owner=$(stat -c %u "$APP_DIR")
git_owner=$(stat -c %u "$APP_DIR/.git")
if [[ $owner != 0 || $git_owner != 0 ]]; then
  warn "$APP_DIR 的所有者不是 root（UID $owner），通常是从其他电脑上传文件造成的。"
  warn "机器人以 root 身份运行这些代码，建议改回 root：chown -R root:root $APP_DIR"
  if ! git config --system --get-all safe.directory 2>/dev/null | grep -qxF "$APP_DIR"; then
    git config --system --add safe.directory "$APP_DIR"
    info "已让 git 信任 $APP_DIR（只对这一个目录生效），继续更新。"
  fi
fi

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
