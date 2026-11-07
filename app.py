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
import sys
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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MessageOriginChannel,
    Update,
)
from telegram.constants import ParseMode  # noqa: E402
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

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
# Formatting helpers
#
# Every reply is sent as Telegram HTML.  Dynamic values (feed titles,
# keywords, section names, error texts...) must go through e(); example
# commands that take arguments go through c(), which renders them in a
# monospace "tap to copy" style.  Telegram only makes the "/command" part of
# "/command arg" clickable, so a clickable "/subscribe 1" would actually send
# a bare "/subscribe".  Commands without arguments stay plain and clickable.
# ---------------------------------------------------------------------------

def e(value) -> str:
    return html.escape(str(value), quote=False)


def c(command: str) -> str:
    return f"<code>{e(command)}</code>"


TAG_PATTERN = re.compile(r"<[^>]+>")


def html_to_plain(text: str) -> str:
    return html.unescape(TAG_PATTERN.sub("", text))


# ---------------------------------------------------------------------------
# Help texts
# ---------------------------------------------------------------------------

COPY_TIP = "💡 等宽字体的命令（如 <code>/subscribe 1</code>）点一下即可复制，改成自己的内容后发送；蓝色的命令直接点就会发送。"

HELP_HOME = f"""📖 <b>使用说明</b>

<b>第一次使用，按顺序操作：</b>
1. 发送 /feeds，查看有哪些 RSS 源。每行开头的数字是“源ID”
2. 订阅：{c("/subscribe 1")}（1 换成你要的源ID）
3. 选择推送方式（二选一）：
   · 这个源的新文章全部推送（不看关键词）：{c("/all 1 on")}
   · 只推送包含关键词的文章：{c("/keywords OpenAI,Claude")}
4. 发送 /status，确认最后一行是“✅ 设置完整”

⚠️ 刚订阅的源默认是“关键词推送”：如果没有添加关键词，也没有开启全量推送，就收不到任何消息。

{COPY_TIP}

点下面的按钮查看详细说明 👇"""

HELP_TOPICS: dict[str, tuple[str, str]] = {
    "sub": (
        "📥 订阅与推送方式",
        f"""📥 <b>订阅与推送方式</b>

<b>订阅</b>
/feeds — 查看所有源和源ID
{c("/subscribe 1")} — 订阅 1 号源
/mysources — 查看我订阅了哪些源
{c("/unsubscribe 1")} — 取消订阅 1 号源（同时清除它的推送方式和版块设置）

<b>推送方式</b>（每个源分别设置）
· 全量推送：这个源的新文章都推送，<b>不看关键词</b>
· 关键词推送：只推送包含你的关键词的文章（刚订阅时默认是这种）

/all — 查看每个源当前的推送方式
{c("/all 1 on")} — 1 号源改为全量推送
{c("/all 1 off")} — 1 号源改为关键词推送

<b>注意</b>
· 只推送订阅之后出现的新文章，不补发旧文章
· 全量推送的源即使添加了关键词也不起作用；想按关键词筛选，要改回关键词推送
· 全量推送的文章同样会被屏蔽词和版块筛选过滤""",
    ),
    "kw": (
        "🔍 关键词与屏蔽词",
        f"""🔍 <b>关键词与屏蔽词</b>

· <b>关键词只对“关键词推送”的源生效</b>。全量推送的源不看关键词，新文章都会推送
· <b>屏蔽词对你订阅的所有源都生效</b>，包括全量推送的源
· 关键词和屏蔽词都不能单独给某个源设置
机器人检查文章的标题和摘要：不区分英文大小写，只要包含就算命中。

<b>普通关键词</b>：包含其中任意一个就推送
{c("/keywords OpenAI,Claude")} — 添加两个关键词
/keywords — 查看所有关键词和它们的编号（关键词ID）

<b>组合关键词</b>：必须同时包含所有词才推送
{c("/combo dmit,corona")} — 同时包含 dmit 和 corona 才推送

<b>暂停、启用、删除</b>（数字是关键词ID，用 /keywords 查看）
{c("/kwoff 3")} — 暂停 3 号关键词
{c("/kwon 3")} — 重新启用 3 号关键词
{c("/delkw 3")} — 删除 3 号关键词

<b>屏蔽词</b>：包含其中任意一个就不推送（全量推送也会被拦截）
{c("/block 求购,已出")} — 添加两个屏蔽词
/blocks — 查看屏蔽词和编号
{c("/delblock 2")} — 删除 2 号屏蔽词

💡 多个词之间用逗号隔开，中文逗号、英文逗号、顿号都可以。
⚠️ 太短的英文词容易误命中，例如 AI 也会命中 said、mail。""",
    ),
    "sec": (
        "🗂 版块筛选",
        f"""🗂 <b>版块筛选</b>（每个源分别设置）

不设置时，这个源所有版块的文章都会推送。

<b>第 1 步</b>：查看 1 号源有哪些版块
{c("/sections 1")}
回复里每行开头的数字是版块ID，例如：
1. 日常（daily）
142. 技术（tech）

<b>第 2 步</b>：只保留想看的版块
{c("/sections 1 1,142")} — 只推送日常和技术
⚠️ 每次设置都会替换之前的选择。想再加一个版块，要把所有想要的版块ID一起写上。

<b>恢复全部版块</b>
{c("/clearsections 1")}

<b>注意</b>
· 版块来自文章自带的分类。新添加的源要等机器人检查一次后才能看到；最近没人发帖的版块可能暂时不显示
· 设置版块筛选后，以后新出现的版块不会自动加入
· 有的 RSS 不提供分类，这种源无法按版块筛选，可以改用关键词""",
    ),
    "target": (
        "📤 推送到群/频道",
        """📤 <b>推送到哪里</b>

推送会发到你的<b>每一个</b>推送目标（最多 {max_targets} 个），不能指定某个源只发到某个目标。

<b>私聊</b>：发送 /start 自动添加

<b>群组</b>
1. 把机器人拉进群
2. 群管理员在群里发送 /addtarget

<b>频道</b>
1. 把机器人设为频道管理员，并允许它发布消息
2. 从频道转发任意一条消息给机器人，它会回复这个频道的 chat_id
3. 在私聊里发送 <code>/addtarget -1001234567890</code>（换成上一步得到的 chat_id）
   公开频道也可以用用户名：<code>/addtarget @频道用户名</code>
你本人也必须是这个频道的管理员。

/targets — 查看推送目标和编号（目标ID）
<code>/deltarget 2</code> — 删除 2 号目标""",
    ),
    "rules": (
        "⚙️ 推送规则",
        f"""⚙️ <b>推送规则</b>

RSS 里出现一篇新文章时，<b>同时满足</b>下面所有条件才会推送给你：
1. 你订阅了这个源
2. 你没有暂停推送（/pause）
3. 文章在你选的版块里（没设置版块筛选就不限）
4. 文章的标题和摘要里没有你的屏蔽词
5. 这个源是全量推送；或者是关键词推送，并且文章命中了你的某个关键词

满足后，会发到你所有的推送目标。

<b>每个源分别设置</b>：推送方式（/all）、版块筛选
<b>只对关键词推送的源生效</b>：关键词（全量推送的源不看关键词）
<b>对所有源统一生效</b>：屏蔽词、暂停、推送目标

<b>几种编号不要混用</b>
· 源ID：/feeds 查看
· 关键词ID：/keywords 查看
· 屏蔽词ID：/blocks 查看
· 版块ID：{c("/sections 1")} 查看（1 是源ID）
· 目标ID：/targets 查看
例如 {c("/all 1 off")} 里的 1 是源ID，{c("/kwoff 1")} 里的 1 是关键词ID，两者无关。""",
    ),
    "status": (
        "📊 状态与历史",
        f"""📊 <b>状态与历史</b>

/status — 查看全部设置。收不到推送时先发这个，它会指出问题
/history — 最近 10 条推送记录
{c("/history 30")} — 最近 30 条（最多 50 条）
/pause — 暂停所有推送（设置会保留）
/resume — 恢复推送（暂停期间的文章不补发）
/whoami — 查看你的用户ID；在群里发送还会显示本群的 chat_id""",
    ),
    "admin": (
        "🛠 管理员命令",
        f"""🛠 <b>管理员命令</b>

设置管理员：在服务器上运行 <code>rss-tg-bot admin 你的用户ID</code>（用户ID 用 /whoami 查看）。

{c("/addfeed https://example.com/feed.xml")} — 添加 RSS 源（会先试读一次）
{c("/delfeed 3")} — 删除 3 号源，以及所有人对它的订阅和设置（推送历史保留）
/feedstatus — 查看每个源是否读取正常
/renumber — 整理编号：把所有人的关键词和屏蔽词按顺序重新编为 1、2、3……（会先预览，确认后才执行）

新添加的源第一次检查只记录现有文章，不会推送旧文章。
删除关键词后编号不会补位，编号只增不减，这是为了防止删错；需要时用 /renumber 整理。""",
    ),
}

HELP_LAYOUT = [("sub", "kw"), ("sec", "target"), ("rules", "status"), ("admin",)]

HELP_ALIASES = {
    "1": "sub", "订阅": "sub", "源": "sub", "推送方式": "sub", "模式": "sub", "sub": "sub", "subscribe": "sub",
    "2": "kw", "关键词": "kw", "屏蔽": "kw", "屏蔽词": "kw", "kw": "kw", "keywords": "kw",
    "3": "sec", "版块": "sec", "板块": "sec", "sections": "sec", "sec": "sec",
    "4": "target", "目标": "target", "群组": "target", "频道": "target", "target": "target", "targets": "target",
    "5": "rules", "规则": "rules", "rules": "rules",
    "6": "status", "状态": "status", "历史": "status", "status": "status",
    "7": "admin", "管理": "admin", "管理员": "admin", "admin": "admin",
}


def help_page(key: str) -> tuple[str, InlineKeyboardMarkup]:
    if key in HELP_TOPICS:
        text = HELP_TOPICS[key][1].replace("{max_targets}", str(MAX_TARGETS))
        keyboard = [[InlineKeyboardButton("◀ 返回目录", callback_data="help:home")]]
        return text, InlineKeyboardMarkup(keyboard)
    keyboard = [
        [InlineKeyboardButton(HELP_TOPICS[name][0], callback_data=f"help:{name}") for name in row] for row in HELP_LAYOUT
    ]
    return HELP_HOME, InlineKeyboardMarkup(keyboard)


USER_COMMANDS = [
    ("start", "开始使用，并把私聊设为推送目标"),
    ("help", "使用说明"),
    ("status", "查看我的设置，并提示收不到推送的原因"),
    ("feeds", "查看可订阅的 RSS 源"),
    ("subscribe", "订阅源，例：/subscribe 1"),
    ("mysources", "我订阅的源和推送方式"),
    ("unsubscribe", "取消订阅，例：/unsubscribe 1"),
    ("all", "推送方式，例：/all 1 on 或 /all 1 off"),
    ("keywords", "查看或添加关键词，例：/keywords OpenAI,Claude"),
    ("combo", "添加组合关键词，例：/combo dmit,corona"),
    ("kwoff", "暂停关键词，例：/kwoff 3"),
    ("kwon", "启用关键词，例：/kwon 3"),
    ("delkw", "删除关键词，例：/delkw 3"),
    ("block", "添加屏蔽词，例：/block 求购,已出"),
    ("blocks", "查看屏蔽词"),
    ("delblock", "删除屏蔽词，例：/delblock 2"),
    ("sections", "版块筛选，例：/sections 1"),
    ("clearsections", "恢复全部版块，例：/clearsections 1"),
    ("addtarget", "添加推送目标（群组/频道）"),
    ("targets", "查看推送目标"),
    ("deltarget", "删除推送目标，例：/deltarget 2"),
    ("history", "最近的推送记录"),
    ("pause", "暂停全部推送"),
    ("resume", "恢复推送"),
    ("whoami", "查看我的用户ID和当前 chat_id"),
]
ADMIN_COMMANDS = [
    ("addfeed", "管理员：添加 RSS 源"),
    ("delfeed", "管理员：删除 RSS 源"),
    ("feedstatus", "管理员：RSS 源读取状态"),
    ("renumber", "管理员：把关键词和屏蔽词重新编为 1、2、3……"),
]
GROUP_COMMANDS = [
    ("addtarget", "群管理员：把本群设为推送目标"),
    ("whoami", "查看我的用户ID和本群 chat_id"),
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
                f"你不在机器人的允许名单内。请把你的用户ID {user.id} 发给管理员。" if user else "你不在机器人的允许名单内。"
            )
        return False
    store.ensure_user(user.id)
    return True


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def respond(update: Update, message: str, reply_markup=None) -> None:
    chunks = split_message(message)
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        try:
            await update.effective_message.reply_text(
                chunk, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup
            )
        except BadRequest as error:
            # Safety net: never leave the user without an answer because of markup.
            log.error("HTML reply rejected (%s); sending as plain text", error)
            await update.effective_message.reply_text(html_to_plain(chunk), link_preview_options=NO_PREVIEW, reply_markup=markup)


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
    mode = "全量推送（不看关键词）" if row["full_push"] else "关键词推送"
    names = selected_section_names(user_id, row["id"])
    sections_text = "全部版块" if not names else "只看 " + "、".join(display_section_name(n) for n in names)
    disabled = "（已被管理员停用）" if not row["enabled"] else ""
    return f"{row['id']}. {e(row['title'])}{disabled} — {mode} · {e(sections_text)}"


def full_push_note(user_id: int) -> str:
    """Explain that keywords are ignored for feeds in full-push mode."""
    ids = [row["id"] for row in subscription_rows(user_id) if row["full_push"]]
    if not ids:
        return ""
    names = "、".join(f"{feed_id} 号" for feed_id in ids)
    return (
        f"\n\n注意：{names}源是全量推送，不看关键词，新文章都会推送。"
        f"想让关键词对它生效，改回关键词推送：{c(f'/all {ids[0]} off')}"
    )


def feed_label(feed_id: int, title: str | None) -> str:
    return f"{feed_id} 号源「{e(title)}」" if title else f"{feed_id} 号源"


# ---------------------------------------------------------------------------
# Commands: basics
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not is_private(update):
        await respond(update, "请在和机器人的私聊里发送 /start 完成设置。\n想把本群设为推送目标的话，请群管理员在本群发送 /addtarget。")
        return
    user = update.effective_user
    status, _ = add_target(user.id, update.effective_chat.id, user.full_name)
    target_line = {
        "added": "✅ 已把当前私聊设为推送目标。",
        "exists": "✅ 当前私聊已经是推送目标。",
        "full": f"⚠️ 推送目标已达上限（{MAX_TARGETS} 个），当前私聊没有添加。发送 /targets 查看。",
    }[status]
    await respond(
        update,
        f"""👋 <b>欢迎使用 RSS 提醒机器人</b>
{target_line}

<b>按顺序操作即可开始：</b>
1. 发送 /feeds，查看有哪些 RSS 源，记下每行开头的数字（源ID）
2. 订阅：{c("/subscribe 1")}（1 换成你要的源ID）
3. 选择推送方式（二选一）：
   · 这个源的新文章全部推送（不看关键词）：{c("/all 1 on")}
   · 只推送包含关键词的文章：{c("/keywords OpenAI,Claude")}
4. 发送 /status 检查设置

⚠️ 刚订阅的源默认是“关键词推送”：如果没有添加关键词，也没有开启全量推送，就收不到任何消息。

{COPY_TIP}
发送 /help 查看详细说明。""",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    topic = command_text(context).casefold()
    text, markup = help_page(HELP_ALIASES.get(topic, "home"))
    await respond(update, text, reply_markup=markup)


async def help_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not query.from_user or not is_allowed(query.from_user.id):
        return
    text, markup = help_page((query.data or "").partition(":")[2])
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
    except BadRequest as error:
        if "not modified" not in str(error).lower():
            raise


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    lines = [f"你的用户ID：{c(update.effective_user.id)}"]
    chat = update.effective_chat
    if chat and chat.type != "private":
        lines.append(f"本群（{e(chat.title or '群组')}）的 chat_id：{c(chat.id)}")
        lines.append("群管理员在本群发送 /addtarget，就能把本群设为推送目标。")
    else:
        lines.append("需要频道的 chat_id？从频道转发任意一条消息给我即可。")
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
        await respond(update, "目前还没有 RSS 源，请联系管理员添加。")
        return
    lines = []
    for row in rows:
        state = "（已停用）" if not row["enabled"] else ("  ✅ 已订阅" if row["subscribed"] else "")
        lines.append(f"{row['id']}. {e(row['title'])}{state}")
    example = next((row["id"] for row in rows if row["enabled"] and not row["subscribed"]), rows[0]["id"])
    await respond(
        update,
        "<b>可订阅的 RSS 源</b>（每行开头的数字是源ID）\n" + "\n".join(lines)
        + f"\n\n订阅：{c(f'/subscribe {example}')}（数字换成你要的源ID）",
    )


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
        await respond(update, "只有管理员可以添加 RSS 源。")
        return
    url = command_text(context)
    if not url.startswith(("https://", "http://")):
        await respond(update, f"请在命令后面写上 RSS 地址，例如：\n{c('/addfeed https://example.com/feed.xml')}")
        return
    if store.execute("SELECT 1 FROM feeds WHERE url=?", (url,)).fetchone():
        await respond(update, "这个 RSS 源已经添加过了，发送 /feeds 查看。")
        return
    try:
        parsed = await asyncio.to_thread(fetch_feed, url)
    except Exception as error:
        await respond(update, f"无法读取这个 RSS：{e(str(error)[:300])}")
        return
    if not parsed.entries:
        await respond(update, "能打开这个地址，但没有读到任何文章。请确认它是 RSS 或 Atom 地址。")
        return
    title = clean_text(parsed.feed.get("title", "")) or urlparse(url).netloc
    try:
        cursor = store.execute("INSERT INTO feeds(title, url) VALUES(?, ?)", (title, url))
        store.commit()
    except sqlite3.IntegrityError:
        await respond(update, "这个 RSS 源已经添加过了，发送 /feeds 查看。")
        return
    feed_id = cursor.lastrowid
    await respond(
        update,
        f"✅ 已添加 {feed_label(feed_id, title)}\n"
        f"用户发送 {c(f'/subscribe {feed_id}')} 即可订阅。\n"
        "第一次检查只记录现有文章，不会推送旧文章。",
    )


async def delfeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not is_admin(update.effective_user.id):
        await respond(update, "只有管理员可以删除 RSS 源。")
        return
    feed_id = int_arg(context)
    if feed_id is None or len(context.args) != 1:
        await respond(update, f"请在命令后面写上源ID，例如：{c('/delfeed 3')}\n源ID可以用 /feeds 查看。")
        return
    feed = get_feed(feed_id)
    if not feed:
        await respond(update, f"没有 {feed_id} 号源，发送 /feeds 查看。")
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
    await respond(update, f"已删除 {feed_label(feed_id, feed['title'])}，以及所有人对它的订阅和设置。推送历史保留。")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    feed_id = int_arg(context)
    if feed_id is None:
        await respond(update, f"请在命令后面写上源ID，例如：{c('/subscribe 1')}\n源ID可以用 /feeds 查看。")
        return
    feed = get_feed(feed_id)
    if not feed or not feed["enabled"]:
        await respond(update, f"没有可以订阅的 {feed_id} 号源，发送 /feeds 查看。")
        return
    label = feed_label(feed_id, feed["title"])
    if is_subscribed(user_id, feed_id):
        await respond(update, f"你已经订阅了{label}。发送 /mysources 查看推送方式。")
        return
    store.execute("INSERT INTO subscriptions(user_id, feed_id, created_at) VALUES(?, ?, ?)", (user_id, feed_id, now()))
    store.commit()
    keyword_count = active_rule_count(user_id)
    if keyword_count:
        keyword_line = f"你已有 {keyword_count} 个启用的关键词，会马上对这个源生效。"
    else:
        keyword_line = "⚠️ 你还没有关键词，所以这个源现在不会推送任何文章。"
    await respond(
        update,
        f"""✅ 已订阅{label}
当前推送方式：关键词推送（默认）
{keyword_line}

<b>下一步（二选一）：</b>
· 这个源的新文章全部推送（不看关键词）：{c(f"/all {feed_id} on")}
· 添加关键词：{c("/keywords OpenAI,Claude")}（换成你想要的词；关键词对你所有关键词推送的源都生效）

只推送订阅之后出现的新文章。""",
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    feed_id = int_arg(context)
    if feed_id is None:
        await respond(update, f"请在命令后面写上源ID，例如：{c('/unsubscribe 1')}\n你订阅的源可以用 /mysources 查看。")
        return
    if not is_subscribed(user_id, feed_id):
        await respond(update, f"你没有订阅 {feed_id} 号源。发送 /mysources 查看你的订阅。")
        return
    feed = get_feed(feed_id)
    for table in ("subscriptions", "full_pushes", "selected_sections"):
        store.execute(f"DELETE FROM {table} WHERE user_id=? AND feed_id=?", (user_id, feed_id))
    store.commit()
    await respond(update, f"已取消订阅{feed_label(feed_id, feed['title'] if feed else None)}，它的推送方式和版块设置也已清除。")


async def my_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    rows = subscription_rows(user_id)
    if not rows:
        await respond(update, f"你还没有订阅任何源。先发送 /feeds 查看，再用 {c('/subscribe 1')} 订阅。")
        return
    body = "\n".join(describe_subscription(user_id, row) for row in rows)
    first = rows[0]["id"]
    await respond(
        update,
        f"<b>我的订阅</b>（每行开头的数字是源ID）\n{body}\n\n"
        f"改推送方式：{c(f'/all {first} on')} 或 {c(f'/all {first} off')}\n"
        f"设置版块：{c(f'/sections {first}')}",
    )


async def all_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    args = context.args or []
    if not args:
        rows = subscription_rows(user_id)
        if not rows:
            await respond(update, f"你还没有订阅任何源。先发送 /feeds 查看，再用 {c('/subscribe 1')} 订阅。")
            return
        body = "\n".join(
            f"{r['id']}. {e(r['title'])} — {'✅ 全量推送（不看关键词）' if r['full_push'] else '🔍 关键词推送'}" for r in rows
        )
        first = rows[0]["id"]
        await respond(
            update,
            f"<b>推送方式</b>（每行开头的数字是源ID）\n{body}\n\n"
            f"改为全量推送：{c(f'/all {first} on')}\n改为关键词推送：{c(f'/all {first} off')}",
        )
        return
    feed_id = int_arg(context)
    option = args[1].lower() if len(args) == 2 else ""
    if feed_id is None or option not in {"on", "off"}:
        await respond(
            update,
            f"写法：源ID 后面加 on 或 off，例如：\n{c('/all 1 on')} — 全量推送（不看关键词）\n{c('/all 1 off')} — 关键词推送\n"
            "只发送 /all 可以查看当前设置。",
        )
        return
    if not is_subscribed(user_id, feed_id):
        await respond(update, f"你没有订阅 {feed_id} 号源。发送 /mysources 查看你订阅的源。")
        return
    active = int(option == "on")
    store.execute(
        "INSERT INTO full_pushes(user_id,feed_id,active,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id,feed_id) DO UPDATE SET active=excluded.active,updated_at=excluded.updated_at",
        (user_id, feed_id, active, now()),
    )
    store.commit()
    label = feed_label(feed_id, get_feed(feed_id)["title"])
    if active:
        await respond(
            update,
            f"✅ {label}已改为全量推送：新文章都会推送，<b>不再看关键词</b>。\n"
            f"（屏蔽词和版块筛选仍然有效。想按关键词筛选，改回：{c(f'/all {feed_id} off')}）",
        )
        return
    message = f"🔍 {label}已改为关键词推送：只推送包含你关键词的文章。"
    if not active_rule_count(user_id):
        message += f"\n⚠️ 你现在没有启用的关键词，这个源不会推送任何文章。添加关键词：{c('/keywords OpenAI,Claude')}"
    await respond(update, message)


# ---------------------------------------------------------------------------
# Commands: keywords and blocked words
# ---------------------------------------------------------------------------

def short_word_warning(words: list[str]) -> str:
    short = [word for word in words if is_short_ascii(word)]
    if not short:
        return ""
    return (
        f"\n\n⚠️ {e('、'.join(short))} 比较短，容易误命中（例如 AI 也会命中 said、mail）。"
        "如果推送太多，可以删掉它换成更完整的词，或者用 /combo 组合关键词。"
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
                skipped.append(f"{existing[value.casefold()]}. {e(value)}")
                continue
            cursor = store.execute(
                "INSERT INTO rules(user_id, kind, value, created_at) VALUES(?, 'word', ?, ?)", (user_id, value, now())
            )
            added.append(f"{cursor.lastrowid}. {e(value)}")
        store.commit()
        if not values:
            await respond(update, f"没有识别到关键词。写法：{c('/keywords OpenAI,Claude')}")
            return
        parts = []
        if added:
            parts.append("✅ 已添加关键词（文章包含其中任意一个就推送）：\n" + "\n".join(added))
        if skipped:
            parts.append("这些关键词之前已经有了，没有重复添加：\n" + "\n".join(skipped))
        message = (
            "\n\n".join(parts)
            + short_word_warning([v for v in values if v.casefold() not in existing])
            + full_push_note(user_id)
        )
        if added and not store.execute("SELECT 1 FROM subscriptions WHERE user_id=?", (user_id,)).fetchone():
            message += f"\n\n提示：你还没有订阅任何源。先发送 /feeds 查看，再用 {c('/subscribe 1')} 订阅。"
        await respond(update, message)
        return
    rows = store.execute("SELECT id, kind, value, active FROM rules WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    if not rows:
        await respond(
            update,
            f"你还没有关键词。\n\n添加普通关键词（包含任意一个就推送）：{c('/keywords OpenAI,Claude')}\n"
            f"添加组合关键词（全部包含才推送）：{c('/combo dmit,corona')}",
        )
        return
    body = "\n".join(
        f"{r['id']}. {'组合' if r['kind'] == 'combo' else '普通'}：{e(display_rule(r['kind'], r['value']))}  "
        f"{'✅ 启用中' if r['active'] else '⏸ 已暂停'}"
        for r in rows
    )
    first = rows[0]["id"]
    await respond(
        update,
        f"<b>我的关键词</b>（每行开头的数字是关键词ID）\n对你所有“关键词推送”的源生效。\n\n{body}\n\n"
        f"暂停：{c(f'/kwoff {first}')}　启用：{c(f'/kwon {first}')}　删除：{c(f'/delkw {first}')}\n"
        "（把数字换成要操作的关键词ID）" + full_push_note(user_id),
    )


async def combo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    values = unique_values(comma_values(command_text(context)))
    if len(values) < 2:
        await respond(
            update,
            f"组合关键词至少要两个不同的词，用逗号隔开，例如：\n{c('/combo dmit,corona')}\n"
            "表示文章必须同时包含 dmit 和 corona 才推送。",
        )
        return
    wanted = {value.casefold() for value in values}
    for row in store.execute("SELECT id, value FROM rules WHERE user_id=? AND kind='combo'", (user_id,)):
        if {value.casefold() for value in comma_values(row["value"])} == wanted:
            await respond(update, f"这个组合关键词已经有了：{row['id']}. {e(display_rule('combo', row['value']))}")
            return
    cursor = store.execute(
        "INSERT INTO rules(user_id, kind, value, created_at) VALUES(?, 'combo', ?, ?)", (user_id, ",".join(values), now())
    )
    store.commit()
    await respond(
        update,
        f"✅ 已添加组合关键词 {cursor.lastrowid}：{e(' + '.join(values))}\n文章必须同时包含这些词才会推送。"
        + full_push_note(user_id),
    )


async def set_rule_state(update: Update, context: ContextTypes.DEFAULT_TYPE, active: int) -> None:
    if not await guard(update):
        return
    command = "/kwon" if active else "/kwoff"
    rule_id = int_arg(context)
    if rule_id is None or len(context.args) != 1:
        await respond(
            update,
            f"请在命令后面写上关键词ID，例如：{c(f'{command} 3')}\n关键词ID可以用 /keywords 查看。\n\n"
            f"如果你想改的是某个源的推送方式，请用 {c('/all 1 on')} 或 {c('/all 1 off')}。",
        )
        return
    rule = store.execute("SELECT kind, value FROM rules WHERE id=? AND user_id=?", (rule_id, update.effective_user.id)).fetchone()
    if not rule:
        await respond(update, f"没有找到你的 {rule_id} 号关键词。发送 /keywords 查看关键词ID。")
        return
    store.execute("UPDATE rules SET active=? WHERE id=?", (active, rule_id))
    store.commit()
    action = "✅ 已启用" if active else "⏸ 已暂停"
    await respond(update, f"{action} {rule_id} 号关键词：{e(display_rule(rule['kind'], rule['value']))}")


async def kwon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_rule_state(update, context, 1)


async def kwoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_rule_state(update, context, 0)


async def delkw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rule_id = int_arg(context)
    if rule_id is None:
        await respond(update, f"请在命令后面写上关键词ID，例如：{c('/delkw 3')}\n关键词ID可以用 /keywords 查看。")
        return
    rule = store.execute("SELECT kind, value FROM rules WHERE id=? AND user_id=?", (rule_id, update.effective_user.id)).fetchone()
    if not rule:
        await respond(update, f"没有找到你的 {rule_id} 号关键词。发送 /keywords 查看关键词ID。")
        return
    store.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    store.commit()
    await respond(update, f"已删除 {rule_id} 号关键词：{e(display_rule(rule['kind'], rule['value']))}")


async def block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    values = unique_values(comma_values(command_text(context)))
    if not values:
        await respond(
            update,
            f"请在命令后面写上屏蔽词，多个词用逗号隔开，例如：\n{c('/block 求购,已出')}\n"
            "文章的标题或摘要包含其中任意一个词就不会推送。",
        )
        return
    existing = {
        row["value"].casefold(): row["id"] for row in store.execute("SELECT id, value FROM blocked_words WHERE user_id=?", (user_id,))
    }
    added, skipped = [], []
    for value in values:
        if value.casefold() in existing:
            skipped.append(f"{existing[value.casefold()]}. {e(value)}")
            continue
        cursor = store.execute("INSERT INTO blocked_words(user_id, value, created_at) VALUES(?, ?, ?)", (user_id, value, now()))
        added.append(f"{cursor.lastrowid}. {e(value)}")
    store.commit()
    parts = []
    if added:
        parts.append("🚫 已添加屏蔽词（对所有源生效，全量推送也会被拦截）：\n" + "\n".join(added))
    if skipped:
        parts.append("这些屏蔽词之前已经有了，没有重复添加：\n" + "\n".join(skipped))
    await respond(update, "\n\n".join(parts))


async def blocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = store.execute("SELECT id, value FROM blocked_words WHERE user_id=? ORDER BY id", (update.effective_user.id,)).fetchall()
    if not rows:
        await respond(update, f"你还没有屏蔽词。添加：{c('/block 求购,已出')}")
        return
    body = "\n".join(f"{r['id']}. {e(r['value'])}" for r in rows)
    await respond(
        update,
        f"<b>我的屏蔽词</b>（每行开头的数字是屏蔽词ID）\n{body}\n\n"
        f"删除：{c(f'/delblock {rows[0][0]}')}（把数字换成要删除的屏蔽词ID）",
    )


async def delblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    block_id = int_arg(context)
    if block_id is None:
        await respond(update, f"请在命令后面写上屏蔽词ID，例如：{c('/delblock 2')}\n屏蔽词ID可以用 /blocks 查看。")
        return
    row = store.execute("SELECT value FROM blocked_words WHERE id=? AND user_id=?", (block_id, update.effective_user.id)).fetchone()
    if not row:
        await respond(update, f"没有找到你的 {block_id} 号屏蔽词。发送 /blocks 查看。")
        return
    store.execute("DELETE FROM blocked_words WHERE id=?", (block_id,))
    store.commit()
    await respond(update, f"已删除 {block_id} 号屏蔽词：{e(row['value'])}")


# ---------------------------------------------------------------------------
# Commands: sections
# ---------------------------------------------------------------------------

async def sections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    feed_id = int_arg(context)
    if feed_id is None:
        await respond(
            update,
            f"请在命令后面写上源ID：\n{c('/sections 1')} — 查看 1 号源的版块\n"
            f"{c('/sections 1 1,142')} — 1 号源只推送 1 号和 142 号版块\n你订阅的源可以用 /mysources 查看。",
        )
        return
    feed = get_feed(feed_id)
    if not feed:
        await respond(update, f"没有 {feed_id} 号源。发送 /mysources 查看你订阅的源。")
        return
    label = feed_label(feed_id, feed["title"])
    available = {
        row["id"]: row["name"] for row in store.execute("SELECT id, name FROM sections WHERE feed_id=? ORDER BY id", (feed_id,))
    }
    if len(context.args) > 1:
        if not is_subscribed(user_id, feed_id):
            await respond(update, f"请先订阅这个源：{c(f'/subscribe {feed_id}')}")
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
                f"{e('、'.join(invalid) or '（空）')} 不是{label}的版块ID，原来的设置没有改动。\n"
                f"先发送 {c(f'/sections {feed_id}')} 查看这个源的版块ID。",
            )
            return
        store.execute("DELETE FROM selected_sections WHERE user_id=? AND feed_id=?", (user_id, feed_id))
        for section_id in valid:
            store.execute(
                "INSERT INTO selected_sections(user_id, feed_id, section_id) VALUES(?, ?, ?)", (user_id, feed_id, section_id)
            )
        store.commit()
        names = "、".join(display_section_name(available[section_id]) for section_id in valid)
        message = (
            f"✅ {label}现在只推送这些版块：{e(names)}\n"
            f"（之前的选择已被替换。恢复全部版块：{c(f'/clearsections {feed_id}')}）"
        )
        if invalid:
            message += f"\n这些不是有效的版块ID，已忽略：{e('、'.join(invalid))}"
        await respond(update, message)
        return
    chosen = {
        row["section_id"]
        for row in store.execute("SELECT section_id FROM selected_sections WHERE user_id=? AND feed_id=?", (user_id, feed_id))
    }
    if not available:
        await respond(
            update,
            f"{label}还没有发现版块。\n版块来自文章自带的分类：新添加的源要等机器人检查一次后才有；"
            "如果一直没有，说明这个 RSS 不提供分类，可以改用关键词筛选。",
        )
        return
    current = "推送全部版块" if not chosen else "只推送打 ✅ 的版块"
    body = "\n".join(
        f"{section_id}. {e(display_section_name(name))}{'  ✅' if section_id in chosen else ''}" for section_id, name in available.items()
    )
    sample = ",".join(str(section_id) for section_id in list(available)[:2])
    await respond(
        update,
        f"<b>{label}的版块</b>（每行开头的数字是版块ID）\n当前：{current}\n\n{body}\n\n"
        f"只推送某些版块：{c(f'/sections {feed_id} {sample}')}（换成你要的版块ID，会替换之前的选择）\n"
        f"恢复全部版块：{c(f'/clearsections {feed_id}')}",
    )


async def clear_sections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    feed_id = int_arg(context)
    if feed_id is None:
        await respond(update, f"请在命令后面写上源ID，例如：{c('/clearsections 1')}")
        return
    cursor = store.execute("DELETE FROM selected_sections WHERE user_id=? AND feed_id=?", (update.effective_user.id, feed_id))
    store.commit()
    if cursor.rowcount:
        await respond(update, f"✅ {feed_id} 号源已恢复为推送全部版块。")
    else:
        await respond(update, f"{feed_id} 号源本来就是推送全部版块，不需要恢复。")


# ---------------------------------------------------------------------------
# Commands: targets
# ---------------------------------------------------------------------------

def add_target(user_id: int, chat_id: int | str, title: str) -> tuple[str, str]:
    """Returns (status, HTML message)."""
    if store.execute("SELECT 1 FROM targets WHERE user_id=? AND chat_id=?", (user_id, str(chat_id))).fetchone():
        return "exists", f"「{e(title)}」已经是你的推送目标了。"
    count = store.execute("SELECT COUNT(*) count FROM targets WHERE user_id=?", (user_id,)).fetchone()["count"]
    if count >= MAX_TARGETS:
        return "full", f"每人最多 {MAX_TARGETS} 个推送目标，已经满了。发送 /targets 查看，删掉不需要的再试。"
    store.execute(
        "INSERT INTO targets(user_id, chat_id, title, created_at) VALUES(?, ?, ?, ?)", (user_id, str(chat_id), title, now())
    )
    store.commit()
    return "added", f"✅ 已添加推送目标：{e(title)}（chat_id：{e(chat_id)}）"


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
            await respond(update, "在群里只需要发送 /addtarget，不用加任何内容。绑定频道请在私聊里操作。")
            return
        if not await is_chat_admin(context, chat.id, user_id):
            hint = "\n你现在是以匿名管理员身份发言，机器人无法确认身份。请先在群设置里关闭“匿名”再试。" if user_id == ANONYMOUS_ADMIN_ID else ""
            await respond(update, "只有群管理员才能把本群设为推送目标。" + hint)
            return
        if not await bot_can_post(context, chat):
            await respond(update, "机器人在本群没有发消息的权限，请先在群设置里允许它发消息。")
            return
        _, message = add_target(user_id, chat.id, chat.title or str(chat.id))
        await respond(update, message)
        return
    if not args:
        status, message = add_target(user_id, chat.id, update.effective_user.full_name)
        if status == "added":
            message = "✅ 已把当前私聊设为推送目标。"
        await respond(
            update,
            f"{message}\n\n<b>绑定群组</b>：把机器人拉进群，由群管理员在群里发送 /addtarget\n"
            f"<b>绑定频道</b>：在私聊发送 {c('/addtarget -1001234567890')}（换成频道的 chat_id）\n"
            "频道的 chat_id：从频道转发任意一条消息给我就能看到。详细步骤见 /help 里的“推送到群/频道”。",
        )
        return
    raw = args[0]
    try:
        target_chat = await context.bot.get_chat(int(raw) if raw.lstrip("-").isdigit() else raw)
    except (TelegramError, ValueError):
        await respond(
            update,
            f"找不到 {e(raw)}。请检查：\n1. chat_id 或 @用户名 是否写对（频道的 chat_id 一般以 -100 开头）\n"
            "2. 机器人是否已经在这个群或频道里（频道要设为管理员）\n\n频道的 chat_id：从频道转发任意一条消息给我就能看到。",
        )
        return
    if target_chat.type not in {"group", "supergroup", "channel"}:
        await respond(update, "这里只能绑定群组或频道。私聊发送 /start 就会自动设为推送目标。")
        return
    if not await is_chat_admin(context, target_chat.id, user_id):
        await respond(update, "无法确认你是这个群或频道的管理员。只有管理员才能绑定；绑定频道时，机器人也必须是频道管理员。")
        return
    if not await bot_can_post(context, target_chat):
        await respond(update, "机器人在这个群或频道里没有发消息的权限。频道需要把机器人设为管理员，并允许它发布消息。")
        return
    _, message = add_target(user_id, target_chat.id, target_chat.title or target_chat.username or str(target_chat.id))
    await respond(update, message)


async def targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = store.execute("SELECT id, chat_id, title FROM targets WHERE user_id=? ORDER BY id", (update.effective_user.id,)).fetchall()
    if not rows:
        await respond(update, "你还没有推送目标。在私聊里发送 /start，就会把私聊设为推送目标。")
        return
    body = "\n".join(f"{r['id']}. {e(r['title'] or r['chat_id'])}（chat_id：{e(r['chat_id'])}）" for r in rows)
    await respond(
        update,
        f"<b>我的推送目标</b>（每行开头的数字是目标ID，共 {len(rows)}/{MAX_TARGETS} 个）\n{body}\n\n"
        f"每个目标都会收到同样的推送。\n删除：{c(f'/deltarget {rows[-1][0]}')}（把数字换成要删除的目标ID）",
    )


async def deltarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    target_id = int_arg(context)
    if target_id is None:
        await respond(update, f"请在命令后面写上目标ID，例如：{c('/deltarget 2')}\n目标ID可以用 /targets 查看。")
        return
    row = store.execute("SELECT chat_id, title FROM targets WHERE id=? AND user_id=?", (target_id, update.effective_user.id)).fetchone()
    if not row:
        await respond(update, f"没有找到你的 {target_id} 号推送目标。发送 /targets 查看。")
        return
    store.execute("DELETE FROM targets WHERE id=?", (target_id,))
    store.commit()
    await respond(update, f"已删除 {target_id} 号推送目标：{e(row['title'] or row['chat_id'])}")


async def forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    origin = update.effective_message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        chat = origin.chat
        await respond(
            update,
            f"频道「{e(chat.title)}」的 chat_id 是 {c(chat.id)}\n\n"
            f"先确认机器人已经是这个频道的管理员（允许发布消息），然后发送：\n{c(f'/addtarget {chat.id}')}",
        )
        return
    await respond(update, "这条消息不是从频道转发来的，看不到频道的 chat_id。\n绑定群组的话，请群管理员直接在群里发送 /addtarget。")


# ---------------------------------------------------------------------------
# Commands: status
# ---------------------------------------------------------------------------

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        store.execute("UPDATE users SET paused=1 WHERE user_id=?", (update.effective_user.id,))
        store.commit()
        await respond(update, "⏸ 已暂停你的所有推送，设置都会保留。恢复请发送 /resume")


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
        state = "✅ 已发送" if r["status"] == "sent" else f"❌ 发送失败：{e((r['error'] or '未知原因')[:80])}"
        title = e(r["title"])
        if (r["link"] or "").startswith(("http://", "https://")):
            title = f'<a href="{html.escape(r["link"], quote=True)}">{title}</a>'
        entries.append(
            f"<b>[{e(r['feed'])}]</b> {title}\n命中：{e(r['matched'])} · {state}\n发到：{e(r['target'])} · {format_time(r['sent_at'])}"
        )
    tail = "" if requested else f"\n\n查看更多：{c('/history 30')}（最多 50 条）"
    await respond(update, f"<b>最近 {len(rows)} 条推送记录</b>（{e(DISPLAY_TZ_NAME)} 时间）\n\n" + "\n\n".join(entries) + tail)


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

    lines = [f"<b>我的状态</b>：{'⏸ 已暂停' if paused else '▶️ 推送中'}", ""]
    if subs:
        lines.append(f"<b>订阅的源</b>（{len(subs)} 个）")
        lines.extend(describe_subscription(user_id, row) for row in subs)
    else:
        lines.append("<b>订阅的源</b>：无")
    lines += [
        "",
        f"关键词：{rule_active} 个启用中（共 {rule_total} 个）",
        f"屏蔽词：{block_total} 个",
        f"推送目标：{target_total} 个（最多 {MAX_TARGETS} 个）",
        f"检查 RSS 的间隔：{POLL_INTERVAL} 秒",
    ]

    problems = []
    if paused:
        problems.append("推送已暂停。发送 /resume 恢复。")
    if not subs:
        problems.append(f"还没有订阅任何源。发送 /feeds 查看，再用 {c('/subscribe 1')} 订阅。")
    if not target_total:
        problems.append("没有推送目标。在私聊里发送 /start 添加。")
    keyword_feeds = [row["id"] for row in subs if not row["full_push"]]
    if keyword_feeds and not rule_active:
        ids = "、".join(f"{feed_id} 号" for feed_id in keyword_feeds)
        problems.append(
            f"{ids}源是关键词推送，但你没有启用的关键词，所以不会推送任何文章。"
            f"添加关键词：{c('/keywords OpenAI,Claude')}；或者改成全量推送：{c(f'/all {keyword_feeds[0]} on')}"
        )
    if any(not row["enabled"] for row in subs):
        problems.append("你订阅的源里有被管理员停用的。")
    if problems:
        lines += ["", "<b>⚠️ 需要处理：</b>"] + [f"{index}. {text}" for index, text in enumerate(problems, 1)]
    else:
        lines += ["", "✅ 设置完整。有符合条件的新文章时就会推送给你。"]
    await respond(update, "\n".join(lines))


async def feedstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not is_admin(update.effective_user.id):
        await respond(update, "只有管理员可以使用这个命令。")
        return
    rows = store.execute(
        "SELECT f.id, f.title, f.url, f.enabled, f.last_checked, f.last_error, "
        "(SELECT COUNT(*) FROM subscriptions s WHERE s.feed_id=f.id) subscribers FROM feeds f ORDER BY f.id"
    ).fetchall()
    if not rows:
        await respond(update, f"还没有 RSS 源。添加：{c('/addfeed https://example.com/feed.xml')}")
        return
    body = "\n\n".join(
        f"<b>{r['id']}. {e(r['title'])}</b>{'（已停用）' if not r['enabled'] else ''}\n{e(r['url'])}\n"
        f"订阅人数：{r['subscribers']} · 上次检查：{format_time(r['last_checked'])}\n"
        f"{'❌ 读取失败：' + e(r['last_error']) if r['last_error'] else '✅ 读取正常'}"
        for r in rows
    )
    await respond(update, f"<b>RSS 源状态</b>（{e(DISPLAY_TZ_NAME)} 时间）\n\n{body}")


# ---------------------------------------------------------------------------
# Renumbering keywords and blocked words (admin)
#
# IDs are shared by all users and never reused, so they only grow.  An admin
# can compact them to 1..n on demand.  Nothing else references these IDs:
# delivery history stores the matched keyword text, not the rule ID.
# ---------------------------------------------------------------------------

RENUMBER_TABLES = (("rules", "关键词"), ("blocked_words", "屏蔽词"))


def renumber_plan(table: str) -> list[tuple[int, int]]:
    ids = [row[0] for row in store.execute(f"SELECT id FROM {table} ORDER BY id")]
    return [(old, new) for new, old in enumerate(ids, 1)]


def next_id(table: str) -> int:
    row = store.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
    return (row[0] if row else 0) + 1


def renumber_needed() -> bool:
    for table, _ in RENUMBER_TABLES:
        plan = renumber_plan(table)
        if any(old != new for old, new in plan) or next_id(table) != len(plan) + 1:
            return True
    return False


def renumber_all() -> dict[str, dict[int, int]]:
    """Renumber every keyword and blocked word to 1..n, keeping their order.

    Runs in a single transaction: either everything is renumbered or nothing.
    Returns {table: {old_id: new_id}}.
    """
    result: dict[str, dict[int, int]] = {}
    store.commit()
    with store.connection:
        for table, _ in RENUMBER_TABLES:
            plan = renumber_plan(table)
            # Two passes through negative IDs so no step collides with an existing ID.
            for old, new in plan:
                if old != new:
                    store.execute(f"UPDATE {table} SET id=? WHERE id=?", (-new, old))
            store.execute(f"UPDATE {table} SET id=-id WHERE id<0")
            store.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
            if plan:
                store.execute("INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)", (table, len(plan)))
            result[table] = dict(plan)
    return result


def describe_item(table: str, row) -> str:
    return e(display_rule(row["kind"], row["value"])) if table == "rules" else e(row["value"])


def user_items(table: str, user_id: int):
    columns = "id, kind, value" if table == "rules" else "id, value"
    return store.execute(f"SELECT {columns} FROM {table} WHERE user_id=? ORDER BY id", (user_id,)).fetchall()


def renumber_report(user_id: int, mapping: dict[str, dict[int, int]], done: bool) -> str:
    """Show the caller's own changes; other users' items are only counted."""
    lines = []
    others = []
    for table, label in RENUMBER_TABLES:
        rows = user_items(table, user_id)
        inverse = {new: old for old, new in mapping[table].items()}
        own_ids = set()
        changes = []
        for row in rows:
            # After renumbering the rows already carry their new IDs.
            if done:
                new, old = row["id"], inverse.get(row["id"], row["id"])
            else:
                old, new = row["id"], mapping[table].get(row["id"], row["id"])
            own_ids.add(old)
            if old != new:
                changes.append(f"{old} → {new}　{describe_item(table, row)}")
        lines.append(f"<b>你的{label}</b>：" + ("编号不变" if not changes else ""))
        lines.extend(changes)
        moved_by_others = sum(1 for o, n in mapping[table].items() if o != n and o not in own_ids)
        if moved_by_others:
            others.append(f"{moved_by_others} 条{label}")
    if others:
        lines.append(f"其他用户：共有 {'、'.join(others)}的编号{'已' if done else '会'}改变")
    return "\n".join(lines)


def renumber_preview_mapping() -> dict[str, dict[int, int]]:
    return {table: dict(renumber_plan(table)) for table, _ in RENUMBER_TABLES}


RENUMBER_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("✅ 确认整理", callback_data="renumber:yes"), InlineKeyboardButton("取消", callback_data="renumber:no")]]
)


async def renumber(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not is_admin(update.effective_user.id):
        await respond(update, "只有管理员可以整理编号。")
        return
    if not renumber_needed():
        await respond(update, "关键词和屏蔽词的编号已经是从 1 开始连续的，不需要整理。")
        return
    report = renumber_report(update.effective_user.id, renumber_preview_mapping(), done=False)
    await respond(
        update,
        "🔢 <b>整理编号</b>\n"
        "把所有用户的关键词和屏蔽词，按现在的顺序重新编为 1、2、3……之后新建的接着往后排。\n\n"
        f"{report}\n\n"
        "⚠️ 整理后，之前记下的编号会失效，请以 /keywords 和 /blocks 显示的为准。推送不受影响。",
        reply_markup=RENUMBER_KEYBOARD,
    )


async def renumber_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not query.from_user or not is_admin(query.from_user.id):
        return
    if query.data != "renumber:yes":
        text = "已取消，编号没有改动。"
    elif not renumber_needed():
        text = "编号已经是从 1 开始连续的，不需要整理。"
    else:
        mapping = renumber_all()
        text = (
            "✅ <b>编号已整理</b>\n\n"
            f"{renumber_report(query.from_user.id, mapping, done=True)}\n\n"
            f"下一个新关键词的编号是 {next_id('rules')}，新屏蔽词是 {next_id('blocked_words')}。\n"
            "查看：/keywords　/blocks"
        )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=None)


def cli_renumber() -> None:
    """`python app.py renumber`: used by `rss-tg-bot renumber` on the server."""
    if not renumber_needed():
        print("关键词和屏蔽词的编号已经是从 1 开始连续的，不需要整理。")
        return
    mapping = renumber_all()
    for table, label in RENUMBER_TABLES:
        changed = [(old, new) for old, new in mapping[table].items() if old != new]
        print(f"{label}：共 {len(mapping[table])} 条，{len(changed)} 条编号改变")
        for old, new in changed:
            print(f"  {old} → {new}")
        print(f"  下一个新{label}的编号：{next_id(table)}")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    command = (update.effective_message.text or "").split()[0]
    await respond(update, f"没有 {e(command)} 这个命令。发送 /help 查看所有命令。")


async def plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await respond(update, "请发送以 / 开头的命令。不知道怎么用的话，发送 /help 查看说明。")


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
    "history": history, "status": status, "feedstatus": feedstatus, "renumber": renumber,
}


def build_application() -> Application:
    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    for name, handler in COMMAND_HANDLERS.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(help_button, pattern=r"^help:"))
    app.add_handler(CallbackQueryHandler(renumber_button, pattern=r"^renumber:"))
    private = filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(private & filters.FORWARDED, forwarded_message))
    app.add_handler(MessageHandler(private & filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, plain_text))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    if sys.argv[1:] == ["renumber"]:
        cli_renumber()
        return
    if not TOKEN:
        raise SystemExit("TG_BOT_TOKEN is missing in .env")
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
