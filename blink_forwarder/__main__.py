"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

import aiohttp

from . import __version__
from .config import ConfigError, Settings, load_settings
from .db import StateDB
from .forwarder import run
from .telegram import TelegramClient


async def check(settings: Settings) -> int:
    """Validate configuration, Telegram access, and local state."""
    print(f"Configuration loaded; timezone={settings.tz_name}")
    print(f"Data directory: {settings.db_path.parent}")
    print(f"Archive:        {settings.archive_dir}")
    print(f"Blink login:    {settings.blink_login_file}")

    problems = 0

    try:
        settings.ensure_dirs()
        probe = settings.tmp_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print("Data directory: writable")
    except OSError as exc:
        print(f"Data directory: NOT writable ({exc})")
        problems += 1

    if settings.blink_login_file.is_file():
        mode = oct(settings.blink_login_file.stat().st_mode & 0o777)
        print(f"Blink login:    present (mode {mode})")
        if settings.blink_login_file.stat().st_mode & 0o077:
            print("Blink login:    WARNING - should be mode 600 (chmod 600)")
    else:
        print(
            "Blink login:    MISSING - run `blink-login` "
            "(local: `.venv/bin/python scripts/blink_login.py`)"
        )
        problems += 1

    session = aiohttp.ClientSession()
    telegram = TelegramClient(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        session,
        silent=settings.silent,
    )
    try:
        me = await telegram.get_me()
        bot_id = int(me["id"])
        print(f"Telegram bot:   @{me.get('username', '?')} (id {bot_id})")
        chat = await telegram.get_chat()
        title = chat.get("title") or chat.get("username") or chat.get("id")
        chat_type = str(chat.get("type", "?"))
        print(f"Telegram chat:  {title} ({chat_type}, id {chat.get('id')})")

        member = await telegram.get_chat_member(bot_id)
        status = str(member.get("status", "?"))
        can_post = bool(member.get("can_post_messages", False))
        can_send = bool(member.get("can_send_messages", False))
        rights = {
            key: value
            for key, value in member.items()
            if key.startswith("can_") or key in {"status", "is_anonymous"}
        }
        print(f"Bot status:     {status}")
        print(f"Bot rights:     {rights}")

        if chat_type == "channel":
            if status in {"administrator", "creator"} and can_post:
                print("Bot access:     OK - the bot can post videos here")
            elif status in {"administrator", "creator"}:
                print(
                    "Bot access:     WARNING - the bot is an admin, but the "
                    "'Post Messages' permission is off.\n"
                    "                Open the channel -> Administrators -> "
                    "select the bot -> enable 'Post Messages' -> Save."
                )
                problems += 1
            else:
                print(
                    f"Bot access:     WARNING - the bot is '{status}', not an "
                    "administrator of this channel.\n"
                    "                Add it as an administrator with "
                    "'Post Messages' enabled."
                )
                problems += 1
        else:
            print(
                f"Bot access:     NOTE - this chat is a {chat_type}, not a "
                "channel.\n"
                "                can_post_messages only exists for channels; "
                "for a group the bot posts as an administrator, so this is "
                "expected.\n"
                "                If you intended to post to a channel, use the "
                "channel's -100... id."
            )
            if status in {"left", "kicked"}:
                problems += 1
    except Exception as exc:
        print(f"Telegram:       FAILED - {exc}")
        print(
            "                Check the bot token, add the bot to the channel "
            "as an admin, and verify the chat id."
        )
        problems += 1
    finally:
        await session.close()

    try:
        db = StateDB(settings.db_path)
        stats = db.stats()
        db.close()
        print(
            f"State DB:       {settings.db_path} "
            f"({stats.sent} sent, {stats.skipped} skipped)"
        )
    except Exception as exc:
        print(f"State DB:       FAILED - {exc}")
        problems += 1

    if problems:
        print(f"\n{problems} problem(s) found.")
        print(
            "For raw Telegram chat/member details, run:\n"
            "  python scripts/telegram_diag.py"
        )
        return 2
    print("\nConfiguration looks good.")
    return 0


def show_stats(settings: Settings) -> int:
    if not settings.db_path.is_file():
        print("No state database yet; nothing has been forwarded.")
        return 0
    db = StateDB(settings.db_path)
    stats = db.stats()
    db.close()
    print(f"Sent:     {stats.sent}")
    print(f"Skipped:  {stats.skipped}")
    print(f"Last run: {stats.last_processed_at or 'never'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blink-forwarder",
        description="Forward Blink local-storage clips to a Telegram channel.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config, Telegram access, and the saved Blink login",
    )
    parser.add_argument(
        "--stats", action="store_true", help="show forwarding counters and exit"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list clips that would be sent without downloading or posting",
    )
    parser.add_argument(
        "--max-clips",
        type=int,
        metavar="N",
        help="override MAX_CLIPS_PER_RUN for this run",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    try:
        settings = load_settings(
            dry_run=True if args.dry_run else None,
            max_clips=args.max_clips,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3

    if args.stats:
        return show_stats(settings)
    if args.check:
        return asyncio.run(check(settings))

    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
