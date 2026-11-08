"""Offline tests for app.py.  Run with:  python -m unittest discover -s tests -v"""
from __future__ import annotations

import asyncio
import http.server
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="rss-tg-bot-test-")
os.environ.update(
    {
        "DB_PATH": str(Path(TMP) / "test.db"),
        "ADMIN_USER_IDS": "1",
        "ALLOWED_USER_IDS": "",
        "MAX_TARGETS_PER_USER": "10",
        "POLL_INTERVAL_SECONDS": "60",
        "FETCH_TIMEOUT_SECONDS": "20",
        "DELIVERY_RETENTION_DAYS": "180",
        "TG_BOT_TOKEN": "",
        "no_proxy": "127.0.0.1,localhost",
        "NO_PROXY": "127.0.0.1,localhost",
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
from telegram import MessageOriginChannel  # noqa: E402
from telegram.error import Forbidden, RetryAfter  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

ALLOWED_TAG = re.compile(r'</?(b|i|u|s|code|pre)>|<a href="[^"<>]*">|</a>')
ENTITY = re.compile(r"&(lt|gt|amp|quot|#\d+);")


def assert_telegram_html(text: str) -> None:
    """Reject anything Telegram's HTML parser would refuse."""
    stripped = ALLOWED_TAG.sub("", text)
    if "<" in stripped or ">" in stripped:
        raise AssertionError(f"unescaped < or > in: {text!r}")
    if "&" in ENTITY.sub("", stripped):
        raise AssertionError(f"unescaped & in: {text!r}")
    for tag in ("b", "i", "u", "s", "code", "pre", "a"):
        opened = len(re.findall(rf"<{tag}[ >]", text))
        if opened != text.count(f"</{tag}>"):
            raise AssertionError(f"unbalanced <{tag}> in: {text!r}")
    if len(text) > 4096:
        raise AssertionError("message longer than 4096 characters")


CLICKABLE_WITH_ARGS = re.compile(r"(?<![\w/.])/([a-z]+)[ \t]+([A-Za-z0-9@\-][^\s]*)")


def assert_no_clickable_command_with_args(text: str) -> None:
    """Telegram only sends the "/command" part when tapped, so arguments must be in <code>."""
    outside = re.sub(r"<code>.*?</code>", "", text)
    outside = re.sub(r"<a href=\"[^\"]*\">.*?</a>", "", outside)  # article titles in /history
    match = CLICKABLE_WITH_ARGS.search(app.html_to_plain(outside))
    if match:
        raise AssertionError(f"clickable command with argument {match.group(0)!r} in: {text!r}")


class FakeMessage:
    def __init__(self, text="", forward_origin=None):
        self.text = text
        self.forward_origin = forward_origin
        self.replies: list[str] = []
        self.markups: list = []

    async def reply_text(self, text, **kwargs):
        if kwargs.get("parse_mode") == "HTML":
            assert_telegram_html(text)
            assert_no_clickable_command_with_args(text)
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FakeBot:
    id = 999

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail: dict[str, list[Exception]] = {}
        self.members: dict[tuple[str, int], SimpleNamespace] = {}
        self.chats: dict[str, SimpleNamespace] = {}

    async def send_message(self, chat_id, text, **kwargs):
        errors = self.fail.get(str(chat_id))
        if errors:
            raise errors.pop(0)
        self.sent.append((str(chat_id), text))

    async def get_chat_member(self, chat_id, user_id):
        member = self.members.get((str(chat_id), user_id))
        if member is None:
            from telegram.error import BadRequest

            raise BadRequest("user not found")
        return member

    async def get_chat(self, chat_id):
        chat = self.chats.get(str(chat_id))
        if chat is None:
            from telegram.error import BadRequest

            raise BadRequest("chat not found")
        return chat


BOT = FakeBot()
APP = SimpleNamespace(bot=BOT)


def run(coro):
    return asyncio.run(coro)


def call(handler, user_id=10, args=None, chat_type="private", chat_id=None, chat_title=None, message=None):
    message = message or FakeMessage("/" + handler.__name__)
    chat = SimpleNamespace(
        id=chat_id if chat_id is not None else user_id,
        type=chat_type,
        title=chat_title,
        full_name=f"User{user_id}",
        username=None,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, full_name=f"User{user_id}"),
        effective_chat=chat,
        effective_message=message,
    )
    context = SimpleNamespace(args=list(args or []), bot=BOT)
    run(handler(update, context))
    return "\n".join(message.replies)


def reset_db():
    for table in (
        "users", "feeds", "subscriptions", "full_pushes", "sections", "selected_sections",
        "rules", "blocked_words", "targets", "items", "deliveries",
    ):
        app.store.execute(f"DELETE FROM {table}")
        app.store.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
    app.store.commit()
    BOT.sent.clear()
    BOT.fail.clear()
    BOT.members.clear()
    BOT.chats.clear()


def parsed(entries, status=200, etag=None):
    return {"status": status, "entries": entries, "etag": etag, "modified": None, "bozo": 0}


class Parsed(dict):
    @property
    def entries(self):
        return self["entries"]


def entry(key, title, summary="", tags=()):
    return {"id": key, "title": title, "link": f"https://example.com/{key}", "summary": summary, "tags": [{"term": t} for t in tags]}


def add_feed(title="Feed", url="https://example.com/feed"):
    cursor = app.store.execute("INSERT INTO feeds(title, url) VALUES(?, ?)", (title, url))
    app.store.commit()
    return cursor.lastrowid


def feed_row(feed_id):
    return app.get_feed(feed_id)


def poll(feed_id, entries):
    app.process_feed(feed_row(feed_id), Parsed(parsed(entries)))
    run(app.deliver_pending(APP))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class HelperTests(unittest.TestCase):
    def test_comma_values_accepts_chinese_separators(self):
        self.assertEqual(app.comma_values("OpenAI，Claude、Gemini, AI ,,"), ["OpenAI", "Claude", "Gemini", "AI"])

    def test_split_message_respects_limit(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(500))
        chunks = app.split_message(text, limit=1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("\n".join(chunks).replace("\n", ""), text.replace("\n", ""))
        long_line = app.split_message("y" * 2500, limit=1000)
        self.assertEqual([len(c) for c in long_line], [1000, 1000, 500])
        self.assertEqual(app.split_message("hi"), ["hi"])

    def test_push_card_escapes_html(self):
        self.assertEqual(
            app.push_card("<b>A&B</b>", 'https://x.com/?a=1&b="2"'),
            '<a href="https://x.com/?a=1&amp;b=&quot;2&quot;"><b>&lt;b&gt;A&amp;B&lt;/b&gt;</b></a>',
        )
        self.assertEqual(app.push_card("T"), "<b>T</b>")

    def test_help_pages_are_valid_telegram_html(self):
        for key in ["home", *app.HELP_TOPICS]:
            text, markup = app.help_page(key)
            assert_telegram_html(text)
            self.assertNotIn("{", text)
            self.assertNotIn("}", text)
            buttons = [button for row in markup.inline_keyboard for button in row]
            for button in buttons:
                self.assertTrue(app.help_page(button.callback_data.partition(":")[2]))
                self.assertLessEqual(len(button.callback_data.encode()), 64)
        home_buttons = [b.callback_data for row in app.help_page("home")[1].inline_keyboard for b in row]
        self.assertEqual(sorted(home_buttons), sorted(f"help:{key}" for key in app.HELP_TOPICS))
        for alias, topic in app.HELP_ALIASES.items():
            self.assertIn(topic, app.HELP_TOPICS, alias)

    def test_commands_with_arguments_are_never_clickable(self):
        """Telegram would only send the bare command, so args must be inside <code>."""
        pages = [app.help_page(key)[0] for key in ["home", *app.HELP_TOPICS]]
        for page in pages:
            outside_code = re.sub(r"<code>.*?</code>", "", page)
            for match in re.finditer(r"(?<![\w/.])/([a-z]+)[ \t]+([A-Za-z0-9@\-][^\s]*)", outside_code):
                self.fail(f"clickable command with argument: {match.group(0)!r} in {page[:40]!r}")

    def test_command_menu_is_valid_for_telegram(self):
        for name, description in app.USER_COMMANDS + app.ADMIN_COMMANDS + app.GROUP_COMMANDS:
            self.assertRegex(name, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(1 <= len(description) <= 256)
            self.assertIn(name, app.COMMAND_HANDLERS)
        self.assertLessEqual(len(app.USER_COMMANDS + app.ADMIN_COMMANDS), 100)

    def test_every_command_mentioned_in_help_exists(self):
        help_text = app.html_to_plain(app.HELP_HOME + "".join(text for _, text in app.HELP_TOPICS.values()))
        for command in set(re.findall(r"(?<![\w/.])/([a-z]+)", help_text)):
            self.assertIn(command, app.COMMAND_HANDLERS, command)

    def test_register_commands(self):
        calls = []

        class MenuBot:
            async def set_my_commands(self, commands, scope=None):
                calls.append((type(scope).__name__, [c.command for c in commands]))

        run(app.register_commands(SimpleNamespace(bot=MenuBot())))
        scopes = [scope for scope, _ in calls]
        self.assertEqual(scopes, ["BotCommandScopeAllPrivateChats", "BotCommandScopeAllGroupChats", "BotCommandScopeChat"])
        self.assertIn("feedstatus", calls[2][1])
        self.assertNotIn("feedstatus", calls[0][1])

    def test_application_builds(self):
        original = app.TOKEN
        app.TOKEN = "123456:TEST"
        try:
            application = app.build_application()
            self.assertTrue(application.handlers[0])
        finally:
            app.TOKEN = original


# ---------------------------------------------------------------------------
# Matching and delivery
# ---------------------------------------------------------------------------

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.feed_id = add_feed()
        call(app.start, user_id=10)
        call(app.subscribe, user_id=10, args=[str(self.feed_id)])
        BOT.sent.clear()

    def test_first_fetch_is_baseline_only(self):
        poll(self.feed_id, [entry("a", "OpenAI news")])
        call(app.keywords, user_id=10, args=["OpenAI"])
        self.assertEqual(BOT.sent, [])
        poll(self.feed_id, [entry("a", "OpenAI news"), entry("b", "OpenAI again")])
        self.assertEqual(len(BOT.sent), 1)
        self.assertIn("OpenAI again", BOT.sent[0][1])

    def test_empty_first_response_does_not_initialize(self):
        app.process_feed(feed_row(self.feed_id), Parsed(parsed([])))
        self.assertEqual(feed_row(self.feed_id)["initialized"], 0)

    def test_keyword_combo_block_sections_and_full_push(self):
        call(app.keywords, user_id=10, args=["claude"])
        call(app.combo, user_id=10, args=["dmit,corona"])
        call(app.block, user_id=10, args=["求购"])
        poll(self.feed_id, [entry("base", "base", tags=["daily", "tech"])])
        poll(
            self.feed_id,
            [
                entry("1", "Claude release", tags=["tech"]),
                entry("2", "DMIT only", tags=["tech"]),
                entry("3", "dmit Corona deal", tags=["daily"]),
                entry("4", "求购 Claude", tags=["tech"]),
                entry("5", "nothing", tags=["tech"]),
            ],
        )
        self.assertEqual([t for _, t in BOT.sent], [app.push_card("Claude release", "https://example.com/1"), app.push_card("dmit Corona deal", "https://example.com/3")])

        tech_id = app.store.execute("SELECT id FROM sections WHERE name='tech'").fetchone()["id"]
        reply = call(app.sections, user_id=10, args=[str(self.feed_id), f"{tech_id},999"])
        self.assertIn("技术（tech）", reply)
        self.assertIn("999", reply)
        call(app.all_posts, user_id=10, args=[str(self.feed_id), "on"])
        BOT.sent.clear()
        poll(self.feed_id, [entry("6", "daily post", tags=["daily"]), entry("7", "tech post", tags=["tech"]), entry("8", "求购 tech", tags=["tech"])])
        self.assertEqual([t for _, t in BOT.sent], [app.push_card("tech post", "https://example.com/7")])

    def test_full_push_ignores_keywords(self):
        call(app.keywords, user_id=10, args=["Claude"])
        call(app.all_posts, user_id=10, args=[str(self.feed_id), "on"])
        poll(self.feed_id, [entry("base", "b")])
        poll(self.feed_id, [entry("1", "Claude news"), entry("2", "unrelated post")])
        self.assertEqual(len(BOT.sent), 2)  # keywords do not narrow a full-push feed
        call(app.all_posts, user_id=10, args=[str(self.feed_id), "off"])
        BOT.sent.clear()
        poll(self.feed_id, [entry("3", "Claude again"), entry("4", "another unrelated")])
        self.assertEqual([t for _, t in BOT.sent], [app.push_card("Claude again", "https://example.com/3")])

    def test_match_title_only_ignores_words_in_summary(self):
        """The reported case: a buying post whose body happens to contain 出."""
        call(app.combo, user_id=10, args=["出,golden"])
        call(app.combo, user_id=10, args=["出,rfc"])
        poll(self.feed_id, [entry("base", "b")])
        buying = entry("buy", "+200 收一个RFC JP T1 GOLDEN", summary="有出的老板私聊")
        selling = entry("sell", "出 GOLDEN JP 年付", summary="价格私聊")
        poll(self.feed_id, [buying, selling])
        self.assertEqual(len(BOT.sent), 2)  # default (title + summary): the buying post matches too
        self.assertIn("只看标题", call(app.match, user_id=10, args=["title"]))
        BOT.sent.clear()
        buying2 = entry("buy2", "+300 收一个RFC GOLDEN", summary="有出的私聊")
        selling2 = entry("sell2", "出 RFC 月付", summary="")
        poll(self.feed_id, [buying2, selling2])
        self.assertEqual([t for _, t in BOT.sent], [app.push_card("出 RFC 月付", "https://example.com/sell2")])
        call(app.match, user_id=10, args=["all"])
        BOT.sent.clear()
        poll(self.feed_id, [entry("buy3", "收 GOLDEN", summary="有出的私聊")])
        self.assertEqual(len(BOT.sent), 1)  # back to title + summary

    def test_match_title_keeps_blocked_words_on_summary(self):
        call(app.combo, user_id=10, args=["出,golden"])
        call(app.block, user_id=10, args=["求购"])
        call(app.match, user_id=10, args=["标题"])
        poll(self.feed_id, [entry("base", "b")])
        poll(self.feed_id, [entry("x", "出 golden", summary="顺便求购一台"), entry("y", "出 golden HK", summary="")])
        self.assertEqual([t for _, t in BOT.sent], [app.push_card("出 golden HK", "https://example.com/y")])

    def test_match_setting_is_per_user(self):
        call(app.start, user_id=11)
        call(app.subscribe, user_id=11, args=[str(self.feed_id)])
        for user_id in (10, 11):
            call(app.keywords, user_id=user_id, args=["golden"])
        call(app.match, user_id=10, args=["title"])
        poll(self.feed_id, [entry("base", "b")])
        poll(self.feed_id, [entry("x", "出售 VPS", summary="golden 线路")])
        self.assertEqual([chat for chat, _ in BOT.sent], ["11"])  # only the user still matching summaries

    def test_paused_user_gets_nothing(self):
        call(app.all_posts, user_id=10, args=[str(self.feed_id), "on"])
        call(app.pause, user_id=10)
        poll(self.feed_id, [entry("base", "b")])
        poll(self.feed_id, [entry("x", "new")])
        self.assertEqual(BOT.sent, [])

    def test_crash_recovery_does_not_drop_or_duplicate(self):
        call(app.all_posts, user_id=10, args=[str(self.feed_id), "on"])
        poll(self.feed_id, [entry("base", "b")])
        # Simulate a crash: the item is stored but delivery never ran.
        app.process_feed(feed_row(self.feed_id), Parsed(parsed([entry("x", "new")])))
        self.assertEqual(app.store.execute("SELECT COUNT(*) c FROM items WHERE pending=1").fetchone()["c"], 1)
        run(app.deliver_pending(APP))
        run(app.deliver_pending(APP))
        self.assertEqual(len(BOT.sent), 1)
        row = app.store.execute("SELECT pending, summary FROM items WHERE item_key='x'").fetchone()
        self.assertEqual((row["pending"], row["summary"]), (0, None))

    def test_partial_delivery_is_not_resent(self):
        call(app.all_posts, user_id=10, args=[str(self.feed_id), "on"])
        app.store.execute("INSERT INTO targets(user_id, chat_id, title, created_at) VALUES(10, '-100', 'G', 'now')")
        app.store.commit()
        poll(self.feed_id, [entry("base", "b")])
        app.process_feed(feed_row(self.feed_id), Parsed(parsed([entry("x", "new")])))
        item = app.store.execute("SELECT * FROM items WHERE item_key='x'").fetchone()
        first_target = app.store.execute("SELECT id FROM targets WHERE chat_id='10'").fetchone()["id"]
        app.record_delivery(10, item["id"], first_target, "全量推送", "sent", None)
        run(app.deliver_pending(APP))
        self.assertEqual(BOT.sent, [("-100", app.push_card("new", "https://example.com/x"))])

    def test_retry_after_and_forbidden(self):
        call(app.all_posts, user_id=10, args=[str(self.feed_id), "on"])
        poll(self.feed_id, [entry("base", "b")])
        BOT.fail["10"] = [RetryAfter(0)]
        poll(self.feed_id, [entry("x", "new")])
        self.assertEqual(len(BOT.sent), 1)
        BOT.fail["10"] = [Forbidden("bot was blocked by the user")]
        poll(self.feed_id, [entry("y", "newer")])
        history = call(app.history, user_id=10)
        self.assertIn("❌ 发送失败", history)
        self.assertIn("✅ 已发送", history)

    def test_deleted_feed_pending_items_are_cleared(self):
        poll(self.feed_id, [entry("base", "b")])
        app.process_feed(feed_row(self.feed_id), Parsed(parsed([entry("x", "new")])))
        call(app.delfeed, user_id=1, args=[str(self.feed_id)])
        run(app.deliver_pending(APP))
        self.assertEqual(app.store.execute("SELECT COUNT(*) c FROM items WHERE pending=1").fetchone()["c"], 0)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

class CommandTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.feed_id = add_feed("NodeSeek")

    def test_start_in_private_and_group(self):
        reply = call(app.start)
        self.assertIn("已把当前私聊设为推送目标", reply)
        # /start and /help must describe the same steps and warnings.
        for phrase in ("全部推送（不看关键词）", "默认是“关键词推送”"):
            self.assertIn(phrase, reply)
            self.assertIn(phrase, app.HELP_HOME)
        self.assertIn("当前私聊已经是推送目标", call(app.start))
        self.assertIn("请在和机器人的私聊里", call(app.start, chat_type="supergroup", chat_id=-100))

    def test_help_topics(self):
        message = FakeMessage()
        self.assertIn("第一次使用", call(app.help_command, message=message))
        self.assertIsNotNone(message.markups[-1])  # topic buttons are attached
        self.assertIn("组合关键词", call(app.help_command, args=["关键词"]))
        self.assertIn("组合关键词", call(app.help_command, args=["2"]))
        self.assertIn("最多 10 个", call(app.help_command, args=["目标"]))
        self.assertIn("同时满足", call(app.help_command, args=["规则"]))
        self.assertIn("第一次使用", call(app.help_command, args=["不存在"]))

    def test_help_buttons_switch_pages(self):
        edits = []

        class Query:
            from_user = SimpleNamespace(id=10)

            def __init__(self, data):
                self.data = data

            async def answer(self):
                pass

            async def edit_message_text(self, text, **kwargs):
                assert_telegram_html(text)
                edits.append((text, kwargs["reply_markup"]))

        for data in ["help:kw", "help:home", "help:unknown"]:
            run(app.help_button(SimpleNamespace(callback_query=Query(data)), SimpleNamespace(bot=BOT)))
        self.assertIn("关键词与屏蔽词", edits[0][0])
        self.assertEqual(edits[0][1].inline_keyboard[0][0].callback_data, "help:home")
        self.assertIn("第一次使用", edits[1][0])
        self.assertIn("第一次使用", edits[2][0])

    def test_subscribe_flow_messages(self):
        reply = call(app.subscribe, args=[str(self.feed_id)])
        self.assertIn("关键词推送（默认）", reply)
        self.assertIn("还没有关键词", reply)
        self.assertIn("已经订阅", call(app.subscribe, args=[str(self.feed_id)]))
        self.assertIn("没有可以订阅的 99 号源", call(app.subscribe, args=["99"]))
        self.assertIn("<code>/subscribe 1</code>", call(app.subscribe, args=["abc"]))
        self.assertIn("✅ 已订阅", call(app.feeds))
        self.assertIn("关键词推送", call(app.my_sources))

    def test_status_reports_problems(self):
        reply = call(app.status)
        self.assertIn("还没有订阅任何源", reply)
        self.assertIn("没有推送目标", reply)
        call(app.start)
        call(app.subscribe, args=[str(self.feed_id)])
        self.assertIn("不会推送任何文章", call(app.status))
        call(app.keywords, args=["Claude"])
        self.assertIn("✅ 设置完整", call(app.status))
        call(app.pause)
        self.assertIn("推送已暂停", call(app.status))

    def test_keywords_ids_dedupe_and_warning(self):
        reply = call(app.keywords, args=["OpenAI，AI、openai"])
        self.assertIn("1. OpenAI", reply)
        self.assertIn("2. AI", reply)
        self.assertNotIn("3.", reply)
        self.assertIn("容易误命中", reply)
        self.assertIn("之前已经有了", call(app.keywords, args=["ai"]))
        listing = call(app.keywords)
        self.assertIn("1. 普通：OpenAI  ✅ 启用中", listing)
        self.assertIn("<code>/kwoff 1</code>", listing)

    def test_rule_state_and_delete_report_missing_ids(self):
        call(app.keywords, args=["Claude"])
        self.assertIn("已暂停 1 号关键词", call(app.kwoff, args=["1"]))
        self.assertIn("⏸ 已暂停", call(app.keywords))
        self.assertIn("已启用 1 号关键词", call(app.kwon, args=["1"]))
        self.assertIn("没有找到你的 9 号关键词", call(app.kwoff, args=["9"]))
        self.assertIn("<code>/all 1 off</code>", call(app.kwoff, args=["1", "off"]))
        self.assertIn("没有找到", call(app.kwon, user_id=11, args=["1"]))  # other users' rules are invisible
        self.assertIn("已删除 1 号关键词：Claude", call(app.delkw, args=["1"]))
        self.assertIn("没有找到", call(app.delkw, args=["1"]))

    def test_keyword_replies_warn_about_full_push_feeds(self):
        call(app.subscribe, args=[str(self.feed_id)])
        self.assertNotIn("不看关键词", call(app.keywords, args=["Claude"]))
        reply = call(app.all_posts, args=[str(self.feed_id), "on"])
        self.assertIn("不再看关键词", reply)
        self.assertIn(f"<code>/all {self.feed_id} off</code>", reply)
        for reply in (
            call(app.keywords, args=["Gemini"]),
            call(app.keywords),
            call(app.combo, args=["a,b"]),
        ):
            self.assertIn(f"{self.feed_id} 号源是全量推送，不看关键词", reply)
        self.assertIn("全量推送（不看关键词）", call(app.my_sources))
        self.assertIn("全量推送（不看关键词）", call(app.status))
        self.assertIn("全量推送（不看关键词）", call(app.all_posts))
        help_kw = call(app.help_command, args=["关键词"])
        self.assertIn("关键词只对“关键词推送”的源生效", help_kw)
        self.assertNotIn("关键词和屏蔽词对你订阅的", help_kw)

    def test_match_command_replies(self):
        reply = call(app.match)
        self.assertIn("关键词匹配范围</b>：标题 + 摘要", reply)
        self.assertIn("<code>/match title</code>", reply)
        self.assertIn("只看标题", call(app.match, args=["TITLE"]))
        self.assertIn("关键词匹配范围</b>：只看标题", call(app.match, args=["乱写"]))  # unknown -> show usage
        self.assertIn("匹配范围：只看标题", call(app.status))
        call(app.keywords, args=["x1"])
        self.assertIn("匹配范围：只看标题（/match 修改）", call(app.keywords))
        self.assertIn("标题 + 摘要", call(app.match, args=["all"]))
        self.assertIn("/match title", app.help_page("kw")[0])

    def test_combo(self):
        self.assertIn("至少要两个", call(app.combo, args=["a,A"]))
        self.assertIn("dmit + corona", call(app.combo, args=["dmit,corona"]))
        self.assertIn("已经有了", call(app.combo, args=["Corona,DMIT"]))

    def test_blocks(self):
        self.assertIn("1. 求购", call(app.block, args=["求购,已出"]))
        self.assertIn("之前已经有了", call(app.block, args=["求购"]))
        self.assertIn("1. 求购", call(app.blocks))
        self.assertIn("已删除 1 号屏蔽词", call(app.delblock, args=["1"]))
        self.assertIn("没有找到", call(app.delblock, args=["1"]))

    def test_sections_validation(self):
        app.store.execute("INSERT INTO sections(feed_id, name) VALUES(?, 'daily'), (?, 'tech')", (self.feed_id, self.feed_id))
        other = add_feed("Other", "https://example.com/other")
        app.store.execute("INSERT INTO sections(feed_id, name) VALUES(?, 'misc')", (other,))
        app.store.commit()
        self.assertIn("请先订阅", call(app.sections, args=[str(self.feed_id), "1"]))
        call(app.subscribe, args=[str(self.feed_id)])
        self.assertIn("当前：推送全部版块", call(app.sections, args=[str(self.feed_id)]))
        call(app.sections, args=[str(self.feed_id), "1"])
        reply = call(app.sections, args=[str(self.feed_id), "3,abc"])
        self.assertIn("原来的设置没有改动", reply)
        self.assertIn("日常（daily）  ✅", call(app.sections, args=[str(self.feed_id)]))
        self.assertIn("只看 日常（daily）", call(app.my_sources))
        self.assertIn("已恢复为推送全部版块", call(app.clear_sections, args=[str(self.feed_id)]))
        self.assertIn("本来就是推送全部版块", call(app.clear_sections, args=[str(self.feed_id)]))

    def test_unsubscribe_clears_sections(self):
        app.store.execute("INSERT INTO sections(feed_id, name) VALUES(?, 'daily')", (self.feed_id,))
        app.store.commit()
        call(app.subscribe, args=[str(self.feed_id)])
        call(app.sections, args=[str(self.feed_id), "1"])
        self.assertIn("你没有订阅 5 号源", call(app.unsubscribe, args=["5"]))
        self.assertIn("已取消订阅", call(app.unsubscribe, args=[str(self.feed_id)]))
        self.assertEqual(app.store.execute("SELECT COUNT(*) c FROM selected_sections").fetchone()["c"], 0)

    def test_all_posts(self):
        self.assertIn("你没有订阅 1 号源", call(app.all_posts, args=[str(self.feed_id), "on"]))
        call(app.subscribe, args=[str(self.feed_id)])
        self.assertIn("<code>/all 1 on</code>", call(app.all_posts, args=[str(self.feed_id), "yes"]))
        self.assertIn("已改为全量推送", call(app.all_posts, args=[str(self.feed_id), "on"]))
        self.assertIn("✅ 全量推送", call(app.all_posts))
        self.assertIn("没有启用的关键词", call(app.all_posts, args=[str(self.feed_id), "off"]))

    def test_targets_group_and_channel(self):
        call(app.start)
        # Group: non-admin is refused, admin succeeds.
        self.assertIn("只有群管理员", call(app.addtarget, chat_type="supergroup", chat_id=-200, chat_title="G"))
        BOT.members[("-200", 10)] = SimpleNamespace(status="administrator")
        BOT.members[("-200", BOT.id)] = SimpleNamespace(status="member")
        self.assertIn("已添加推送目标：G", call(app.addtarget, chat_type="supergroup", chat_id=-200, chat_title="G"))
        # Channel bound from private chat stores the channel's real title and id.
        BOT.chats["-1001"] = SimpleNamespace(id=-1001, type="channel", title="My Channel", username="mychan")
        self.assertIn("找不到", call(app.addtarget, args=["-1002"]))
        self.assertIn("无法确认你是", call(app.addtarget, args=["-1001"]))
        BOT.members[("-1001", 10)] = SimpleNamespace(status="creator")
        BOT.members[("-1001", BOT.id)] = SimpleNamespace(status="administrator", can_post_messages=False)
        self.assertIn("没有发消息的权限", call(app.addtarget, args=["-1001"]))
        BOT.members[("-1001", BOT.id)] = SimpleNamespace(status="administrator", can_post_messages=True)
        self.assertIn("My Channel（chat_id：-1001）", call(app.addtarget, args=["-1001"]))
        listing = call(app.targets)
        self.assertIn("My Channel", listing)
        self.assertIn("3/10", listing)
        self.assertIn("已删除 3 号推送目标：My Channel", call(app.deltarget, args=["3"]))
        self.assertIn("没有找到", call(app.deltarget, args=["3"]))

    def test_forwarded_channel_message_reveals_chat_id(self):
        origin = MessageOriginChannel(
            date=__import__("datetime").datetime.now(),
            chat=__import__("telegram").Chat(id=-1009, type="channel", title="News"),
            message_id=1,
        )
        reply = call(app.forwarded_message, message=FakeMessage("hi", forward_origin=origin))
        self.assertIn("<code>/addtarget -1009</code>", reply)
        self.assertIn("不是从频道转发来的", call(app.forwarded_message, message=FakeMessage("hi")))

    def test_admin_commands(self):
        self.assertIn("只有管理员", call(app.delfeed, args=[str(self.feed_id)]))
        self.assertIn("只有管理员", call(app.feedstatus))
        self.assertIn("只有管理员", call(app.addfeed, args=["https://example.com"]))
        self.assertIn("已经添加过了", call(app.addfeed, user_id=1, args=["https://example.com/feed"]))
        self.assertIn("NodeSeek", call(app.feedstatus, user_id=1))
        self.assertIn("没有 99 号源", call(app.delfeed, user_id=1, args=["99"]))
        self.assertIn("已删除 1 号源", call(app.delfeed, user_id=1, args=[str(self.feed_id)]))

    def test_whitelist_is_silent_in_groups(self):
        app.ALLOWED_USER_IDS.add(1)
        try:
            self.assertIn("不在机器人的允许名单内", call(app.feeds, user_id=55))
            self.assertEqual(call(app.feeds, user_id=55, chat_type="group", chat_id=-5), "")
        finally:
            app.ALLOWED_USER_IDS.clear()

    def test_special_characters_never_break_html(self):
        nasty = '<b>&"Tom & Jerry"</b> <script>'
        app.store.execute("UPDATE feeds SET title=?, last_error=? WHERE id=?", (nasty, nasty, self.feed_id))
        app.store.execute("INSERT INTO sections(feed_id, name) VALUES(?, ?)", (self.feed_id, nasty))
        app.store.commit()
        BOT.chats["-1001"] = SimpleNamespace(id=-1001, type="channel", title=nasty, username=None)
        BOT.members[("-1001", 10)] = SimpleNamespace(status="creator")
        BOT.members[("-1001", BOT.id)] = SimpleNamespace(status="administrator", can_post_messages=True)
        fid = str(self.feed_id)
        steps = [
            (app.start, []), (app.feeds, []), (app.subscribe, [fid]), (app.my_sources, []), (app.all_posts, []),
            (app.all_posts, [fid, "on"]), (app.all_posts, [fid, "off"]), (app.keywords, ["<x>,a&b,Tom & Jerry"]),
            (app.keywords, []), (app.combo, ["<a>,&b"]), (app.kwoff, ["1"]), (app.kwon, ["1"]), (app.block, ["<i>,&"]),
            (app.blocks, []), (app.sections, [fid]), (app.sections, [fid, "1"]), (app.sections, [fid, "<9>"]),
            (app.addtarget, ["-1001"]), (app.addtarget, ["<bad>"]), (app.targets, []), (app.status, []),
            (app.history, []), (app.feedstatus, []), (app.whoami, []), (app.delkw, ["1"]), (app.delblock, ["1"]),
            (app.unsubscribe, [fid]), (app.delfeed, [fid]),
        ]
        for handler, args in steps:
            call(handler, user_id=10 if handler is not app.feedstatus and handler is not app.delfeed else 1, args=args)
        call(app.unknown_command, message=FakeMessage("/<b>&"))
        call(app.whoami, chat_type="supergroup", chat_id=-5, chat_title=nasty)

    def test_respond_falls_back_to_plain_text(self):
        class StrictMessage(FakeMessage):
            async def reply_text(self, text, **kwargs):
                if kwargs.get("parse_mode") == "HTML":
                    from telegram.error import BadRequest

                    raise BadRequest("Can't parse entities")
                self.replies.append(text)

        message = StrictMessage()
        update = SimpleNamespace(effective_message=message)
        run(app.respond(update, "<b>粗体</b> &amp; <code>/x 1</code>"))
        self.assertEqual(message.replies, ["粗体 & /x 1"])

    def test_history_long_output_is_split(self):
        call(app.start)
        feed = self.feed_id
        for i in range(50):
            cursor = app.store.execute(
                "INSERT INTO items(feed_id,item_key,title,link,first_seen) VALUES(?,?,?,?,?)",
                (feed, str(i), "标题" * 40, "https://example.com/" + "p" * 80, app.now()),
            )
            app.record_delivery(10, cursor.lastrowid, 1, "全量推送", "sent", None)
        message = FakeMessage()
        call(app.history, args=["50"], message=message)
        self.assertGreater(len(message.replies), 1)
        self.assertTrue(all(len(r) <= 4096 for r in message.replies))


class RenumberTests(unittest.TestCase):
    ADMIN = 1

    def setUp(self):
        reset_db()

    def ids(self, table, user_id=None):
        sql = f"SELECT id FROM {table}" + (" WHERE user_id=?" if user_id else "") + " ORDER BY id"
        return [r[0] for r in app.store.execute(sql, (user_id,) if user_id else ())]

    def press(self, data, user_id=ADMIN):
        edits = []

        class Query:
            from_user = SimpleNamespace(id=user_id)

            def __init__(self):
                self.data = data

            async def answer(self):
                pass

            async def edit_message_text(self, text, **kwargs):
                assert_telegram_html(text)
                assert_no_clickable_command_with_args(text)
                edits.append(text)

        run(app.renumber_button(SimpleNamespace(callback_query=Query()), SimpleNamespace(bot=BOT)))
        return edits[-1] if edits else None

    def test_users_situation_1_to_5_and_7(self):
        """Keyword 6 was deleted: 1,2,3,4,5,7 becomes 1..6 and the next one is 7."""
        for i in range(1, 8):
            call(app.combo, user_id=self.ADMIN, args=[f"出,w{i}"])
        call(app.delkw, user_id=self.ADMIN, args=["6"])
        self.assertEqual(self.ids("rules"), [1, 2, 3, 4, 5, 7])
        preview = call(app.renumber, user_id=self.ADMIN)
        self.assertIn("7 → 6", preview)
        self.assertNotIn("1 → 1", preview)
        self.assertEqual(self.ids("rules"), [1, 2, 3, 4, 5, 7])  # preview changes nothing
        result = self.press("renumber:yes")
        self.assertIn("编号已整理", result)
        self.assertIn("下一个新关键词的编号是 7", result)
        self.assertEqual(self.ids("rules"), [1, 2, 3, 4, 5, 6])
        listing = call(app.keywords, user_id=self.ADMIN)
        self.assertIn("6. 组合：出 + w7", listing)
        for page in (app.help_page("kw")[0], app.help_page("admin")[0]):
            self.assertIn("/renumber", page)
            self.assertIn("不会补位", page)
        self.assertIn("已添加组合关键词 7：出 + w8", call(app.combo, user_id=self.ADMIN, args=["出,w8"]))

    def test_existing_commands_and_matching_still_work(self):
        feed_id = add_feed()
        call(app.start, user_id=self.ADMIN)
        call(app.subscribe, user_id=self.ADMIN, args=[str(feed_id)])
        call(app.keywords, user_id=self.ADMIN, args=["alpha,beta,gamma"])
        call(app.delkw, user_id=self.ADMIN, args=["2"])
        call(app.kwoff, user_id=self.ADMIN, args=["3"])  # gamma paused before renumbering
        self.press("renumber:yes")
        listing = call(app.keywords, user_id=self.ADMIN)
        self.assertIn("1. 普通：alpha  ✅ 启用中", listing)
        self.assertIn("2. 普通：gamma  ⏸ 已暂停", listing)  # state survives renumbering
        self.assertIn("已启用 2 号关键词：gamma", call(app.kwon, user_id=self.ADMIN, args=["2"]))
        poll(feed_id, [entry("base", "b")])
        poll(feed_id, [entry("1", "alpha news"), entry("2", "beta news"), entry("3", "gamma news")])
        self.assertEqual(
            [t for _, t in BOT.sent],
            [app.push_card("alpha news", "https://example.com/1"), app.push_card("gamma news", "https://example.com/3")],
        )
        self.assertIn("已删除 1 号关键词：alpha", call(app.delkw, user_id=self.ADMIN, args=["1"]))

    def test_multiple_users_keep_order_and_privacy(self):
        call(app.keywords, user_id=self.ADMIN, args=["a1"])   # 1
        call(app.keywords, user_id=20, args=["x1"])           # 2
        call(app.keywords, user_id=self.ADMIN, args=["a2"])   # 3
        call(app.keywords, user_id=20, args=["x2"])           # 4
        call(app.delkw, user_id=self.ADMIN, args=["1"])
        preview = call(app.renumber, user_id=self.ADMIN)
        self.assertIn("3 → 2", preview)
        self.assertIn("其他用户：共有 2 条关键词的编号会改变", preview)
        self.assertNotIn("x1", preview)  # other users' keywords are never shown
        self.press("renumber:yes")
        self.assertEqual(self.ids("rules", self.ADMIN), [2])
        self.assertEqual(self.ids("rules", 20), [1, 3])
        self.assertIn("1. 普通：x1", call(app.keywords, user_id=20))
        self.assertIn("没有找到你的 1 号关键词", call(app.delkw, user_id=self.ADMIN, args=["1"]))  # still x1's

    def test_blocked_words_too(self):
        call(app.block, user_id=self.ADMIN, args=["b1,b2,b3"])
        call(app.delblock, user_id=self.ADMIN, args=["1"])
        self.press("renumber:yes")
        self.assertEqual(self.ids("blocked_words"), [1, 2])
        self.assertIn("1. b2", call(app.blocks, user_id=self.ADMIN))
        self.assertIn("3. b4", call(app.block, user_id=self.ADMIN, args=["b4"]))

    def test_deleted_last_items_reset_the_counter(self):
        call(app.keywords, user_id=self.ADMIN, args=["a,b,c"])
        for rule_id in ("1", "2", "3"):
            call(app.delkw, user_id=self.ADMIN, args=[rule_id])
        self.assertTrue(app.renumber_needed())  # nothing left, but next ID would be 4
        self.press("renumber:yes")
        self.assertIn("1. new", call(app.keywords, user_id=self.ADMIN, args=["new"]))

    def test_permissions_cancel_and_nothing_to_do(self):
        call(app.keywords, user_id=10, args=["a,b"])
        call(app.delkw, user_id=10, args=["1"])
        self.assertIn("只有管理员", call(app.renumber, user_id=10))
        self.assertIsNone(self.press("renumber:yes", user_id=10))  # non-admin button press is ignored
        self.assertEqual(self.ids("rules"), [2])
        self.assertIn("已取消", self.press("renumber:no"))
        self.assertEqual(self.ids("rules"), [2])
        self.press("renumber:yes")
        self.assertIn("不需要整理", call(app.renumber, user_id=self.ADMIN))
        self.assertIn("不需要整理", self.press("renumber:yes"))

    def test_failure_rolls_back_everything(self):
        call(app.keywords, user_id=self.ADMIN, args=["a,b,c"])
        call(app.block, user_id=self.ADMIN, args=["x,y"])
        call(app.delkw, user_id=self.ADMIN, args=["1"])
        call(app.delblock, user_id=self.ADMIN, args=["1"])
        original = app.store.execute

        def failing(sql, params=()):
            if "blocked_words SET id" in sql:
                raise sqlite3.OperationalError("simulated failure")
            return original(sql, params)

        app.store.execute = failing
        try:
            with self.assertRaises(sqlite3.OperationalError):
                app.renumber_all()
        finally:
            app.store.execute = original
        self.assertEqual(self.ids("rules"), [2, 3])  # the keyword step was rolled back too
        self.assertEqual(self.ids("blocked_words"), [2])
        self.assertEqual(app.next_id("rules"), 4)

    def test_cli(self):
        import contextlib
        import io

        call(app.keywords, user_id=self.ADMIN, args=["a,b"])
        call(app.delkw, user_id=self.ADMIN, args=["1"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.cli_renumber()
        self.assertIn("2 → 1", out.getvalue())
        self.assertIn("下一个新关键词的编号：2", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.cli_renumber()
        self.assertIn("不需要整理", out.getvalue())


# ---------------------------------------------------------------------------
# Real HTTP fetching (local server) and schema migration
# ---------------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Local</title>
<item><guid>1</guid><title>Hello</title><link>http://x/1</link><category>tech</category></item>
</channel></rss>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            threading.Event().wait(3)
        if self.path == "/broken":
            self.send_response(500)
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("ETag", '"v1"')
        self.end_headers()
        self.wfile.write(RSS)

    def log_message(self, *args):
        pass


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        reset_db()

    def test_etag_and_304(self):
        result = app.fetch_feed(self.base + "/feed")
        self.assertEqual(result.get("status"), 200)
        self.assertEqual(result.get("etag"), '"v1"')
        self.assertEqual(app.fetch_feed(self.base + "/feed", etag='"v1"').get("status"), 304)

    def test_poll_once_end_to_end(self):
        feed_id = add_feed("Local", self.base + "/feed")
        broken = add_feed("Broken", self.base + "/broken")
        run(app.poll_once(APP))
        row = feed_row(feed_id)
        self.assertEqual((row["initialized"], row["etag"], row["last_error"]), (1, '"v1"', None))
        self.assertEqual(feed_row(broken)["last_error"], "HTTP 500")
        self.assertEqual(feed_row(broken)["initialized"], 0)
        run(app.poll_once(APP))  # 304 path
        self.assertIsNone(feed_row(feed_id)["last_error"])

    def test_timeout_is_enforced(self):
        socket_timeout = __import__("socket").getdefaulttimeout()
        __import__("socket").setdefaulttimeout(1)
        try:
            with self.assertRaises(Exception):
                app.fetch_feed(self.base + "/slow")
        finally:
            __import__("socket").setdefaulttimeout(socket_timeout)

    def test_addfeed_with_real_fetch(self):
        reply = call(app.addfeed, user_id=1, args=[self.base + "/feed2"])
        self.assertIn("已添加 1 号源「Local」", reply)
        self.assertIn("无法读取", call(app.addfeed, user_id=1, args=[self.base + "/broken"]))


OLD_SCHEMA = """
CREATE TABLE users (user_id INTEGER PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE feeds (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1, initialized INTEGER NOT NULL DEFAULT 0, last_checked TEXT, last_error TEXT);
CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER NOT NULL, item_key TEXT NOT NULL,
  title TEXT NOT NULL, link TEXT, summary TEXT, categories TEXT, first_seen TEXT NOT NULL, UNIQUE(feed_id, item_key));
INSERT INTO feeds(title, url, initialized) VALUES('Old', 'https://old/feed', 1);
INSERT INTO users(user_id, paused, created_at) VALUES(7, 0, 'x');
INSERT INTO items(feed_id, item_key, title, summary, first_seen) VALUES(1, 'k', 'T', 'long summary', '2025-01-01T00:00:00+00:00');
"""


class MigrationTests(unittest.TestCase):
    def test_old_database_is_upgraded_in_place(self):
        path = Path(TMP) / "old.db"
        connection = sqlite3.connect(path)
        connection.executescript(OLD_SCHEMA)
        connection.commit()
        connection.close()
        store = app.Store(path)
        self.assertIn("etag", store._columns("feeds"))
        self.assertIn("pending", store._columns("items"))
        item = store.execute("SELECT * FROM items").fetchone()
        self.assertEqual((item["title"], item["pending"], item["summary"]), ("T", 0, None))
        self.assertEqual(store.execute("SELECT initialized FROM feeds").fetchone()["initialized"], 1)
        self.assertIn("match_scope", store._columns("users"))
        self.assertEqual(store.execute("SELECT match_scope FROM users WHERE user_id=7").fetchone()[0], "all")
        store.connection.close()
        app.Store(path).connection.close()  # idempotent


if __name__ == "__main__":
    unittest.main()
