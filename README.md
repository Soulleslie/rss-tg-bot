# RSS Telegram Bot

多用户 RSS 关键词提醒机器人。所有运行内容都集中在 `/opt/rss-tg-bot`。机器人不开放入站端口，通过 long polling 主动连接 Telegram。

每条命令的用法、示例和排错方法见 [完整使用手册](docs/USAGE.zh-CN.md)。

## 功能

- 普通关键词、组合关键词，可单独暂停或启用；支持屏蔽词
- 每个 RSS 源可分别设置“全量推送”或“关键词推送”，也可分别设置版块筛选
- 每位用户最多 10 个推送目标（私聊、群组、频道）
- 每位用户的规则互相独立，RSS 源由所有用户共享
- 用 SQLite 记录去重和推送历史，重启后保留；推送到一半时进程中断，重启后会接着推送，不会漏推，也不会重复推送
- 首次抓取只建立基线，不推送历史内容
- `/status` 会自动指出可能导致收不到推送的问题
- Telegram 输入框的 `/` 菜单会列出所有命令和说明
- 抓取有超时，并使用 ETag/Last-Modified 条件请求；内容没变时不会重复下载
- 遇到 Telegram 限流会自动等待重试

运行要求：Debian/Ubuntu，Python 3.10 及以上（python-telegram-bot 22.x 的要求；已在 3.10–3.13 上测试）。

## VPS 安装

```bash
git clone <YOUR_GITHUB_REPO_URL> /opt/rss-tg-bot
cd /opt/rss-tg-bot
sudo bash scripts/install.sh
sudo nano /opt/rss-tg-bot/.env
```

填写 `TG_BOT_TOKEN` 后，再运行一次安装脚本：

```bash
sudo bash scripts/install.sh
```

然后私聊机器人，发送 `/start` 和 `/whoami`。把返回的用户 ID 填进 `.env` 的 `ADMIN_USER_IDS`，再执行 `sudo systemctl restart rss-tg-bot`。建议同时把这个 ID 写进 `ALLOWED_USER_IDS`。

## 配置项（`.env`）

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `TG_BOT_TOKEN` | — | 必填，BotFather 给的 Token |
| `ADMIN_USER_IDS` | 空 | 管理员用户 ID，多个用英文逗号分隔 |
| `ALLOWED_USER_IDS` | 空 | 白名单；为空表示所有人都能使用 |
| `POLL_INTERVAL_SECONDS` | 60 | RSS 检查间隔（秒），最小 10 |
| `MAX_TARGETS_PER_USER` | 10 | 每位用户的推送目标上限 |
| `FETCH_TIMEOUT_SECONDS` | 20 | 单个源的读取超时（秒），超时会记录为错误 |
| `FETCH_CONCURRENCY` | 4 | 同时读取的源数量 |
| `DELIVERY_RETENTION_DAYS` | 180 | 推送历史保留天数；0 表示永久保留 |
| `DISPLAY_TIMEZONE` | Asia/Shanghai | `/history`、`/feedstatus` 显示时间用的时区 |

修改 `.env` 后需要执行 `sudo systemctl restart rss-tg-bot` 才会生效。

## 更新

```bash
sudo bash /opt/rss-tg-bot/scripts/update.sh
```

脚本会依次执行：

1. 备份 `.env` 和数据库；
2. `git pull` 拉取最新代码；
3. 安装依赖；
4. 用临时数据库运行自检，失败时不重启，旧版本继续运行；
5. 更新 systemd 服务文件并重启，然后确认服务已正常运行。

数据库结构会在启动时自动升级，已有的订阅、关键词和历史都会保留。

## 日常维护

```bash
sudo systemctl status rss-tg-bot
```

```bash
sudo journalctl -u rss-tg-bot -f
```

```bash
sudo bash /opt/rss-tg-bot/scripts/backup.sh
```

备份默认保存在 `/root/rss-tg-bot-backups`，只有 root 可读（备份里有 Token），超过 30 天的自动删除。恢复备份时，先停止服务，再解压覆盖：

```bash
sudo systemctl stop rss-tg-bot
```

```bash
sudo tar -xzf /root/rss-tg-bot-backups/<备份文件名>.tar.gz -C /opt/rss-tg-bot
```

```bash
sudo systemctl start rss-tg-bot
```

## 开发与测试

```bash
python -m unittest discover -s tests -v
```

测试使用临时数据库和本地 HTTP 服务器，不需要 Token，也不需要联网。

## 完整卸载

可以先备份，再运行：

```bash
sudo bash /opt/rss-tg-bot/scripts/uninstall.sh
```

脚本要求输入 `DELETE` 确认，随后停止并删除服务、项目目录、Token 配置和 SQLite 数据库。它不会删除系统自带的 Python 和 `python3-venv`。

## Telegram 命令速查

在私聊中依次发送：`/start` → `/feeds` → `/subscribe 1`。然后二选一：

- 全量推送：`/all 1 on`
- 关键词推送：`/keywords OpenAI,Claude`

最后发送 `/status` 检查。在 Telegram 里发送 `/help` 可以查看使用说明，发送 `/help 关键词`、`/help 版块`、`/help 目标` 等可以查看分类说明。
