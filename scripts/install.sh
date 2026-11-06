#!/usr/bin/env bash
# One-command installer for the RSS Telegram bot (Debian / Ubuntu, systemd).
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Soulleslie/rss-tg-bot/main/scripts/install.sh)
#
# Non-interactive use (e.g. cloud-init):
#   TG_BOT_TOKEN=123:abc ADMIN_USER_IDS=111 bash install.sh
#
# Running it again on a machine where the bot is already installed performs
# an update instead and never touches .env or the database.
set -euo pipefail

APP_DIR=/opt/rss-tg-bot
SERVICE=rss-tg-bot.service
REPO_URL=${REPO_URL:-https://github.com/Soulleslie/rss-tg-bot.git}
BRANCH=${BRANCH:-main}
TELEGRAM_API=${TELEGRAM_API:-https://api.telegram.org}
ENV_FILE="$APP_DIR/.env"

info() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[注意]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

have_tty() { (exec </dev/tty) 2>/dev/null; }
INTERACTIVE=0
have_tty && INTERACTIVE=1

ask() {  # ask <prompt> [silent]
  local answer
  printf '%s' "$1" >/dev/tty
  if [[ ${2:-} == silent ]]; then
    IFS= read -rs answer </dev/tty || true
    printf '\n' >/dev/tty
  else
    IFS= read -r answer </dev/tty || true
  fi
  answer=${answer//[[:space:]]/}
  printf '%s' "$answer"
}

get_env() { [[ -f "$ENV_FILE" ]] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 || true; }

set_env() {  # values are validated before calling, so they never contain '|'
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
  fi
}

check_token() {  # prints ok:<username>, invalid or unreachable
  local response code body
  # The token goes through curl's config on stdin so it never shows up in `ps`.
  if ! response=$(printf 'url = "%s/bot%s/getMe"\n' "$TELEGRAM_API" "$1" \
      | curl -sS --max-time 15 -w '\n%{http_code}' -K - 2>/dev/null); then
    echo unreachable
    return
  fi
  code=${response##*$'\n'}
  body=${response%$'\n'*}
  body=${body//[[:space:]]/}
  if [[ $code == 200 && $body == *'"ok":true'* ]]; then
    echo "ok:$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$body")"
  elif [[ $code == 401 || $code == 404 ]]; then
    echo invalid  # Telegram answers 401 Unauthorized / 404 Not Found for bad tokens
  else
    echo unreachable
  fi
}

# --- Preconditions ---------------------------------------------------------

[[ $(id -u) -eq 0 ]] || die "请用 root 运行（先执行 sudo -i 切换到 root，再运行安装命令）。"
command -v apt-get >/dev/null || die "目前只支持 Debian / Ubuntu 系统。"
command -v systemctl >/dev/null || die "需要 systemd，当前系统没有 systemctl。"

if [[ -d "$APP_DIR/.git" && -f "$ENV_FILE" ]] && grep -q '^TG_BOT_TOKEN=.\+' "$ENV_FILE"; then
  info "检测到机器人已经安装在 $APP_DIR，改为执行更新（不会改动 .env 和数据库）。"
  exec bash "$APP_DIR/scripts/update.sh"
fi

# --- 1. System packages ----------------------------------------------------

info "1/6 安装系统依赖（git、curl、Python）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates python3 python3-venv >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "需要 Python 3.10 或更高版本，当前是 $(python3 -V 2>&1)。请使用 Ubuntu 22.04 / Debian 12 或更新的系统。"

# --- 2. Code ---------------------------------------------------------------

info "2/6 下载代码到 $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
  echo "代码已存在，跳过下载。"
else
  if [[ -d "$APP_DIR" && -n $(ls -A "$APP_DIR") ]]; then
    die "$APP_DIR 已存在但不是本项目的 git 仓库。请先备份并移走这个目录，再重新运行。"
  fi
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

# --- 3. Bot token ----------------------------------------------------------

info "3/6 配置 Bot Token"
umask 077
[[ -f "$ENV_FILE" ]] || cp "$APP_DIR/.env.example" "$ENV_FILE"
chmod 600 "$ENV_FILE"

token=${TG_BOT_TOKEN:-$(get_env TG_BOT_TOKEN)}
from_env=0
[[ -n $token ]] && from_env=1
bot_username=""
hint_shown=0
while :; do
  if [[ -z $token ]]; then
    [[ $INTERACTIVE -eq 1 ]] || die "没有可以输入的终端。请这样运行：TG_BOT_TOKEN=你的Token bash install.sh"
    if [[ $hint_shown -eq 0 ]]; then
      echo "还没有 Token？在 Telegram 里找 @BotFather，发送 /newbot 创建机器人即可得到。" >/dev/tty
      hint_shown=1
    fi
    token=$(ask "请粘贴 Bot Token（输入时不会显示，粘贴后按回车）：" silent)
    from_env=0
  fi
  if [[ ! $token =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]]; then
    [[ $from_env -eq 1 || $INTERACTIVE -eq 0 ]] && die "Token 格式不正确。正确的格式类似 123456789:AAH...（数字、冒号、一串字母数字）。"
    warn "Token 格式不正确，应该类似 123456789:AAH...，请重新粘贴。"
    token=""
    continue
  fi
  result=$(check_token "$token")
  case $result in
    ok:*)
      bot_username=${result#ok:}
      info "Token 有效，机器人是 @${bot_username}"
      break
      ;;
    invalid)
      [[ $from_env -eq 1 || $INTERACTIVE -eq 0 ]] && die "Telegram 表示这个 Token 无效。请到 @BotFather 确认后重新运行。"
      warn "Telegram 表示这个 Token 无效，请检查后重新粘贴。"
      token=""
      ;;
    *)
      warn "暂时连不上 Telegram，无法验证 Token，先继续安装。如果启动后机器人没反应，请检查 Token 和服务器网络。"
      break
      ;;
  esac
done
set_env TG_BOT_TOKEN "$token"

# --- 4. Administrator (optional) -------------------------------------------

info "4/6 设置管理员（可以跳过）"
admins=${ADMIN_USER_IDS:-$(get_env ADMIN_USER_IDS)}
allowed=${ALLOWED_USER_IDS:-$(get_env ALLOWED_USER_IDS)}
if [[ -z $admins && $INTERACTIVE -eq 1 ]]; then
  echo "管理员可以添加和删除 RSS 源。不知道自己的用户ID的话直接按回车跳过，" >/dev/tty
  echo "装好后私聊机器人发送 /whoami 查看，再运行：rss-tg-bot admin 你的用户ID" >/dev/tty
  admins=$(ask "管理员用户ID（多个用英文逗号分隔，可留空）：")
fi
if [[ -n $admins && ! $admins =~ ^[0-9]+(,[0-9]+)*$ ]]; then
  warn "管理员用户ID格式不正确（应为数字，多个用英文逗号分隔），已跳过。之后可以运行 rss-tg-bot admin 你的用户ID 设置。"
  admins=""
fi
if [[ -n $admins && -z $allowed && $INTERACTIVE -eq 1 ]]; then
  reply=$(ask "是否只允许管理员使用这个机器人？其他人发消息会被拒绝 [y/N]：")
  [[ $reply =~ ^[Yy] ]] && allowed=$admins
fi
if [[ -n $allowed && ! $allowed =~ ^[0-9]+(,[0-9]+)*$ ]]; then
  warn "白名单格式不正确，已跳过。"
  allowed=""
fi
set_env ADMIN_USER_IDS "$admins"
set_env ALLOWED_USER_IDS "$allowed"

# --- 5. Python environment -------------------------------------------------

info "5/6 安装 Python 运行环境（需要一两分钟）"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
if ! (cd "$APP_DIR" && "$APP_DIR/.venv/bin/python" -m unittest discover -s tests >/tmp/rss-tg-bot-selftest.log 2>&1); then
  tail -n 30 /tmp/rss-tg-bot-selftest.log
  die "自检失败，完整日志：/tmp/rss-tg-bot-selftest.log。请把它发给开发者。"
fi
echo "自检通过。"

# --- 6. Service ------------------------------------------------------------

info "6/6 启动机器人并设置开机自启"
install -m 644 "$APP_DIR/systemd/$SERVICE" "/etc/systemd/system/$SERVICE"
chmod 755 "$APP_DIR/scripts/rss-tg-bot"
ln -sf "$APP_DIR/scripts/rss-tg-bot" /usr/local/bin/rss-tg-bot
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 5
if ! systemctl is-active --quiet "$SERVICE"; then
  journalctl -u "$SERVICE" -n 30 --no-pager || true
  die "机器人没有启动成功，请查看上面的日志。"
fi

echo
info "安装完成，机器人已经在运行。"
if [[ -n $bot_username ]]; then
  echo "下一步：打开 https://t.me/${bot_username}，给机器人发送 /start"
else
  echo "下一步：在 Telegram 里打开你的机器人，发送 /start"
fi
[[ -z $admins ]] && echo "设置管理员：私聊机器人发送 /whoami 查看用户ID，然后运行 rss-tg-bot admin 你的用户ID"
cat <<'EOF'

常用管理命令：
  rss-tg-bot status      查看运行状态
  rss-tg-bot update      一键更新
  rss-tg-bot uninstall   一键卸载
  rss-tg-bot             查看全部命令
EOF
