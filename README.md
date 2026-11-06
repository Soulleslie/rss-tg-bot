# RSS Telegram Bot

多用户 RSS 关键词提醒机器人。所有可删除的运行内容都集中在 `/opt/rss-tg-bot`；不开放入站端口，Telegram 通过 long polling 连接。

完整的中文命令说明、每条命令的可复制示例、版块选择方法与排错指南见 [完整使用手册](docs/USAGE.zh-CN.md)。

## 功能

- 普通关键词、组合关键词、独立开关与屏蔽词
- RSS 分类/版块多选
- 每位用户最多 10 个私聊、群组或频道推送目标
- 每用户独立规则；共享 RSS 源
- SQLite 去重和推送历史，重启后保留
- 默认 10 秒轮询，可通过 `.env` 更改
- 每个 RSS 源可在“全量推送”和“关键词推送”之间独立切换
- 首次抓取只建立基线，不推送历史内容

## VPS 安装

```bash
git clone <YOUR_GITHUB_REPO_URL> /opt/rss-tg-bot
cd /opt/rss-tg-bot
sudo bash scripts/install.sh
sudo nano /opt/rss-tg-bot/.env
```

填写 `TG_BOT_TOKEN` 后再次运行：

```bash
sudo bash scripts/install.sh
```

随后私聊 Bot，发送 `/start`、`/whoami`。将返回的 ID 填入 `.env` 的 `ADMIN_USER_IDS`，并运行 `sudo systemctl restart rss-tg-bot`。推荐同时将该 ID 写进 `ALLOWED_USER_IDS`。

## 日常维护

```bash
sudo systemctl status rss-tg-bot
sudo journalctl -u rss-tg-bot -f
sudo bash /opt/rss-tg-bot/scripts/update.sh
sudo bash /opt/rss-tg-bot/scripts/backup.sh
```

## 完整卸载

先可选备份，再运行：

```bash
sudo bash /opt/rss-tg-bot/scripts/uninstall.sh
```

脚本要求输入 `DELETE`，随后停止并删除服务、项目、Token 配置和 SQLite 数据库；它不会删除系统 Python 或 `python3-venv`。

## Telegram 命令速查

先在私聊中执行：`/start` → `/feeds` → `/subscribe 1`。然后二选一：

- 全量推送：`/all 1 on`
- 关键词推送：`/keywords OpenAI,AI`（如曾开启全量，执行 `/all 1 off`）

在 Telegram 内发送 `/help` 可看到同样的快速说明和示例。完整的每条命令参数、ID 的区别、群组/频道绑定、版块选择与排错，请看 [完整使用手册](docs/USAGE.zh-CN.md)。
