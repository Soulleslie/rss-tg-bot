# RSS Telegram Bot

多用户 RSS 关键词提醒机器人。所有可删除的运行内容都集中在 `/opt/rss-tg-bot`；不开放入站端口，Telegram 通过 long polling 连接。

完整的中文命令说明、示例与排错指南见 [使用手册](docs/USAGE.zh-CN.md)。

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

## Telegram 命令

1. **订阅源**：`/feeds`、`/subscribe <源ID>`、`/unsubscribe <源ID>`、`/mysources`
2. **推送方式**：`/all` 查看模式；`/all <源ID> <on|off>` 设置全量或关键词推送。
3. **关键词与过滤**：`/keywords [词1,词2]`、`/combo <词1,词2>`、`/on <ID>`、`/off <ID>`、`/delkw <ID>`、`/block <词1,词2>`、`/blocks`、`/delblock <ID>`、`/sections <源ID> [版块ID1,版块ID2]`、`/clearsections <源ID>`。
4. **推送目标**：`/addtarget [chat_id]`、`/targets`、`/deltarget <ID>`。
5. **查询与控制**：`/status`、`/history [数量]`、`/pause`、`/resume`、`/whoami`。
6. **管理员**：`/addfeed <RSS地址>`、`/delfeed <源ID>`、`/feedstatus`。
