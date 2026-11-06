"""Multi-user RSS keyword notifier for Telegram."""
import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
from dotenv import load_dotenv
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TOKEN = os.getenv("TG_BOT_TOKEN", "")
POLL_INTERVAL = max(10, int(os.getenv("POLL_INTERVAL_SECONDS", "10")))
MAX_TARGETS = max(1, int(os.getenv("MAX_TARGETS_PER_USER", "10")))


def parse_ids(name: str) -> set[int]:
    return {int(value) for value in os.getenv(name, "").split(",") if value.strip().isdigit()}


ALLOWED_USER_IDS = parse_ids("ALLOWED_USER_IDS")
ADMIN_USER_IDS = parse_ids("ADMIN_USER_IDS")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rss_tg_bot")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]*>", " ", value or "")
    value = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def comma_values(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class Store:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(ROOT / "rss_tg_bot.db", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
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
        self.connection.commit()

    def execute(self, sql: str, params: tuple = ()):
        return self.connection.execute(sql, params)

    def commit(self) -> None:
        self.connection.commit()

    def ensure_user(self, user_id: int) -> None:
        self.execute("INSERT OR IGNORE INTO users(user_id, created_at) VALUES(?, ?)", (user_id, now()))
        self.commit()


store = Store()


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


async def guard(update: Update) -> bool:
    if not update.effective_user or not is_allowed(update.effective_user.id):
        await update.effective_message.reply_text("你不在机器人允许名单内。")
        return False
    store.ensure_user(update.effective_user.id)
    return True


async def respond(update: Update, message: str) -> None:
    await update.effective_message.reply_text(message, disable_web_page_preview=True)


def command_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chat = update.effective_chat
    if chat.type == "private":
        add_target(update.effective_user.id, chat.id, update.effective_user.full_name)
    await respond(
        update,
        """RSS 提醒机器人已准备好，当前私聊已设为推送目标。

开始使用：
1. /feeds 查看 RSS 源
2. /subscribe <源ID> 订阅
3. /all <源ID> on 开启全量推送；或用 /keywords 添加关键词
4. /status 检查配置

/help 查看完整命令与说明。""",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await respond(
        update,
        """常用命令（按使用顺序）：

① RSS 源与订阅
/feeds — 查看可用 RSS 源
/subscribe <源ID> — 订阅
/unsubscribe <源ID> — 取消订阅
/mysources — 查看我的订阅

② 推送方式
/all — 查看各源模式
/all <源ID> <on|off> — 开启/关闭该源全量推送

③ 关键词与过滤
/keywords [词1,词2] — 查看/添加普通关键词
/combo 词1,词2 — 添加组合关键词（全部命中）
/on <ID> /off <ID> /delkw <ID> — 管理关键词
/block 词1,词2 /blocks /delblock <ID> — 管理屏蔽词
/sections <源ID> [版块ID1,版块ID2] — 选择版块
/clearsections <源ID> — 恢复全部版块

④ 推送目标
/addtarget [chat_id] /targets /deltarget <ID>

⑤ 查询与控制
/status /history [数量]
/pause /resume /whoami

⑥ 管理员命令
/addfeed <RSS地址> /delfeed <源ID> /feedstatus""",
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        await respond(update, f"你的 Telegram 用户 ID：{update.effective_user.id}")


async def feeds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = store.execute("SELECT id, title, enabled FROM feeds ORDER BY id").fetchall()
    body = "\n".join(f"{row['id']}. {row['title']} {'✅' if row['enabled'] else '⏸'}" for row in rows)
    await respond(update, f"可订阅 RSS 源：\n{body or '暂无，请联系管理员添加。'}")


async def addfeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if update.effective_user.id not in ADMIN_USER_IDS:
        await respond(update, "仅管理员可添加共享 RSS 源。先用 /whoami 获取 ID，并在服务器 .env 的 ADMIN_USER_IDS 中配置。")
        return
    url = command_text(context)
    if not url.startswith(("https://", "http://")):
        await respond(update, "用法：/addfeed https://example.com/feed.xml")
        return
    parsed = await asyncio.to_thread(feedparser.parse, url)
    if not parsed.entries:
        await respond(update, "无法读取有效 RSS，或该源当前没有文章。")
        return
    title = clean_text(parsed.feed.get("title", "")) or urlparse(url).netloc
    try:
        store.execute("INSERT INTO feeds(title, url) VALUES(?, ?)", (title, url))
        store.commit()
        await respond(update, f"已添加：{title}。首次抓取只建立去重基线，不推送历史文章。")
    except sqlite3.IntegrityError:
        await respond(update, "这个 RSS 源已存在。")


async def delfeed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if update.effective_user.id not in ADMIN_USER_IDS or not context.args:
        await respond(update, "用法（管理员）：/delfeed <源ID>")
        return
    if len(context.args) != 2:
        await respond(update, "用法：/all <源ID> <on|off>。不带参数可查看模式。")
        return
    feed_id = context.args[0]
    store.execute("DELETE FROM subscriptions WHERE feed_id=?", (feed_id,))
    store.execute("DELETE FROM full_pushes WHERE feed_id=?", (feed_id,))
    store.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
    store.commit()
    await respond(update, "已删除 RSS 源及其订阅关系；推送历史会保留。")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args or not context.args[0].isdigit():
        await respond(update, "用法：/subscribe <源ID>")
        return
    feed = store.execute("SELECT title FROM feeds WHERE id=? AND enabled=1", (context.args[0],)).fetchone()
    if not feed:
        await respond(update, "未找到可订阅的 RSS 源。")
        return
    store.execute(
        "INSERT OR IGNORE INTO subscriptions(user_id, feed_id, created_at) VALUES(?, ?, ?)",
        (update.effective_user.id, context.args[0], now()),
    )
    store.commit()
    await respond(update, f"已订阅：{feed['title']}。只会推送之后出现的新文章。")


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await respond(update, "用法：/unsubscribe <源ID>")
        return
    store.execute("DELETE FROM subscriptions WHERE user_id=? AND feed_id=?", (update.effective_user.id, context.args[0]))
    store.execute("DELETE FROM full_pushes WHERE user_id=? AND feed_id=?", (update.effective_user.id, context.args[0]))
    store.commit()
    await respond(update, "已取消订阅。")


async def my_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = store.execute(
        "SELECT f.id, f.title FROM subscriptions s JOIN feeds f ON f.id=s.feed_id WHERE s.user_id=? ORDER BY f.id",
        (update.effective_user.id,),
    ).fetchall()
    await respond(update, "我的 RSS：\n" + ("\n".join(f"{row['id']}. {row['title']}" for row in rows) or "暂无订阅。"))


async def all_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    if not context.args:
        rows = store.execute(
            "SELECT f.id,f.title,COALESCE(p.active,0) active FROM subscriptions s JOIN feeds f ON f.id=s.feed_id LEFT JOIN full_pushes p ON p.user_id=s.user_id AND p.feed_id=s.feed_id WHERE s.user_id=? ORDER BY f.id",
            (user_id,),
        ).fetchall()
        body = "\n".join(f"{r['id']}. {r['title']} {'✅ 全量推送' if r['active'] else '关键词推送'}" for r in rows)
        await respond(update, "推送模式：\n" + (body or "暂无订阅。"))
        return
    feed_id = context.args[0]
    subscribed = feed_id.isdigit() and store.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND feed_id=?", (user_id, feed_id)).fetchone()
    if not subscribed:
        await respond(update, "请提供你已订阅的源 ID。先用 /mysources 查看。")
        return
    option = context.args[1].lower()
    if option not in {"on", "off"}:
        await respond(update, "用法：/all <源ID> <on|off>。")
        return
    active = int(option == "on")
    store.execute(
        "INSERT INTO full_pushes(user_id,feed_id,active,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id,feed_id) DO UPDATE SET active=excluded.active,updated_at=excluded.updated_at",
        (user_id, feed_id, active, now()),
    )
    store.commit()
    await respond(update, "已开启全量推送（屏蔽词和版块筛选仍生效）。" if active else "已切换为关键词推送。")


async def keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = command_text(context)
    if text:
        for value in comma_values(text):
            store.execute("INSERT INTO rules(user_id, kind, value, created_at) VALUES(?, 'word', ?, ?)", (update.effective_user.id, value, now()))
        store.commit()
        await respond(update, "普通关键词已添加。")
        return
    rows = store.execute("SELECT id, kind, value, active FROM rules WHERE user_id=? ORDER BY id", (update.effective_user.id,)).fetchall()
    body = "\n".join(f"{r['id']}. {'组合' if r['kind']=='combo' else '普通'}：{r['value']} {'✅' if r['active'] else '⏸'}" for r in rows)
    await respond(update, "我的关键词：\n" + (body or "暂无。"))


async def combo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    values = comma_values(command_text(context))
    if len(values) < 2:
        await respond(update, "用法：/combo <词1,词2>，至少两个词。")
        return
    store.execute("INSERT INTO rules(user_id, kind, value, created_at) VALUES(?, 'combo', ?, ?)", (update.effective_user.id, ",".join(values), now()))
    store.commit()
    await respond(update, f"已添加组合关键词：{' + '.join(values)}")


async def set_rule_state(update: Update, context: ContextTypes.DEFAULT_TYPE, active: int) -> None:
    if not await guard(update):
        return
    if not context.args:
        await respond(update, "请提供关键词 ID。")
        return
    store.execute("UPDATE rules SET active=? WHERE id=? AND user_id=?", (active, context.args[0], update.effective_user.id))
    store.commit()
    await respond(update, "关键词状态已更新。")


async def on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_rule_state(update, context, 1)


async def off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_rule_state(update, context, 0)


async def delkw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await respond(update, "用法：/delkw <关键词ID>")
        return
    store.execute("DELETE FROM rules WHERE id=? AND user_id=?", (context.args[0], update.effective_user.id))
    store.commit()
    await respond(update, "关键词已删除。")


async def block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    for value in comma_values(command_text(context)):
        store.execute("INSERT OR IGNORE INTO blocked_words(user_id, value, created_at) VALUES(?, ?, ?)", (update.effective_user.id, value, now()))
    store.commit()
    await respond(update, "屏蔽词已添加。")


async def blocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = store.execute("SELECT id, value FROM blocked_words WHERE user_id=? ORDER BY id", (update.effective_user.id,)).fetchall()
    await respond(update, "屏蔽词：\n" + ("\n".join(f"{r['id']}. {r['value']}" for r in rows) or "暂无。"))


async def delblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await respond(update, "用法：/delblock <屏蔽词ID>")
        return
    store.execute("DELETE FROM blocked_words WHERE id=? AND user_id=?", (context.args[0], update.effective_user.id))
    store.commit()
    await respond(update, "屏蔽词已删除。")


async def sections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await respond(update, "用法：/sections <源ID> [版块ID1,版块ID2]")
        return
    user_id, feed_id = update.effective_user.id, context.args[0]
    if len(context.args) > 1:
        store.execute("DELETE FROM selected_sections WHERE user_id=? AND feed_id=?", (user_id, feed_id))
        for section_id in comma_values(" ".join(context.args[1:])):
            if section_id.isdigit():
                store.execute("INSERT OR IGNORE INTO selected_sections(user_id, feed_id, section_id) VALUES(?, ?, ?)", (user_id, feed_id, section_id))
        store.commit()
        await respond(update, "版块选择已更新。")
        return
    rows = store.execute(
        "SELECT s.id,s.name,ss.section_id IS NOT NULL chosen FROM sections s LEFT JOIN selected_sections ss ON ss.section_id=s.id AND ss.user_id=? WHERE s.feed_id=? ORDER BY s.name",
        (user_id, feed_id),
    ).fetchall()
    body = "\n".join(f"{r['id']}. {r['name']} {'✅' if r['chosen'] else ''}" for r in rows)
    await respond(update, "版块（未选择任何版块表示全部）：\n" + (body or "此源尚未发现版块。"))


async def clear_sections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await respond(update, "用法：/clearsections <源ID>")
        return
    store.execute("DELETE FROM selected_sections WHERE user_id=? AND feed_id=?", (update.effective_user.id, context.args[0]))
    store.commit()
    await respond(update, "已恢复订阅此源的全部版块。")


def add_target(user_id: int, chat_id: int | str, title: str) -> str:
    if store.execute("SELECT 1 FROM targets WHERE user_id=? AND chat_id=?", (user_id, str(chat_id))).fetchone():
        return "这个推送目标已存在。"
    count = store.execute("SELECT COUNT(*) count FROM targets WHERE user_id=?", (user_id,)).fetchone()["count"]
    if count >= MAX_TARGETS:
        return f"每位用户最多 {MAX_TARGETS} 个推送目标。"
    store.execute("INSERT INTO targets(user_id, chat_id, title, created_at) VALUES(?, ?, ?, ?)", (user_id, str(chat_id), title, now()))
    store.commit()
    return "推送目标已添加。"


async def is_chat_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: str | int, user_id: int) -> bool:
    try:
        membership = await context.bot.get_chat_member(chat_id, user_id)
        return membership.status in {"administrator", "creator", "owner"}
    except Exception:
        return False


async def addtarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chat = update.effective_chat
    user_id = update.effective_user.id
    target_id = context.args[0] if context.args else chat.id
    if chat.type != "private" and context.args:
        await respond(update, "群组内请直接发送 /addtarget；频道请在私聊中绑定。")
        return
    if chat.type != "private" and not await is_chat_admin(context, chat.id, user_id):
        await respond(update, "仅群管理员可以绑定此群。")
        return
    if chat.type == "private" and context.args and not await is_chat_admin(context, target_id, user_id):
        await respond(update, "无法验证你是该群或频道的管理员，或机器人尚未在目标聊天中。")
        return
    await respond(update, add_target(user_id, target_id, chat.title or chat.full_name or str(target_id)))


async def targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = store.execute("SELECT id, chat_id, title FROM targets WHERE user_id=? ORDER BY id", (update.effective_user.id,)).fetchall()
    body = "\n".join(f"{r['id']}. {r['title'] or r['chat_id']} ({r['chat_id']})" for r in rows)
    await respond(update, "推送目标：\n" + (body or "暂无。"))


async def deltarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await respond(update, "用法：/deltarget <目标ID>")
        return
    store.execute("DELETE FROM targets WHERE id=? AND user_id=?", (context.args[0], update.effective_user.id))
    store.commit()
    await respond(update, "推送目标已删除。")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        store.execute("UPDATE users SET paused=1 WHERE user_id=?", (update.effective_user.id,))
        store.commit()
        await respond(update, "已暂停提醒。")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await guard(update):
        store.execute("UPDATE users SET paused=0 WHERE user_id=?", (update.effective_user.id,))
        store.commit()
        await respond(update, "已恢复提醒。")


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    limit = min(int(context.args[0]), 50) if context.args and context.args[0].isdigit() else 10
    rows = store.execute(
        "SELECT COALESCE(f.title, '已删除的 RSS') feed,i.title,i.link,d.matched,d.status,d.sent_at FROM deliveries d JOIN items i ON i.id=d.item_id LEFT JOIN feeds f ON f.id=i.feed_id WHERE d.user_id=? ORDER BY d.id DESC LIMIT ?",
        (update.effective_user.id, limit),
    ).fetchall()
    body = "\n\n".join(f"[{r['feed']}] {r['title']}\n命中：{r['matched']} · {r['status']}\n{r['link']}" for r in rows)
    await respond(update, "最近命中：\n" + (body or "暂无推送历史。"))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    user_id = update.effective_user.id
    paused = store.execute("SELECT paused FROM users WHERE user_id=?", (user_id,)).fetchone()["paused"]
    count = lambda table: store.execute(f"SELECT COUNT(*) count FROM {table} WHERE user_id=?", (user_id,)).fetchone()["count"]
    full_count = store.execute("SELECT COUNT(*) count FROM full_pushes WHERE user_id=? AND active=1", (user_id,)).fetchone()["count"]
    await respond(update, f"{'已暂停' if paused else '运行中'}\n订阅源：{count('subscriptions')}（全量推送：{full_count}）\n关键词：{count('rules')}\n屏蔽词：{count('blocked_words')}\n目标：{count('targets')}/{MAX_TARGETS}\n轮询间隔：{POLL_INTERVAL} 秒")


async def feedstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if update.effective_user.id not in ADMIN_USER_IDS:
        await respond(update, "仅管理员可用。")
        return
    rows = store.execute("SELECT id,title,last_checked,last_error FROM feeds ORDER BY id").fetchall()
    body = "\n\n".join(f"{r['id']}. {r['title']}\n上次检查：{r['last_checked'] or '-'}\n错误：{r['last_error'] or '无'}" for r in rows)
    await respond(update, "RSS 健康状态：\n" + (body or "暂无源。"))


def is_blocked(user_id: int, text: str) -> bool:
    searchable = text.casefold()
    blocks = store.execute("SELECT value FROM blocked_words WHERE user_id=?", (user_id,)).fetchall()
    return any(row["value"].casefold() in searchable for row in blocks)


def matched_rules(user_id: int, text: str) -> list[str]:
    searchable = text.casefold()
    hits: list[str] = []
    rules = store.execute("SELECT kind,value FROM rules WHERE user_id=? AND active=1", (user_id,)).fetchall()
    for rule in rules:
        values = [part.casefold() for part in comma_values(rule["value"])]
        is_hit = any(value in searchable for value in values) if rule["kind"] == "word" else bool(values) and all(value in searchable for value in values)
        if is_hit:
            hits.append(rule["value"])
    return hits


def push_card(title: str, feed_title: str, summary: str, categories: list[str], matches: list[str], link: str = "") -> str:
    """Build a linked title; Telegram renders the native source preview below it."""
    del feed_title, summary, categories, matches
    safe_title = html.escape(title)
    safe_link = html.escape(link, quote=True)
    linked_title = f'<a href="{safe_link}"><b>{safe_title}</b></a>' if link else f"<b>{safe_title}</b>"
    return linked_title


async def deliver(app: Application, feed_id: int, item_id: int, title: str, link: str, summary: str, categories: list[str]) -> None:
    feed_title = store.execute("SELECT title FROM feeds WHERE id=?", (feed_id,)).fetchone()["title"]
    subscriptions = store.execute("SELECT s.user_id,u.paused FROM subscriptions s JOIN users u ON u.user_id=s.user_id WHERE s.feed_id=?", (feed_id,)).fetchall()
    for subscription in subscriptions:
        if subscription["paused"]:
            continue
        selected = store.execute("SELECT sec.name FROM selected_sections ss JOIN sections sec ON sec.id=ss.section_id WHERE ss.user_id=? AND ss.feed_id=?", (subscription["user_id"], feed_id)).fetchall()
        if selected and not set(row["name"] for row in selected).intersection(categories):
            continue
        searchable = f"{title}\n{summary}"
        if is_blocked(subscription["user_id"], searchable):
            continue
        full_push = store.execute("SELECT active FROM full_pushes WHERE user_id=? AND feed_id=?", (subscription["user_id"], feed_id)).fetchone()
        matches = ["全量推送"] if full_push and full_push["active"] else matched_rules(subscription["user_id"], searchable)
        if not matches:
            continue
        message = push_card(title, feed_title, summary, categories, matches, link)
        for target in store.execute("SELECT * FROM targets WHERE user_id=?", (subscription["user_id"],)).fetchall():
            try:
                await app.bot.send_message(
                    chat_id=target["chat_id"],
                    text=message,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=False),
                )
                store.execute("INSERT OR IGNORE INTO deliveries(user_id,item_id,target_id,matched,sent_at,status) VALUES(?,?,?,?,?,?)", (subscription["user_id"], item_id, target["id"], "；".join(matches), now(), "sent"))
            except Exception as error:
                store.execute("INSERT OR IGNORE INTO deliveries(user_id,item_id,target_id,matched,sent_at,status,error) VALUES(?,?,?,?,?,?,?)", (subscription["user_id"], item_id, target["id"], "；".join(matches), now(), "failed", str(error)[:500]))
        store.commit()


async def poll_once(app: Application) -> None:
    for feed in store.execute("SELECT * FROM feeds WHERE enabled=1").fetchall():
        try:
            parsed = await asyncio.to_thread(feedparser.parse, feed["url"])
            fresh: list[tuple] = []
            for entry in parsed.entries:
                title = clean_text(entry.get("title", "无标题"))
                link = entry.get("link", "")
                summary = clean_text(entry.get("summary", entry.get("description", "")))
                categories = [clean_text(tag.get("term", "")) for tag in entry.get("tags", []) if clean_text(tag.get("term", ""))]
                for category in categories:
                    store.execute("INSERT OR IGNORE INTO sections(feed_id,name) VALUES(?,?)", (feed["id"], category))
                item_key = entry.get("id") or hashlib.sha256((link + title).encode()).hexdigest()
                exists = store.execute("SELECT id FROM items WHERE feed_id=? AND item_key=?", (feed["id"], item_key)).fetchone()
                if not exists:
                    cursor = store.execute("INSERT INTO items(feed_id,item_key,title,link,summary,categories,first_seen) VALUES(?,?,?,?,?,?,?)", (feed["id"], item_key, title, link, summary, ",".join(categories), now()))
                    if feed["initialized"]:
                        fresh.append((cursor.lastrowid, title, link, summary, categories))
            store.execute("UPDATE feeds SET initialized=1,last_checked=?,last_error=NULL WHERE id=?", (now(), feed["id"]))
            store.commit()
            for item in fresh:
                await deliver(app, feed["id"], *item)
        except Exception as error:
            log.exception("RSS fetch failed: %s", feed["url"])
            store.execute("UPDATE feeds SET last_checked=?,last_error=? WHERE id=?", (now(), str(error)[:500], feed["id"]))
            store.commit()


async def poll_loop(app: Application) -> None:
    while True:
        await poll_once(app)
        await asyncio.sleep(POLL_INTERVAL)


async def post_init(app: Application) -> None:
    app.create_task(poll_loop(app))
    log.info("Polling RSS every %s seconds", POLL_INTERVAL)


def main() -> None:
    if not TOKEN:
        raise SystemExit("TG_BOT_TOKEN is missing in .env")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    handlers = {
        "start": start, "help": help_command, "whoami": whoami, "feeds": feeds,
        "addfeed": addfeed, "delfeed": delfeed, "subscribe": subscribe, "unsubscribe": unsubscribe,
        "mysources": my_sources, "all": all_posts, "keywords": keywords, "combo": combo, "on": on, "off": off,
        "delkw": delkw, "block": block, "blocks": blocks, "delblock": delblock,
        "sections": sections, "clearsections": clear_sections, "addtarget": addtarget,
        "targets": targets, "deltarget": deltarget, "pause": pause, "resume": resume,
        "history": history, "status": status, "feedstatus": feedstatus,
    }
    for name, handler in handlers.items():
        app.add_handler(CommandHandler(name, handler))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
