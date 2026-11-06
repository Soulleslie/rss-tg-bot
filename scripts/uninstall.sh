#!/usr/bin/env bash
# Remove the bot completely.  A backup of .env and the database is made first
# and kept in /root/rss-tg-bot-backups.
# Usage: rss-tg-bot uninstall [--yes]
set -euo pipefail

APP_DIR=/opt/rss-tg-bot
SERVICE=rss-tg-bot.service
BACKUP_DIR=/root/rss-tg-bot-backups

info() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "请用 root 运行：sudo rss-tg-bot uninstall"

if [[ ${1:-} != "--yes" ]]; then
  cat <<EOF
即将卸载 RSS 提醒机器人：
  · 停止并删除系统服务 $SERVICE
  · 删除管理命令 /usr/local/bin/rss-tg-bot
  · 删除整个目录 $APP_DIR（包括 .env 中的 Token、数据库和推送历史）

卸载前会自动备份 .env 和数据库到 $BACKUP_DIR，这个备份不会被删除。
EOF
  (exec </dev/tty) 2>/dev/null || die "没有可以输入的终端。确认要卸载的话，请运行：rss-tg-bot uninstall --yes"
  printf '确认卸载请输入 DELETE：' >/dev/tty
  IFS= read -r answer </dev/tty || true
  [[ $answer == "DELETE" ]] || { echo "已取消，没有做任何改动。"; exit 0; }
fi

if [[ -f "$APP_DIR/scripts/backup.sh" ]] && { [[ -f "$APP_DIR/.env" ]] || [[ -f "$APP_DIR/rss_tg_bot.db" ]]; }; then
  info "备份 .env 和数据库"
  bash "$APP_DIR/scripts/backup.sh" "$BACKUP_DIR" || die "备份失败，为安全起见已停止卸载。"
fi

info "停止并删除服务"
systemctl disable --now "$SERVICE" 2>/dev/null || true
rm -f "/etc/systemd/system/$SERVICE"
systemctl daemon-reload

if [[ -L /usr/local/bin/rss-tg-bot ]]; then
  rm -f /usr/local/bin/rss-tg-bot
fi

info "删除 $APP_DIR"
rm -rf "$APP_DIR"

echo
info "卸载完成。"
[[ -d $BACKUP_DIR ]] && echo "备份保留在 $BACKUP_DIR，不需要的话可以运行：rm -rf $BACKUP_DIR"
echo "系统自带的 Python、git 等软件包没有删除，因为其他程序可能也在使用。"
