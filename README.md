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

运行要求：Debian / Ubuntu（使用 systemd），Python 3.10 及以上，例如 Ubuntu 22.04+、Debian 12+。

## 一键部署

用 root 登录 VPS，运行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Soulleslie/rss-tg-bot/main/scripts/install.sh)
```

脚本会依次完成：安装依赖 → 下载代码到 `/opt/rss-tg-bot` → **提示你粘贴 Bot Token 并向 Telegram 验证** → 可选填写管理员用户ID，并选择是否只允许管理员使用 → 安装运行环境并自检 → 启动机器人、设置开机自启。

- 还没有 Token：在 Telegram 里找 @BotFather，发送 `/newbot` 创建机器人。
- 不知道自己的用户ID：安装时直接回车跳过。装好后私聊机器人发送 `/whoami` 查看，再运行 `rss-tg-bot admin 你的用户ID`。
- 安装完成后，按提示打开机器人发送 `/start` 即可开始使用。
- 在已经装好的机器上再次运行这条命令，会自动改为执行更新，不会覆盖 `.env` 和数据库。

不需要交互的场景（比如开机脚本），可以用环境变量传入：

```bash
curl -fsSL https://raw.githubusercontent.com/Soulleslie/rss-tg-bot/main/scripts/install.sh | TG_BOT_TOKEN=你的Token ADMIN_USER_IDS=你的用户ID bash
```

## 管理命令

安装后可以在任何目录使用 `rss-tg-bot` 命令（需要 root）：

| 命令 | 作用 |
| --- | --- |
| `rss-tg-bot update` | **一键更新**：备份 → 拉取最新代码 → 安装依赖 → 自检 → 重启。自检失败时不重启，旧版本继续运行 |
| `rss-tg-bot uninstall` | **一键卸载**：先自动备份，输入 `DELETE` 确认后删除服务、程序目录和管理命令。备份保留在 `/root/rss-tg-bot-backups` |
| `rss-tg-bot status` | 查看运行状态和当前版本 |
| `rss-tg-bot logs` | 查看实时日志，按 Ctrl+C 退出 |
| `rss-tg-bot restart` | 重启机器人 |
| `rss-tg-bot backup` | 备份 `.env` 和数据库 |
| `rss-tg-bot config` | 编辑 `.env`，保存退出后自动重启 |
| `rss-tg-bot admin <用户ID>` | 添加管理员 |
| `rss-tg-bot allow <用户ID>` | 加入白名单。第一次使用会开启白名单，之后只有名单里的人能用；现有管理员会被自动加入 |

不带参数运行 `rss-tg-bot`，可以查看全部命令。

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

修改 `.env` 推荐用 `rss-tg-bot config`，保存后会自动重启。如果直接编辑文件，改完要运行 `rss-tg-bot restart` 才会生效。

## 更新

```bash
rss-tg-bot update
```

更新时会自动备份；数据库结构在启动时自动升级，已有的订阅、关键词和历史都会保留。如果服务器上的代码文件被改过，更新会停下来，并列出被改动的文件，不会直接覆盖。

> 从旧版本升级：旧版本还没有 `rss-tg-bot` 命令，需要先运行两次 `sudo bash /opt/rss-tg-bot/scripts/update.sh`。第一次拉取新代码，第二次安装管理命令。之后就可以用 `rss-tg-bot update` 了。

## 备份与恢复

```bash
rss-tg-bot backup
```

备份默认保存在 `/root/rss-tg-bot-backups`，只有 root 可读（备份里有 Token），超过 30 天的自动删除。恢复备份时，先停止服务，再解压覆盖：

```bash
systemctl stop rss-tg-bot
```

```bash
tar -xzf /root/rss-tg-bot-backups/<备份文件名>.tar.gz -C /opt/rss-tg-bot
```

```bash
systemctl start rss-tg-bot
```

## 开发与测试

```bash
python -m unittest discover -s tests -v
```

测试使用临时数据库和本地 HTTP 服务器，不需要 Token，也不需要联网。

## 卸载

```bash
rss-tg-bot uninstall
```

卸载前会自动备份 `.env` 和数据库到 `/root/rss-tg-bot-backups`，然后要求输入 `DELETE` 确认，再删除服务、`/opt/rss-tg-bot` 和 `rss-tg-bot` 命令。系统自带的 Python、git 等软件包不会删除。

## Telegram 命令速查

在私聊中依次发送：`/start` → `/feeds` → `/subscribe 1`。然后二选一：

- 全量推送：`/all 1 on`
- 关键词推送：`/keywords OpenAI,Claude`

最后发送 `/status` 检查。在 Telegram 里发送 `/help` 可以查看使用说明，点下方按钮可以切换到各个分类的详细说明。
