# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-user Telegram bot that polls RSS feeds and pushes new articles whose **title** matches a user's keywords. It uses Python 3.10+, python-telegram-bot 22.8, feedparser and SQLite. All the code is in `app.py`, and all tests are in `tests/test_app.py`. Every user-facing text is in Simplified Chinese; keep new strings in that style. Production runs on an Ubuntu VPS at `/opt/rss-tg-bot` as the systemd unit `rss-tg-bot.service`.

## Commands

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v                               # full offline suite (no network, no token)
python -m unittest tests.test_app.DeliveryTests.test_keywords_match_title_only -v   # single test
python app.py                                                          # run the bot (needs TG_BOT_TOKEN in .env)
python app.py renumber                                                 # CLI: compact rule/blocked-word IDs
shellcheck scripts/*.sh scripts/rss-tg-bot                             # scripts are kept shellcheck-clean
```

`tests/test_app.py` sets its environment before it imports `app`: a temp `DB_PATH`, `ADMIN_USER_IDS=1`, and so on. `scripts/update.sh` runs the same suite on the server as a self-test, and the server has a real `.env`. Any new env-driven setting must be pinned in that `os.environ.update(...)` block. Otherwise the server's `.env` can change test results and block updates.

## Architecture (app.py)

- **Config.** Configuration comes from env vars and `.env`, which are read at import time (`env_int`, `parse_ids`). A module-level `store = Store(DB_PATH)` is created on import.
- **`Store`.** It creates the schema with `CREATE TABLE IF NOT EXISTS`. Migrations live in `_migrate()`: they add columns via `PRAGMA table_info` plus `ALTER TABLE`, and create indexes. Schema changes must go there as additive migrations, because existing production DBs have to keep working.
- **Polling pipeline.** `post_init` starts `poll_loop`, and `post_shutdown` cancels it. Each round calls `poll_once`, which runs these steps:
  1. Fetches feeds concurrently via `asyncio.to_thread(fetch_feed)`. A semaphore limits concurrency to `FETCH_CONCURRENCY`. Requests send ETag/Last-Modified, and a 304 counts as success.
  2. `process_feed` inserts new items. The first successful fetch only builds a baseline (`initialized`) and pushes nothing. Later fetches insert items with `pending=1`.
  3. `deliver_pending` → `deliver_item`.
  4. `prune_history`.
- **Durable delivery.** `items.pending` is the delivery queue. Delivery rows are upserted on `UNIQUE(item_id, target_id)`, and targets already `sent` are skipped. As a result, a crash or restart neither drops nor duplicates a push.
- **Matching rules in `deliver_item`.** These semantics were chosen by the user; keep them unless the user asks otherwise:
  - If the user selected sections for the feed, the item's categories must intersect them.
  - Blocked words (`is_blocked`) check title + summary.
  - Keywords (`matched_rules`) check **the title only**. A `word` rule matches when any of its comma-separated values appears; a `combo` rule needs all of them. Matching is case-insensitive.
  - A full-push subscription (`/all`, the `full_pushes` table) ignores keywords entirely. Sections and blocked words still apply.
- **Sending.** `send_with_retry` handles `RetryAfter`, `ChatMigrated` (it rewrites the target's `chat_id`) and `NetworkError` backoff.
- **Telegram HTML convention.** All replies use HTML parse mode through `respond()`, which splits long messages and falls back to plain text on `BadRequest`.
  - Every dynamic value must go through `e()`.
  - An example command with arguments must go through `c()` (`<code>`). Telegram only makes the bare `/cmd` clickable, so a clickable `/cmd arg` would send the wrong thing.
  - The tests enforce both rules: `FakeMessage` runs `assert_telegram_html` and `assert_no_clickable_command_with_args` on every reply.
- **Adding a command touches several places that must stay in sync:**
  - the handler function and its `COMMAND_HANDLERS` entry;
  - the menu lists `USER_COMMANDS` / `ADMIN_COMMANDS` / `GROUP_COMMANDS`, which are registered per scope in `register_commands`;
  - the relevant `HELP_TOPICS` page (plus `HELP_ALIASES` if needed);
  - `README.md` and `docs/USAGE.zh-CN.md`.

  The user has asked to avoid adding new commands unless explicitly requested.
- **IDs.** Tables use `AUTOINCREMENT`, so IDs are never reused. `renumber_all()` compacts the `rules` and `blocked_words` IDs atomically: it swaps through temporary negative IDs and then resets `sqlite_sequence`. It is exposed as the admin-only `/renumber`, which shows a preview and `renumber:yes/no` buttons, and as `python app.py renumber`.
- **Logging.** The `httpx`/`httpcore` loggers are forced to WARNING, because their INFO lines contain the bot token in the URL. Do not lower that level.

## Deployment scripts

- `scripts/install.sh` is the one-click installer and is designed to work via `bash <(curl …)`. It reads prompts from `/dev/tty`, and if the bot is already installed it hands off to `update.sh`.
- `scripts/rss-tg-bot` is the management CLI, symlinked to `/usr/local/bin/rss-tg-bot`. Its subcommands are `status`, `logs`, `restart`, `update`, `backup`, `config`, `admin`, `allow`, `renumber` and `uninstall`.
- `scripts/update.sh` updates the server in this order:
  1. Backs up.
  2. Pulls with `fetch` + `merge --ff-only FETCH_HEAD`. It refuses to run if tracked files were modified locally.
  3. Installs dependencies with pip.
  4. Runs the unittest self-test. If that fails, it does **not** restart.
  5. Reinstalls the systemd unit and the CLI symlink.
  6. Restarts the service.

  The update must keep working on old git (Ubuntu 22.04). That is why `safe.directory` is written to the system git config instead of relying on `-c safe.directory`.
- `systemd/rss-tg-bot.service` runs as root with sandboxing (`ProtectSystem=strict`, `ReadWritePaths=/opt/rss-tg-bot`). Anything that needs to write outside the app directory needs a unit change.
- The user deploys by merging to `main` and running `rss-tg-bot update` on the VPS. Changes must therefore pass the test suite, because the server's self-test gates the restart.
