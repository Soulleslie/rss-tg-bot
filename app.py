"""Multi-user RSS keyword notifier for Telegram."""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
from dotenv import load_dotenv

# Opt in to python-telegram-bot's future behaviour (durations as timedelta) so
# reading RetryAfter.retry_after does not emit deprecation warnings.
os.environ.setdefault("PTB_TIMEDELTA", "1")

from telegram import (  # noqa: E402
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    LinkPreviewOptions,
    MessageOriginChannel,
    Update,
)
from telegram.constants import ParseMode  # noqa: E402
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every Telegram API request URL at INFO level, and that URL contains
# the bot token.  Keep those libraries quiet so the token never reaches journald.
for noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)
log = logging.getLogger("rss_tg_bot")


def env_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError:
        raise SystemExit(f".env 中的 {name} 必须是整数，当前值：{raw!r}") from None


def parse_ids(name: str) -> set[int]:
    return {int(value.strip()) for value in os.getenv(name, "").split(",") if value.strip().lstrip("-").isdigit()}


def load_timezone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown DISPLAY_TIMEZONE %r, falling back to UTC", name)
        return timezone.utc


TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
POLL_INTERVAL = env_int("POLL_INTERVAL_SECONDS", 60, 10)
MAX_TARGETS = env_int("MAX_TARGETS_PER_USER", 10, 1)
FETCH_TIMEOUT = env_int("FETCH_TIMEOUT_SECONDS", 20, 5)
FETCH_CONCURRENCY = env_int("FETCH_CONCURRENCY", 4, 1)
DELIVERY_RETENTION_DAYS = env_int("DELIVERY_RETENTION_DAYS", 180, 0)
DISPLAY_TZ_NAME = os.getenv("DISPLAY_TIMEZONE", "").strip() or "Asia/Shanghai"
DISPLAY_TZ = load_timezone(DISPLAY_TZ_NAME)
DB_PATH = Path(os.getenv("DB_PATH", "").strip() or ROOT / "rss_tg_bot.db")
ALLOWED_USER_IDS = parse_ids("ALLOWED_USER_IDS")
ADMIN_USER_IDS = parse_ids("ADMIN_USER_IDS")

# feedparser downloads with urllib, which has no timeout by default.  Without
# this, a single unresponsive feed would block every later poll forever.
socket.setdefaulttimeout(FETCH_TIMEOUT)

# Telegram's limit is 4096 characters; leave room for UTF-16 surrogate pairs.
MESSAGE_LIMIT = 3500
SEPARATORS = re.compile(r"[,，、]")
ANONYMOUS_ADMIN_ID = 1087968824  # Telegram's "GroupAnonymousBot"

# RSS feeds commonly expose their category identifiers in English.  Keep the
# original tag visible while making the most common labels easier to read.
SECTION_LABELS = {
    "carpool": "拼车",
    "daily": "日常",
    "info": "情报",
    "promotion": "推广",
    "review": "评测",
    "tech": "技术",
    "trade": "交易",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]*>", " ", value or "")
    value = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def comma_values(value: str) -> list[str]:
    """Split on English commas, Chinese commas and 、, dropping blanks."""
    return [part.strip() for part in SEPARATORS.split(value or "") if part.strip()]


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result


def is_short_ascii(word: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{1,3}", word))


def display_section_name(name: str) -> str:
    translated = SECTION_LABELS.get(name.casefold())
    return f"{translated}（{name}）" if translated else name


def display_rule(kind: str, value: str) -> str:
    return " + ".join(comma_values(value)) if kind == "combo" else value


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks, preferring line boundaries."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()] or [text[:limit] or "（空）"]


class Store:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              user_id INTEGER PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feeds (
              id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
              enabled INTEGER NOT NULL DEFAULT 1, initialized INTEGER NOT NULL DEFAULT 0,
              last_checked TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
              user_id INTEGER NOT NULL, feed_id INTEGER NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(user_id, feed_id)
            );
            CREATE TABLE IF NOT EXISTS full_pushes (
              user_id INTEGER NOT NULL, feed_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL, PRIMARY KEY(user_id, feed_id)
            );
            CREATE TABLE IF NOT EXISTS sections (
              id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER NOT NULL, name TEXT NOT NULL,
              UNIQUE(feed_id, name)
            );
            CREATE TABLE IF NOT EXISTS selected_sections (
              user_id INTEGER NOT NULL, feed_id INTEGER NOT NULL, section_id INTEGER NOT NULL,
              PRIMARY KEY(user_id, feed_id, section_id)
            );
            CREATE TABLE IF NOT EXISTS rules (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
              value TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocked_words (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, value TEXT NOT NULL,
              created_at TEXT NOT NULL, UNIQUE(user_id, value)
            );
            CREATE TABLE IF NOT EXISTS targets (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id TEXT NOT NULL,
              title TEXT, created_at TEXT NOT NULL, UNIQUE(user_id, chat_id)
            );
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER NOT NULL, item_key TEXT NOT NULL,
              title TEXT NOT NULL, link TEXT, summary TEXT, categories TEXT, first_seen TEXT NOT NULL,
              UNIQUE(feed_id, item_key)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
              target_id INTEGER NOT NULL, matched TEXT NOT NULL, sent_at TEXT NOT NULL,
              status TEXT NOT NULL, error TEXT, UNIQUE(item_id, target_id)
            );
            """
        )
        self._migrate()
        self.connection.commit()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        additions = {
            "feeds": {"etag": "TEXT", "modified": "TEXT"},
            # pending=1: a new article that has not finished delivery yet.  It
            # survives restarts, so a crash between "seen" and "sent" no longer
            # silently drops the notification.
            "items": {"pending": "INTEGER NOT NULL DEFAULT 0"},
        }
        for table, columns in additions.items():
            existing = self._columns(table)
            for column, ddl in columns.items():
                if column not in existing:
                    self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_subscriptions_feed ON subscriptions(feed_id);
            CREATE INDEX IF NOT EXISTS idx_rules_user ON rules(user_id);
            CREATE INDEX IF NOT EXISTS idx_deliveries_user ON deliveries(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_deliveries_sent_at ON deliveries(sent_at);
            CREATE INDEX IF NOT EXISTS idx_items_pending ON items(pending) WHERE pending=1;
            """
        )
        # Summaries are only needed until an item has been delivered.
        self.connection.execute("UPDATE items SET summary=NULL WHERE pending=0 AND summary IS NOT NULL")

    def execute(self, sql: str, params: tuple = ()):
        return self.connection.execute(sql, params)

    def commit(self) -> None:
        self.connection.commit()

    def ensure_user(self, user_id: int) -> None:
        self.execute("INSERT OR IGNORE INTO users(user_id, created_at) VALUES(?, ?)", (user_id, now()))
        self.commit()


store = Store(DB_PATH)


# ---------------------------------------------------------------------------
# Help texts
# ---------------------------------------------------------------------------

HELP_OVERVIEW = """📖 RSS 提醒机器人 · 使用说明

【一篇新文章怎么决定推不推】
① 你订阅了它所在的 RSS 源
② 版块筛选：没选版块 = 全部版块都要
③ 屏蔽词：标题或摘要包含任一屏蔽词 → 不推
④ 推送模式（每个源单独设置）：
   · 全量推送：直接推
   · 关键词推送（默认）：包含你的关键词才推
⑤ 推送到你的全部推送目标
发送 /pause 可暂停全部推送。

【最快上手】
/feeds — 查看源，记下前面的数字（源ID）
/subscribe 1 — 订阅源 1
然后二选一：
/all 1 on — 源 1 的新文章全部推送
/keywords OpenAI,Claude — 只推包含这些词的文章
最后用 /status 检查设置。

⚠️ 订阅后默认是“关键词推送”。没有关键词又没开全量推送，就收不到任何消息。

【分类说明】直接发送：
/help 订阅 — 订阅与推送模式
/help 关键词 — 关键词与屏蔽词
/help 版块 — 只看某些版块
/help 目标 — 推送到群组/频道
/help 状态 — 状态、历史、暂停
/help 管理 — 管理员命令

写法：<…> 要换成实际内容，不要输入尖括号；多个值用逗号分隔（, ， 、 都可以）。"""

HELP_TOPICS = {
    "订阅": """📖 订阅与推送模式

/feeds — 列出所有 RSS 源；行首数字是“源ID”
/subscribe <源ID> — 订阅，例：/subscribe 1
/mysources — 我的订阅及各自的推送模式
/unsubscribe <源ID> — 取消订阅（同时清除该源的推送模式和版块选择）

推送模式（每个源单独设置）：
/all — 查看所有已订阅源的模式
/all <源ID> on — 全量推送：该源所有新文章都推送
/all <源ID> off — 关键词推送：只推送命中关键词的文章

说明：
· 新订阅的源默认是“关键词推送”
· 全量推送仍然受“版块筛选”和“屏蔽词”限制
· 只推送订阅之后出现的新文章，不补发旧文章""",
    "关键词": """📖 关键词与屏蔽词

关键词和屏蔽词对你订阅的所有源统一生效，不能按源分别设置。
匹配范围：文章标题 + 摘要；不区分英文大小写；只要“包含”就算命中。
⚠️ 很短的英文词容易误命中：AI 会命中 said、mail、rain。尽量用更完整的词。

普通关键词（包含任意一个就推送）：
/keywords — 查看全部关键词及其 ID
/keywords <词1,词2> — 添加，例：/keywords OpenAI,Claude
  每个词单独成为一条关键词，各有自己的 ID

组合关键词（必须同时包含所有词）：
/combo <词1,词2> — 例：/combo dmit,corona
  整组算一条关键词，只有一个 ID

管理关键词（这里的 ID 是“关键词ID”，不是源ID）：
/kwoff <关键词ID> — 暂停，不删除
/kwon <关键词ID> — 重新启用
/delkw <关键词ID> — 永久删除
（旧命令 /off、/on 仍可用，效果相同）

屏蔽词（包含任意一个就不推送，全量推送也会被拦截）：
/block <词1,词2> — 例：/block 求购,已出
/blocks — 查看屏蔽词及其 ID
/delblock <屏蔽词ID> — 删除""",
    "版块": """📖 版块筛选（每个源单独设置）

没选任何版块 = 该源全部版块都推送。

第 1 步：查看源的版块及其 ID
/sections <源ID> — 例：/sections 1
第 2 步：只保留想要的版块
/sections <源ID> <版块ID1,版块ID2> — 例：/sections 1 1,142
  ⚠️ 每次设置都会“替换”之前的选择，不是追加
恢复全部版块：
/clearsections <源ID>

说明：
· 版块来自 RSS 文章自带的分类，是从已经抓到的文章里收集的；某个版块近期没人发帖，列表里就可能暂时没有
· 不同源的版块ID互不通用
· 一篇文章属于多个版块时，只要有一个被选中就会推送""",
    "目标": """📖 推送目标（推送到哪里）

你的所有推送目标都会收到同样的推送，不能按源分别指定。最多 {max_targets} 个。

私聊：发送 /start 自动添加
群组：先把机器人拉进群，由群管理员在群里发送 /addtarget
频道：
 1. 把机器人添加为频道管理员（需要“发布消息”权限）
 2. 获取频道 chat_id：从频道转发任意一条消息给机器人（私聊），机器人会回复它的 chat_id；公开频道也可以直接用 @频道用户名
 3. 在私聊中发送 /addtarget <chat_id>，例：/addtarget -1001234567890 或 /addtarget @mychannel
 你本人也必须是该频道的管理员。

/targets — 查看推送目标及其“目标ID”
/deltarget <目标ID> — 删除，例：/deltarget 2""",
    "状态": """📖 状态、历史与暂停

/status — 查看完整设置，并提示可能导致收不到推送的问题
/history [数量] — 最近推送记录，默认 10 条，最多 50 条，例：/history 30
/pause — 暂停你的全部推送（设置保留）
/resume — 恢复推送
/whoami — 显示你的用户 ID 和当前聊天的 chat_id

收不到推送时，先发 /status，按提示检查。""",
    "管理": """📖 管理员命令（需要在服务器 .env 的 ADMIN_USER_IDS 中配置）

/addfeed <RSS地址> — 添加共享源，例：/addfeed https://example.com/feed.xml
/delfeed <源ID> — 删除源及所有人对它的订阅和设置（推送历史保留）
/feedstatus — 各源上次检查时间、错误和订阅人数

新源第一次抓取只建立“已读基线”，不会推送历史文章。""",
}

HELP_ALIASES = {
    "1": "订阅", "sub": "订阅", "subscribe": "订阅", "源": "订阅",
    "2": "关键词", "kw": "关键词", "keywords": "关键词", "屏蔽": "关键词", "屏蔽词": "关键词",
    "3": "版块", "sections": "版块", "板块": "版块",
    "4": "目标", "target": "目标", "targets": "目标", "频道": "目标", "群组": "目标",
    "5": "状态", "status": "状态", "历史": "状态",
    "6": "管理", "admin": "管理", "管理员": "管理",
}

USER_COMMANDS = [
    ("start", "开始使用，并把私聊设为推送目标"),
    ("help", "使用说明"),
    ("status", "查看我的设置与问题提示"),
    ("feeds", "查看可订阅的 RSS 源"),
    ("subscribe", "订阅源：/subscribe 源ID"),
    ("mysources", "我的订阅与推送模式"),
    ("unsubscribe", "取消订阅：/unsubscribe 源ID"),
    ("all", "推送模式：/all 源ID on 或 off"),
    ("keywords", "查看或添加关键词：/keywords 词1,词2"),
    ("combo", "添加组合关键词：/combo 词1,词2"),
    ("kwoff", "暂停关键词：/kwoff 关键词ID"),
    ("kwon", "启用关键词：/kwon 关键词ID"),
    ("delkw", "删除关键词：/delkw 关键词ID"),
    ("block", "添加屏蔽词：/block 词1,词2"),
    ("blocks", "查看屏蔽词"),
    ("delblock", "删除屏蔽词：/delblock 屏蔽词ID"),
    ("sections", "版块筛选：/sections 源ID [版块ID]"),
    ("clearsections", "恢复全部版块：/clearsections 源ID"),
    ("addtarget", "添加推送目标（群组/频道）"),
    ("targets", "查看推送目标"),
    ("deltarget", "删除推送目标：/deltarget 目标ID"),
    ("history", "最近推送记录：/history [数量]"),
    ("pause", "暂停全部推送"),
    ("resume", "恢复推送"),
    ("whoami", "查看我的用户 ID 和当前 chat_id"),
]
ADMIN_COMMANDS = [
    ("addfeed", "管理员：添加 RSS 源"),
    ("delfeed", "管理员：删除 RSS 源"),
    ("feedstatus", "管理员：RSS 源健康状态"),
]
GROUP_COMMANDS = [
    ("addtarget", "群管理员：把本群设为推送目标"),
    ("whoami", "查看我的用户 ID 和本群 chat_id"),
    ("help", "使用说明"),
]


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        # Stay silent in groups so outsiders cannot make the bot spam the chat.
        if is_private(update) and update.effective_message:
            await update.effective_message.reply_text(
                "你不在机器人允许名单内。请把 /whoami 显示的用户 ID 发给管理员。" if user else "你不在机器人允许名单内。"
            )
        return False
    store.ensure_user(user.id)
    return True


async def respond(update: Update, message: str) -> None:
    for chunk in split_message(message):
        await update.effective_message.reply_text(chunk, link_preview_options=LinkPreviewOptions(is_disabled=True))


def command_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args or []).strip()


def int_arg(context: ContextTypes.DEFAULT_TYPE, index: int = 0) -> int | None:
    args = context.args or []
    if len(args) > index and args[index].isdigit():
        return int(args[index])
    return None


def get_feed(feed_id: int):
    return store.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()


def is_subscribed(user_id: int, feed_id: int) -> bool:
    return bool(store.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND feed_id=?", (user_id, feed_id)).fetchone())


def active_rule_count(user_id: int) -> int:
    return store.execute("SELECT COUNT(*) c FROM rules WHERE user_id=? AND active=1", (user_id,)).fetchone()["c"]


def selected_section_names(user_id: int, feed_id: int) -> list[str]:
    rows = store.execute(
        "SELECT sec.name FROM selected_sections ss JOIN sections sec ON sec.id=ss.section_id "
        "WHERE ss.user_id=? AND ss.feed_id=? ORDER BY sec.id",
        (user_id, feed_id),
    ).fetchall()
    return [row["name"] for row in rows]


def subscription_rows(user_id: int):
    return store.execute(
        "SELECT f.id, f.title, f.enabled, COALESCE(p.active, 0) full_push FROM subscriptions s "
        "JOIN feeds f ON f.id=s.feed_id "
        "LEFT JOIN full_pushes p ON p.user_id=s.user_id AND p.feed_id=s.feed_id "
        "WHERE s.user_id=? ORDER BY f.id",
        (user_id,),
    ).fetchall()


def describe_subscription(user_id: int, row) -> str:
    mode = "全量推送" if row["full_push"] else "关键词推送"
    names = selected_section_names(user_id, row["id"])
    sections_text = "全部版块" if not names else "只看：" + "、".join(display_section_name(n) for n in names)
    disabled = "（源已停用）" if not row["enabled"] else ""
    return f"#{row['id']} {row['title']}{disabled} — {mode} · {sections_text}"


USAGE_HINT = "\n发送 /help 查看完整说明。"


# ---------------------------------------------------------------------------
# Commands: basics
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not is_private(update):
        await respond(
            update,
            "请在与机器人的私聊中发送 /start 完成设置。\n如需把本群设为推送目标，群管理员可在本群发送 /addtarget。",
        )
        return
    user = update.effective_user
    status, _ = add_target(user.id, update.effective_chat.id, user.full_name)
    target_line = {
        "added": "✅ 当前私聊已设为推送目标。",
        "exists": "✅ 当前私聊已是推送目标。",
        "full": f"⚠️ 推送目标已达上限（{MAX_TARGETS} 个），当前私聊未添加。可用 /targets 查看、/deltarget 删除。",
    }[status]
    await respond(
        update,
        f"""RSS 提醒机器人已就绪。
{target_line}

开始使用：
1. /feeds — 查看 RSS 源，记下前面的数字（源ID）
2. /subscribe 1 — 订阅源 1
3. 二选一：
   /all 1 on — 源 1 的新文章全部推送
   /keywords OpenAI,Claude — 只推送包含这些词的文章
4. /status — 检查设置

⚠️ 订阅后默认是“关键词推送”，没有关键词就收不到消息。
发送 /help 查看完整说明。""",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    topic = command_text(context).casefold()
    topic = HELP_ALIASES.get(topic, topic)
    text = HELP_TOPICS.get(topic)
    if text:
        await respond(update, text.format(max_targets=MAX_TARGETS))
    else:
        await respond(update, HELP_OVERVIEW)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    lines = [f"你的 Telegram 用户 ID：{update.effective_user.id}"]
    chat = update.effective_chat
    if chat and chat.type != "private":
        lines.append(f"当前聊天（{chat.title or '群组'}）的 chat_id：{chat.id}")
        lines.append("群管理员可在本群直接发送 /addtarget 把本群设为推送目标。")
    else:
        lines.append("需要频道 chat_id？从频道转发一条消息给我即可。")
    await respond(update, "\n".join(lines))


# ---------------------------------------------------------------------------
# Commands: feeds and subscriptions
# ---------------------------------------------------------------------------

async def feeds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    rows = store.execute(
        "SELECT f.id, f.title, f.enabled, s.user_id IS NOT NULL subscribed FROM feeds f "
        "LEFT JOIN subscriptions s ON s.feed_id=f.id AND s.user_id=? ORDER BY f.id",
        (user_id,),
    ).fetchall()
    if not rows:
        await respond(update, "目前还没有 RSS 源，请联系管理员用 /addfeed 添加。")
        return
    lines = []
    for row in rows:
        state = "（已停用）" if not row["enabled"] else ("  ✅ 已订阅" if row["subscribed"] else "")
        lines.append(f"{row['id']}. {row['title']}{state}")
    await respond(update, "可订阅的 RSS 源（行首数字 = 源ID）：\n" + "\n".join(lines) + "\n\n订阅：/subscribe <源ID>，例：/subscribe 1")


class FeedError(Exception):
    pass


def fetch_feed(url: str, etag: str | None = None, modified: str | None = None):
    """Download and parse a feed.  Runs in a worker thread."""
    parsed = feedparser.parse(url, etag=etag or None, modified=modified or None)
    status = parsed.get("status")
    if status == 304:
        return parsed
    if status and status >= 400:
        raise FeedError(f"HTTP {status}")
    if not parsed.entries and parsed.get("bozo"):
        error = parsed.get("bozo_exception")
        raise FeedError(f"读取或解析失败：{type(error).__name__}: {error}")
    return parsed


async def addfeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not is_admin(update.effective_user.id):
        await respond(update, "仅管理员可添加 RSS 源。管理员需要先用 /whoami 获取用户 ID，填入服务器 .env 的 ADMIN_USER_IDS 并重启服务。")
        return
    url = command_text(context)
    if not url.startswith(("https://", "http://")):
        await respond(update, "用法：/addfeed <RSS地址>\n例：/addfeed https://example.com/feed.xml")
        return
    if store.execute("SELECT 1 FROM feeds WHERE url=?", (url,)).fetchone():
        await respond(update, "这个 RSS 源已存在，发送 /feeds 查看。")
        return
    try:
        parsed = await asyncio.to_thread(fetch_feed, url)
    except Exception as error:
        await respond(update, f"无法读取这个 RSS：{str(error)[:300]}")
        return
    if not parsed.entries:
        await respond(update, "能访问这个地址，但没有读到任何文章。请确认它是 RSS/Atom 地址。")
        return
    title = clean_text(parsed.feed.get("title", "")) or urlparse(url).netloc
    try:
        cursor = store.execute("INSERT INTO feeds(title, url) VALUES(?, ?)", (title, url))
        store.commit()
    except sqlite3.IntegrityError:
        await respond(update, "这个 RSS 源已存在，发送 /feeds 查看。")
        return
    feed_id = cursor.lastrowid
    await respond(
        update,
        f"✅ 已添加源 #{feed_id}：{title}\n用户可发送 /subscribe {feed_id} 订阅。\n首次抓取只建立“已读基线”，不会推送历史文章。",
    )


async def delfeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not is_admin(update.effective_user.id):
        await respond(update, "仅管理员可删除 RSS 源。")
        return
    feed_id = int_arg(context)
    if feed_id is None or len(context.args) != 1:
        await respond(update, "用法：/delfeed <源ID>\n先用 /feeds 查看源ID。")
        return
    feed = get_feed(feed_id)
    if not feed:
        await respond(update, f"没有 ID 为 {feed_id} 的源，发送 /feeds 查看。")
        return
    for sql in (
        "DELETE FROM subscriptions WHERE feed_id=?",
        "DELETE FROM full_pushes WHERE feed_id=?",
        "DELETE FROM selected_sections WHERE feed_id=?",
        "DELETE FROM sections WHERE feed_id=?",
        "UPDATE items SET pending=0, summary=NULL WHERE feed_id=?",
        "DELETE FROM feeds WHERE id=?",
    ):
        store.execute(sql, (feed_id,))
    store.commit()
    await respond(update, f"已删除源 #{feed_id} {feed['title']}，以及所有人对它的订阅、推送模式和版块选择。推送历史保留。")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    feed_id = int_arg(context)
    if feed_id is None:
        await respond(update, "用法：/subscribe <源ID>，例：/subscribe 1\n先用 /feeds 查看源ID。")
        return
    feed = get_feed(feed_id)
    if not feed or not feed["enabled"]:
        await respond(update, f"没有可订阅的源 #{feed_id}，发送 /feeds 查看。")
        return
    if is_subscribed(user_id, feed_id):
        await respond(update, f"你已经订阅了 #{feed_id} {feed['title']}。用 /mysources 查看推送模式。")
        return
    store.execute("INSERT INTO subscriptions(user_id, feed_id, created_at) VALUES(?, ?, ?)", (user_id, feed_id, now()))
    store.commit()
    keyword_count = active_rule_count(user_id)
    keyword_line = (
        f"你已有 {keyword_count} 条启用的关键词，会立即对这个源生效。"
        if keyword_count
        else "⚠️ 你还没有关键词，现在这个源不会推送任何文章。"
    )
    await respond(
        update,
        f"""✅ 已订阅 #{feed_id} {feed['title']}
当前推送模式：关键词推送（默认）
{keyword_line}

下一步二选一：
· /all {feed_id} on — 这个源的新文章全部推送
· /keywords 词1,词2 — 添加关键词（对你所有“关键词推送”的源生效）

只会推送订阅之后出现的新文章。""",
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    feed_id = int_arg(context)
    if feed_id is None:
        await respond(update, "用法：/unsubscribe <源ID>\n先用 /mysources 查看你订阅的源ID。")
        return
    if not is_subscribed(user_id, feed_id):
        await respond(update, f"你没有订阅源 #{feed_id}。发送 /mysources 查看你的订阅。")
        return
    feed = get_feed(feed_id)
    for table in ("subscriptions", "full_pushes", "selected_sections"):
        store.execute(f"DELETE FROM {table} WHERE user_id=? AND feed_id=?", (user_id, feed_id))
    store.commit()
    await respond(update, f"已取消订阅 #{feed_id} {feed['title'] if feed else ''}，并清除了该源的推送模式和版块选择。")


async def my_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    rows = subscription_rows(user_id)
    if not rows:
        await respond(update, "你还没有订阅任何源。先用 /feeds 查看，再用 /subscribe <源ID> 订阅。")
        return
    body = "\n".join(describe_subscription(user_id, row) for row in rows)
    await respond(update, f"我的订阅：\n{body}\n\n切换模式：/all <源ID> on 或 off\n版块筛选：/sections <源ID>")


async def all_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    args = context.args or []
    if not args:
        rows = subscription_rows(user_id)
        if not rows:
            await respond(update, "你还没有订阅任何源。先用 /feeds 和 /subscribe <源ID>。")
            return
        body = "\n".join(f"#{r['id']} {r['title']} — {'✅ 全量推送' if r['full_push'] else '🔍 关键词推送'}" for r in rows)
        await respond(update, f"推送模式：\n{body}\n\n切换：/all <源ID> on（全量）或 /all <源ID> off（关键词）")
        return
    feed_id = int_arg(context)
    option = args[1].lower() if len(args) == 2 else ""
    if feed_id is None or option not in {"on", "off"}:
        await respond(update, "用法：/all <源ID> on 或 /all <源ID> off\n例：/all 1 on\n不带参数发送 /all 可查看当前模式。")
        return
    if not is_subscribed(user_id, feed_id):
        await respond(update, f"你没有订阅源 #{feed_id}。先用 /mysources 查看你订阅的源ID。")
        return
    active = int(option == "on")
    store.execute(
        "INSERT INTO full_pushes(user_id,feed_id,active,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id,feed_id) DO UPDATE SET active=excluded.active,updated_at=excluded.updated_at",
        (user_id, feed_id, active, now()),
    )
    store.commit()
    title = get_feed(feed_id)["title"]
    if active:
        await respond(update, f"✅ #{feed_id} {title} 已切换为全量推送：所有新文章都会推送（版块筛选和屏蔽词仍然生效）。")
        return
    message = f"🔍 #{feed_id} {title} 已切换为关键词推送：只推送包含你关键词的文章。"
    if not active_rule_count(user_id):
        message += "\n⚠️ 你目前没有启用的关键词，这个源将收不到任何推送。用 /keywords 词1,词2 添加。"
    await respond(update, message)


# ---------------------------------------------------------------------------
# Commands: keywords and blocked words
# ---------------------------------------------------------------------------

def short_word_warning(words: list[str]) -> str:
    short = [word for word in words if is_short_ascii(word)]
    if not short:
        return ""
    return (
        f"\n⚠️ {('、'.join(short))} 很短，按“包含”匹配容易误命中（例如 AI 会命中 said、mail）。"
        "如推送过多，可用 /delkw 删除后改用更完整的词，或用 /combo 组合。"
    )


async def keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    text = command_text(context)
    if text:
        values = unique_values(comma_values(text))
        existing = {
            row["value"].casefold(): row["id"]
            for row in store.execute("SELECT id, value FROM rules WHERE user_id=? AND kind='word'", (user_id,))
        }
        added, skipped = [], []
        for value in values:
            if value.casefold() in existing:
                skipped.append(f"#{existing[value.casefold()]} {value}")
                continue
            cursor = store.execute(
                "INSERT INTO rules(user_id, kind, value, created_at) VALUES(?, 'word', ?, ?)", (user_id, value, now())
            )
            added.append(f"#{cursor.lastrowid} {value}")
        store.commit()
        lines = []
        if added:
            lines.append("✅ 已添加普通关键词（包含任意一个就推送）：\n" + "\n".join(added))
        if skipped:
            lines.append("已存在，未重复添加：" + "、".join(skipped))
        if not values:
            lines.append("没有识别到关键词。用法：/keywords 词1,词2")
        message = "\n".join(lines) + short_word_warning([v for v in values if v.casefold() not in existing])
        if added and not store.execute("SELECT 1 FROM subscriptions WHERE user_id=?", (user_id,)).fetchone():
            message += "\n提示：你还没有订阅任何源，先用 /feeds 和 /subscribe <源ID>。"
        await respond(update, message)
        return
    rows = store.execute("SELECT id, kind, value, active FROM rules WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    if not rows:
        await respond(update, "你还没有关键词。\n添加：/keywords 词1,词2（任一命中）或 /combo 词1,词2（全部命中）")
        return
    body = "\n".join(
        f"{r['id']}. {'组合' if r['kind'] == 'combo' else '普通'} · {display_rule(r['kind'], r['value'])} "
        f"{'✅ 启用' if r['active'] else '⏸ 已暂停'}"
        for r in rows
    )
    await respond(
        update,
        f"我的关键词（行首数字 = 关键词ID，对所有“关键词推送”的源生效）：\n{body}\n\n"
        "/kwoff <ID> 暂停 · /kwon <ID> 启用 · /delkw <ID> 删除",
    )


async def combo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    values = unique_values(comma_values(command_text(context)))
    if len(values) < 2:
        await respond(update, "用法：/combo 词1,词2（至少两个不同的词）\n例：/combo dmit,corona —— 同一篇文章必须同时包含这两个词。")
        return
    wanted = {value.casefold() for value in values}
    for row in store.execute("SELECT id, value FROM rules WHERE user_id=? AND kind='combo'", (user_id,)):
        if {value.casefold() for value in comma_values(row["value"])} == wanted:
            await respond(update, f"这组组合关键词已存在：#{row['id']} {display_rule('combo', row['value'])}")
            return
    cursor = store.execute(
        "INSERT INTO rules(user_id, kind, value, created_at) VALUES(?, 'combo', ?, ?)", (user_id, ",".join(values), now())
    )
    store.commit()
    await respond(update, f"✅ 已添加组合关键词 #{cursor.lastrowid}：{' + '.join(values)}\n同一篇文章必须同时包含这些词才会推送。")


async def set_rule_state(update: Update, context: ContextTypes.DEFAULT_TYPE, active: int) -> None:
    if not await guard(update):
        return
    command = "/kwon" if active else "/kwoff"
    rule_id = int_arg(context)
    if rule_id is None or len(context.args) != 1:
        await respond(
            update,
            f"用法：{command} <关键词ID>，例：{command} 3\n先用 /keywords 查看关键词ID。\n"
            "（如果你想切换某个源的推送模式，请用 /all <源ID> on 或 off）",
        )
        return
    rule = store.execute("SELECT kind, value FROM rules WHERE id=? AND user_id=?", (rule_id, update.effective_user.id)).fetchone()
    if not rule:
        await respond(update, f"没有找到你的关键词 #{rule_id}。发送 /keywords 查看关键词ID。")
        return
    store.execute("UPDATE rules SET active=? WHERE id=?", (active, rule_id))
    store.commit()
    await respond(update, f"{'✅ 已启用' if active else '⏸ 已暂停'}关键词 #{rule_id}：{display_rule(rule['kind'], rule['value'])}")


async def kwon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_rule_state(update, context, 1)


async def kwoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_rule_state(update, context, 0)


async def delkw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rule_id = int_arg(context)
    if rule_id is None:
        await respond(update, "用法：/delkw <关键词ID>，例：/delkw 3\n先用 /keywords 查看关键词ID。")
        return
    rule = store.execute("SELECT kind, value FROM rules WHERE id=? AND user_id=?", (rule_id, update.effective_user.id)).fetchone()
    if not rule:
        await respond(update, f"没有找到你的关键词 #{rule_id}。发送 /keywords 查看关键词ID。")
        return
    store.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    store.commit()
    await respond(update, f"已删除关键词 #{rule_id}：{display_rule(rule['kind'], rule['value'])}")


async def block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    values = unique_values(comma_values(command_text(context)))
    if not values:
        await respond(update, "用法：/block 词1,词2，例：/block 求购,已出\n标题或摘要包含任一屏蔽词的文章不会推送。")
        return
    existing = {
        row["value"].casefold(): row["id"] for row in store.execute("SELECT id, value FROM blocked_words WHERE user_id=?", (user_id,))
    }
    added, skipped = [], []
    for value in values:
        if value.casefold() in existing:
            skipped.append(f"#{existing[value.casefold()]} {value}")
            continue
        cursor = store.execute("INSERT INTO blocked_words(user_id, value, created_at) VALUES(?, ?, ?)", (user_id, value, now()))
        added.append(f"#{cursor.lastrowid} {value}")
    store.commit()
    lines = []
    if added:
        lines.append("🚫 已添加屏蔽词（对所有源生效，全量推送也会被拦截）：\n" + "\n".join(added))
    if skipped:
        lines.append("已存在，未重复添加：" + "、".join(skipped))
    await respond(update, "\n".join(lines))


async def blocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = store.execute("SELECT id, value FROM blocked_words WHERE user_id=? ORDER BY id", (update.effective_user.id,)).fetchall()
    if not rows:
        await respond(update, "你还没有屏蔽词。添加：/block 词1,词2")
        return
    body = "\n".join(f"{r['id']}. {r['value']}" for r in rows)
    await respond(update, f"屏蔽词（行首数字 = 屏蔽词ID）：\n{body}\n\n删除：/delblock <屏蔽词ID>")


async def delblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    block_id = int_arg(context)
    if block_id is None:
        await respond(update, "用法：/delblock <屏蔽词ID>，例：/delblock 2\n先用 /blocks 查看屏蔽词ID。")
        return
    row = store.execute("SELECT value FROM blocked_words WHERE id=? AND user_id=?", (block_id, update.effective_user.id)).fetchone()
    if not row:
        await respond(update, f"没有找到你的屏蔽词 #{block_id}。发送 /blocks 查看。")
        return
    store.execute("DELETE FROM blocked_words WHERE id=?", (block_id,))
    store.commit()
    await respond(update, f"已删除屏蔽词 #{block_id}：{row['value']}")


# ---------------------------------------------------------------------------
# Commands: sections
# ---------------------------------------------------------------------------

async def sections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    feed_id = int_arg(context)
    if feed_id is None:
        await respond(update, "用法：\n/sections <源ID> — 查看版块\n/sections <源ID> <版块ID1,版块ID2> — 只看这些版块\n例：/sections 1 1,142")
        return
    feed = get_feed(feed_id)
    if not feed:
        await respond(update, f"没有 ID 为 {feed_id} 的源。发送 /mysources 查看你订阅的源ID。")
        return
    available = {
        row["id"]: row["name"] for row in store.execute("SELECT id, name FROM sections WHERE feed_id=? ORDER BY id", (feed_id,))
    }
    if len(context.args) > 1:
        if not is_subscribed(user_id, feed_id):
            await respond(update, f"请先订阅源 #{feed_id}：/subscribe {feed_id}")
            return
        requested = comma_values(" ".join(context.args[1:]))
        valid, invalid = [], []
        for value in requested:
            section_id = int(value) if value.isdigit() else None
            if section_id in available:
                if section_id not in valid:
                    valid.append(section_id)
            else:
                invalid.append(value)
        if not valid:
            await respond(
                update,
                f"没有有效的版块ID：{'、'.join(invalid) or '（空）'}。原有选择未改动。\n先发送 /sections {feed_id} 查看这个源的版块ID。",
            )
            return
        store.execute("DELETE FROM selected_sections WHERE user_id=? AND feed_id=?", (user_id, feed_id))
        for section_id in valid:
            store.execute(
                "INSERT INTO selected_sections(user_id, feed_id, section_id) VALUES(?, ?, ?)", (user_id, feed_id, section_id)
            )
        store.commit()
        names = "、".join(display_section_name(available[section_id]) for section_id in valid)
        message = f"✅ #{feed_id} {feed['title']} 现在只推送这些版块：{names}\n（已替换之前的选择；恢复全部：/clearsections {feed_id}）"
        if invalid:
            message += f"\n已忽略无效的版块ID：{'、'.join(invalid)}"
        await respond(update, message)
        return
    chosen = {
        row["section_id"]
        for row in store.execute("SELECT section_id FROM selected_sections WHERE user_id=? AND feed_id=?", (user_id, feed_id))
    }
    if not available:
        await respond(update, f"#{feed_id} {feed['title']} 暂时还没有发现版块（版块从抓到的文章分类中收集，稍后再试）。")
        return
    current = "全部版块" if not chosen else "只看已勾选（✅）的版块"
    body = "\n".join(f"{section_id}. {display_section_name(name)}{'  ✅' if section_id in chosen else ''}" for section_id, name in available.items())
    await respond(
        update,
        f"#{feed_id} {feed['title']} 的版块（行首数字 = 版块ID）\n当前：{current}\n\n{body}\n\n"
        f"只看某些版块：/sections {feed_id} 版块ID1,版块ID2（会替换原选择）\n恢复全部：/clearsections {feed_id}",
    )


async def clear_sections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    feed_id = int_arg(context)
    if feed_id is None:
        await respond(update, "用法：/clearsections <源ID>，例：/clearsections 1")
        return
    cursor = store.execute("DELETE FROM selected_sections WHERE user_id=? AND feed_id=?", (update.effective_user.id, feed_id))
    store.commit()
    if cursor.rowcount:
        await respond(update, f"✅ 源 #{feed_id} 已恢复为推送全部版块。")
    else:
        await respond(update, f"源 #{feed_id} 本来就是全部版块，无需恢复。")


# ---------------------------------------------------------------------------
# Commands: targets
# ---------------------------------------------------------------------------

def add_target(user_id: int, chat_id: int | str, title: str) -> tuple[str, str]:
    if store.execute("SELECT 1 FROM targets WHERE user_id=? AND chat_id=?", (user_id, str(chat_id))).fetchone():
        return "exists", f"「{title}」已经是你的推送目标。"
    count = store.execute("SELECT COUNT(*) count FROM targets WHERE user_id=?", (user_id,)).fetchone()["count"]
    if count >= MAX_TARGETS:
        return "full", f"每位用户最多 {MAX_TARGETS} 个推送目标，请先用 /targets 查看、/deltarget 删除不需要的。"
    store.execute(
        "INSERT INTO targets(user_id, chat_id, title, created_at) VALUES(?, ?, ?, ?)", (user_id, str(chat_id), title, now())
    )
    store.commit()
    return "added", f"✅ 已添加推送目标：{title}（{chat_id}）"


async def is_chat_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: str | int, user_id: int) -> bool:
    try:
        membership = await context.bot.get_chat_member(chat_id, user_id)
        return membership.status in {"administrator", "creator", "owner"}
    except TelegramError:
        return False


async def bot_can_post(context: ContextTypes.DEFAULT_TYPE, chat) -> bool:
    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
    except TelegramError:
        return False
    if chat.type == "channel":
        return member.status == "administrator" and bool(getattr(member, "can_post_messages", False))
    if member.status in {"left", "kicked"}:
        return False
    if member.status == "restricted":
        return bool(getattr(member, "can_send_messages", False))
    return True


async def addtarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chat = update.effective_chat
    user_id = update.effective_user.id
    args = context.args or []
    if chat.type != "private":
        if args:
            await respond(update, "在群里请直接发送 /addtarget（不带参数）。绑定频道请在私聊中操作。")
            return
        if not await is_chat_admin(context, chat.id, user_id):
            hint = "\n你正以匿名管理员身份发言，请先在群设置中关闭“匿名”再试。" if user_id == ANONYMOUS_ADMIN_ID else ""
            await respond(update, "仅群管理员可以把本群设为推送目标。" + hint)
            return
        if not await bot_can_post(context, chat):
            await respond(update, "机器人在本群没有发消息的权限，请先调整权限再试。")
            return
        _, message = add_target(user_id, chat.id, chat.title or str(chat.id))
        await respond(update, message)
        return
    if not args:
        _, message = add_target(user_id, chat.id, update.effective_user.full_name)
        await respond(
            update,
            message.replace("✅ 已添加推送目标", "✅ 已把当前私聊添加为推送目标")
            + "\n\n绑定群组：把机器人拉进群，由群管理员在群里发送 /addtarget\n"
            "绑定频道：在私聊发送 /addtarget <频道chat_id或@用户名>（发送 /help 目标 查看详细步骤）",
        )
        return
    raw = args[0]
    try:
        target_chat = await context.bot.get_chat(int(raw) if raw.lstrip("-").isdigit() else raw)
    except (TelegramError, ValueError):
        await respond(
            update,
            f"找不到 {raw}。请确认：\n1. chat_id 或 @用户名 正确（频道 chat_id 一般以 -100 开头）\n"
            "2. 机器人已经在该群/频道中（频道需设为管理员）\n获取频道 chat_id：从频道转发一条消息给我。",
        )
        return
    if target_chat.type not in {"group", "supergroup", "channel"}:
        await respond(update, "只能绑定群组或频道。私聊目标请直接发送 /start。")
        return
    if not await is_chat_admin(context, target_chat.id, user_id):
        await respond(update, "无法确认你是该群/频道的管理员。只有管理员才能绑定；如果是频道，机器人也必须是频道管理员。")
        return
    if not await bot_can_post(context, target_chat):
        await respond(update, "机器人在该群/频道没有发消息的权限。频道需要把机器人设为管理员并开启“发布消息”权限。")
        return
    _, message = add_target(user_id, target_chat.id, target_chat.title or target_chat.username or str(target_chat.id))
    await respond(update, message)


async def targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = store.execute("SELECT id, chat_id, title FROM targets WHERE user_id=? ORDER BY id", (update.effective_user.id,)).fetchall()
    if not rows:
        await respond(update, "你还没有推送目标。私聊发送 /start 即可把私聊设为推送目标。")
        return
    body = "\n".join(f"{r['id']}. {r['title'] or r['chat_id']}（{r['chat_id']}）" for r in rows)
    await respond(
        update,
        f"推送目标（行首数字 = 目标ID，{len(rows)}/{MAX_TARGETS}）：\n{body}\n\n所有目标都会收到相同的推送。删除：/deltarget <目标ID>",
    )


async def deltarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    target_id = int_arg(context)
    if target_id is None:
        await respond(update, "用法：/deltarget <目标ID>，例：/deltarget 2\n先用 /targets 查看目标ID。")
        return
    row = store.execute("SELECT chat_id, title FROM targets WHERE id=? AND user_id=?", (target_id, update.effective_user.id)).fetchone()
    if not row:
        await respond(update, f"没有找到你的推送目标 #{target_id}。发送 /targets 查看。")
        return
    store.execute("DELETE FROM targets WHERE id=?", (target_id,))
    store.commit()
    await respond(update, f"已删除推送目标 #{target_id}：{row['title'] or row['chat_id']}")


async def forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    origin = update.effective_message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        chat = origin.chat
        await respond(
            update,
            f"频道「{chat.title}」的 chat_id：{chat.id}\n\n确认机器人已是该频道管理员（有“发布消息”权限）后，发送：\n/addtarget {chat.id}",
        )
        return
    await respond(update, "这条转发消息不是来自频道，无法识别频道 chat_id。\n绑定群组：请由群管理员直接在群里发送 /addtarget。")


# ---------------------------------------------------------------------------
# Commands: status
# ---------------------------------------------------------------------------

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        store.execute("UPDATE users SET paused=1 WHERE user_id=?", (update.effective_user.id,))
        store.commit()
        await respond(update, "⏸ 已暂停你的全部推送，设置都会保留。恢复：/resume")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        store.execute("UPDATE users SET paused=0 WHERE user_id=?", (update.effective_user.id,))
        store.commit()
        await respond(update, "▶️ 已恢复推送。暂停期间出现的文章不会补发。")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    requested = int_arg(context)
    limit = max(1, min(requested, 50)) if requested is not None else 10
    rows = store.execute(
        "SELECT COALESCE(f.title, '已删除的源') feed, i.title, i.link, d.matched, d.status, d.error, d.sent_at, "
        "COALESCE(t.title, t.chat_id, '已删除的目标') target FROM deliveries d "
        "JOIN items i ON i.id=d.item_id LEFT JOIN feeds f ON f.id=i.feed_id LEFT JOIN targets t ON t.id=d.target_id "
        "WHERE d.user_id=? ORDER BY d.id DESC LIMIT ?",
        (update.effective_user.id, limit),
    ).fetchall()
    if not rows:
        await respond(update, "还没有推送记录。")
        return
    entries = []
    for r in rows:
        state = "✅ 已发送" if r["status"] == "sent" else f"❌ 发送失败（{(r['error'] or '未知错误')[:80]}）"
        entries.append(
            f"[{r['feed']}] {r['title']}\n命中：{r['matched']} · {state}\n→ {r['target']} · {format_time(r['sent_at'])}\n{r['link'] or ''}".rstrip()
        )
    await respond(update, f"最近 {len(rows)} 条推送记录（时间：{DISPLAY_TZ_NAME}）：\n\n" + "\n\n".join(entries))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    paused = store.execute("SELECT paused FROM users WHERE user_id=?", (user_id,)).fetchone()["paused"]
    subs = subscription_rows(user_id)
    rule_total = store.execute("SELECT COUNT(*) c FROM rules WHERE user_id=?", (user_id,)).fetchone()["c"]
    rule_active = active_rule_count(user_id)
    block_total = store.execute("SELECT COUNT(*) c FROM blocked_words WHERE user_id=?", (user_id,)).fetchone()["c"]
    target_total = store.execute("SELECT COUNT(*) c FROM targets WHERE user_id=?", (user_id,)).fetchone()["c"]

    lines = [f"状态：{'⏸ 已暂停（/resume 恢复）' if paused else '▶️ 运行中'}", ""]
    if subs:
        lines.append(f"订阅源（{len(subs)}）：")
        lines.extend("· " + describe_subscription(user_id, row) for row in subs)
    else:
        lines.append("订阅源：无")
    lines += [
        f"关键词：启用 {rule_active} / 共 {rule_total}",
        f"屏蔽词：{block_total}",
        f"推送目标：{target_total} / {MAX_TARGETS}",
        f"RSS 检查间隔：{POLL_INTERVAL} 秒",
    ]

    problems = []
    if paused:
        problems.append("推送已暂停，发送 /resume 恢复。")
    if not subs:
        problems.append("还没有订阅任何源：/feeds 查看，/subscribe <源ID> 订阅。")
    if not target_total:
        problems.append("没有推送目标：私聊发送 /start 添加。")
    keyword_feeds = [f"#{row['id']}" for row in subs if not row["full_push"]]
    if keyword_feeds and not rule_active:
        problems.append(
            f"源 {'、'.join(keyword_feeds)} 是关键词推送，但你没有启用的关键词，它们不会推送任何文章。"
            "用 /keywords 添加关键词，或用 /all <源ID> on 改成全量推送。"
        )
    if any(not row["enabled"] for row in subs):
        problems.append("有订阅的源已被管理员停用。")
    if problems:
        lines += ["", "⚠️ 需要注意："] + [f"{index}. {text}" for index, text in enumerate(problems, 1)]
    else:
        lines += ["", "✅ 设置完整。有符合条件的新文章时就会推送。"]
    await respond(update, "\n".join(lines))


async def feedstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not is_admin(update.effective_user.id):
        await respond(update, "仅管理员可用。")
        return
    rows = store.execute(
        "SELECT f.id, f.title, f.url, f.enabled, f.last_checked, f.last_error, "
        "(SELECT COUNT(*) FROM subscriptions s WHERE s.feed_id=f.id) subscribers FROM feeds f ORDER BY f.id"
    ).fetchall()
    if not rows:
        await respond(update, "还没有源。添加：/addfeed <RSS地址>")
        return
    body = "\n\n".join(
        f"{r['id']}. {r['title']}{'（已停用）' if not r['enabled'] else ''}\n{r['url']}\n"
        f"订阅人数：{r['subscribers']} · 上次检查：{format_time(r['last_checked'])}\n"
        f"{'❌ 错误：' + r['last_error'] if r['last_error'] else '✅ 正常'}"
        for r in rows
    )
    await respond(update, f"RSS 源状态（时间：{DISPLAY_TZ_NAME}）：\n\n{body}")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    command = (update.effective_message.text or "").split()[0]
    await respond(update, f"没有 {command} 这个命令。发送 /help 查看可用命令。")


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await respond(update, "我只处理以 / 开头的命令。发送 /help 查看使用说明。")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error while handling an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message and is_private(update):
        try:
            await update.effective_message.reply_text("处理命令时出错了，请稍后再试。如果一直出现，请联系管理员查看日志。")
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Matching and delivery
# ---------------------------------------------------------------------------

def is_blocked(user_id: int, text: str) -> bool:
    searchable = text.casefold()
    rows = store.execute("SELECT value FROM blocked_words WHERE user_id=?", (user_id,)).fetchall()
    return any(row["value"].casefold() in searchable for row in rows)


def matched_rules(user_id: int, text: str) -> list[str]:
    searchable = text.casefold()
    hits: list[str] = []
    rules = store.execute("SELECT kind,value FROM rules WHERE user_id=? AND active=1", (user_id,)).fetchall()
    for rule in rules:
        values = [part.casefold() for part in comma_values(rule["value"])]
        if rule["kind"] == "word":
            is_hit = any(value in searchable for value in values)
        else:
            is_hit = bool(values) and all(value in searchable for value in values)
        if is_hit:
            hits.append(display_rule(rule["kind"], rule["value"]))
    return hits


def push_card(title: str, link: str = "") -> str:
    """Build a linked title; Telegram renders the native source preview below it."""
    safe_title = html.escape(title)
    if not link:
        return f"<b>{safe_title}</b>"
    return f'<a href="{html.escape(link, quote=True)}"><b>{safe_title}</b></a>'


def retry_seconds(value) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


async def send_with_retry(app: Application, target, text: str) -> None:
    """Send one notification, waiting out flood limits and short network errors."""
    chat_id = target["chat_id"]
    attempt = 0
    while True:
        attempt += 1
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=False),
            )
            return
        except RetryAfter as error:
            if attempt >= 4:
                raise
            await asyncio.sleep(retry_seconds(error.retry_after) + 1)
        except ChatMigrated as error:
            # A group was upgraded to a supergroup; remember the new chat id.
            if attempt >= 2:
                raise
            chat_id = str(error.new_chat_id)
            try:
                store.execute("UPDATE targets SET chat_id=? WHERE id=?", (chat_id, target["id"]))
                store.commit()
            except sqlite3.IntegrityError:
                store.connection.rollback()
        except (BadRequest, Forbidden):
            raise
        except NetworkError:
            if attempt >= 3:
                raise
            await asyncio.sleep(2 * attempt)


def describe_send_error(error: Exception) -> str:
    if isinstance(error, Forbidden):
        return f"机器人被移出或被拉黑：{error}"
    return f"{type(error).__name__}: {error}"


def record_delivery(user_id: int, item_id: int, target_id: int, matched: str, status: str, error: str | None) -> None:
    store.execute(
        "INSERT INTO deliveries(user_id,item_id,target_id,matched,sent_at,status,error) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(item_id,target_id) DO UPDATE SET matched=excluded.matched, sent_at=excluded.sent_at, "
        "status=excluded.status, error=excluded.error",
        (user_id, item_id, target_id, matched, now(), status, error),
    )
    store.commit()


def finish_item(item_id: int) -> None:
    store.execute("UPDATE items SET pending=0, summary=NULL WHERE id=?", (item_id,))
    store.commit()


async def deliver_item(app: Application, item) -> None:
    item_id, feed_id = item["id"], item["feed_id"]
    if not get_feed(feed_id):
        finish_item(item_id)
        return
    title, link, summary = item["title"], item["link"] or "", item["summary"] or ""
    categories = [name for name in (item["categories"] or "").split("\n") if name]
    searchable = f"{title}\n{summary}"
    already_sent = {
        row["target_id"] for row in store.execute("SELECT target_id FROM deliveries WHERE item_id=? AND status='sent'", (item_id,))
    }
    subscriptions = store.execute(
        "SELECT s.user_id, u.paused, COALESCE(p.active, 0) full_push FROM subscriptions s "
        "JOIN users u ON u.user_id=s.user_id "
        "LEFT JOIN full_pushes p ON p.user_id=s.user_id AND p.feed_id=s.feed_id WHERE s.feed_id=?",
        (feed_id,),
    ).fetchall()
    message = push_card(title, link)
    for subscription in subscriptions:
        user_id = subscription["user_id"]
        if subscription["paused"]:
            continue
        selected = selected_section_names(user_id, feed_id)
        if selected and not set(selected).intersection(categories):
            continue
        if is_blocked(user_id, searchable):
            continue
        matches = ["全量推送"] if subscription["full_push"] else matched_rules(user_id, searchable)
        if not matches:
            continue
        matched = "；".join(matches)
        for target in store.execute("SELECT id, chat_id FROM targets WHERE user_id=?", (user_id,)).fetchall():
            if target["id"] in already_sent:
                continue
            try:
                await send_with_retry(app, target, message)
                record_delivery(user_id, item_id, target["id"], matched, "sent", None)
            except Exception as error:
                log.warning("Delivery to %s failed: %s", target["chat_id"], error)
                record_delivery(user_id, item_id, target["id"], matched, "failed", describe_send_error(error)[:500])
    finish_item(item_id)


def entry_categories(entry) -> list[str]:
    names = []
    for tag in entry.get("tags", []) or []:
        name = clean_text(tag.get("term", "") or "")
        if name and name not in names:
            names.append(name)
    return names


def process_feed(feed, parsed) -> int:
    """Store a fetched feed; returns how many new items were queued for delivery."""
    feed_id = feed["id"]
    if parsed.get("status") == 304:
        store.execute("UPDATE feeds SET last_checked=?, last_error=NULL WHERE id=?", (now(), feed_id))
        store.commit()
        return 0
    initialized = bool(feed["initialized"])
    queued = 0
    for entry in parsed.entries:
        title = clean_text(entry.get("title", "")) or "无标题"
        link = entry.get("link", "") or ""
        categories = entry_categories(entry)
        for category in categories:
            store.execute("INSERT OR IGNORE INTO sections(feed_id,name) VALUES(?,?)", (feed_id, category))
        item_key = entry.get("id") or hashlib.sha256((link + title).encode()).hexdigest()
        if store.execute("SELECT 1 FROM items WHERE feed_id=? AND item_key=?", (feed_id, item_key)).fetchone():
            continue
        summary = clean_text(entry.get("summary", entry.get("description", "")) or "") if initialized else None
        store.execute(
            "INSERT INTO items(feed_id,item_key,title,link,summary,categories,first_seen,pending) VALUES(?,?,?,?,?,?,?,?)",
            (feed_id, item_key, title, link, summary, "\n".join(categories), now(), int(initialized)),
        )
        queued += int(initialized)
    # Only mark the baseline as built once articles were actually read, so a
    # temporarily empty response cannot make the next fetch push old articles.
    store.execute(
        "UPDATE feeds SET initialized=?, last_checked=?, last_error=NULL, etag=?, modified=? WHERE id=?",
        (int(initialized or bool(parsed.entries)), now(), parsed.get("etag"), parsed.get("modified"), feed_id),
    )
    store.commit()
    return queued


async def deliver_pending(app: Application) -> None:
    for item in store.execute("SELECT * FROM items WHERE pending=1 ORDER BY id").fetchall():
        try:
            await deliver_item(app, item)
        except Exception:
            log.exception("Delivering item %s failed", item["id"])
            finish_item(item["id"])


_last_prune = 0.0


def prune_history() -> None:
    global _last_prune
    if not DELIVERY_RETENTION_DAYS:
        return
    if _last_prune and time.monotonic() - _last_prune < 86400:
        return
    _last_prune = time.monotonic()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DELIVERY_RETENTION_DAYS)).isoformat(timespec="seconds")
    cursor = store.execute("DELETE FROM deliveries WHERE sent_at < ?", (cutoff,))
    store.commit()
    if cursor.rowcount:
        log.info("Pruned %s delivery records older than %s days", cursor.rowcount, DELIVERY_RETENTION_DAYS)


async def poll_once(app: Application) -> None:
    feeds_to_check = store.execute("SELECT * FROM feeds WHERE enabled=1 ORDER BY id").fetchall()
    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def fetch(feed):
        async with semaphore:
            try:
                return feed, await asyncio.to_thread(fetch_feed, feed["url"], feed["etag"], feed["modified"]), None
            except Exception as error:
                return feed, None, error

    for feed, parsed, error in await asyncio.gather(*(fetch(feed) for feed in feeds_to_check)):
        if error is None:
            try:
                process_feed(feed, parsed)
            except Exception as processing_error:
                store.connection.rollback()
                error = processing_error
        if error is not None:
            log.warning("RSS fetch failed for %s: %s", feed["url"], error)
            store.execute(
                "UPDATE feeds SET last_checked=?, last_error=? WHERE id=?", (now(), str(error)[:500] or type(error).__name__, feed["id"])
            )
            store.commit()
    await deliver_pending(app)
    prune_history()


async def poll_loop(app: Application) -> None:
    while True:
        try:
            await poll_once(app)
        except Exception:
            # Never let one unexpected error stop polling for good.
            log.exception("Polling round failed")
        await asyncio.sleep(POLL_INTERVAL)


def bot_commands(pairs: list[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(name, description) for name, description in pairs]


async def register_commands(app: Application) -> None:
    """Populate Telegram's "/" menu so users can see commands and descriptions."""
    try:
        await app.bot.set_my_commands(bot_commands(USER_COMMANDS), scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(bot_commands(GROUP_COMMANDS), scope=BotCommandScopeAllGroupChats())
    except TelegramError as error:
        log.warning("Could not register command menu: %s", error)
        return
    for admin_id in ADMIN_USER_IDS:
        try:
            await app.bot.set_my_commands(bot_commands(USER_COMMANDS + ADMIN_COMMANDS), scope=BotCommandScopeChat(admin_id))
        except TelegramError as error:
            # Fails until the admin has opened a private chat with the bot.
            log.info("Admin command menu not set for %s: %s", admin_id, error)


async def post_init(app: Application) -> None:
    await register_commands(app)
    # Keep our own reference: Application.create_task() is not tracked before
    # the application is running, so the task would be destroyed on shutdown.
    app.bot_data["poll_task"] = asyncio.create_task(poll_loop(app))
    log.info("Polling RSS every %s seconds", POLL_INTERVAL)


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.pop("poll_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


COMMAND_HANDLERS = {
    "start": start, "help": help_command, "whoami": whoami, "feeds": feeds,
    "addfeed": addfeed, "delfeed": delfeed, "subscribe": subscribe, "unsubscribe": unsubscribe,
    "mysources": my_sources, "all": all_posts, "keywords": keywords, "combo": combo,
    "kwon": kwon, "kwoff": kwoff, "on": kwon, "off": kwoff,
    "delkw": delkw, "block": block, "blocks": blocks, "delblock": delblock,
    "sections": sections, "clearsections": clear_sections, "addtarget": addtarget,
    "targets": targets, "deltarget": deltarget, "pause": pause, "resume": resume,
    "history": history, "status": status, "feedstatus": feedstatus,
}


def build_application() -> Application:
    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    for name, handler in COMMAND_HANDLERS.items():
        app.add_handler(CommandHandler(name, handler))
    private = filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(private & filters.FORWARDED, forwarded_message))
    app.add_handler(MessageHandler(private & filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, plain_text))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    if not TOKEN:
        raise SystemExit("TG_BOT_TOKEN is missing in .env")
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
