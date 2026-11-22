#!/usr/bin/env python3
"""Print raw Telegram chat/admin diagnostics.

This never prints the bot token. Use it when ``blink-forwarder --check`` says
the bot cannot post:

    .venv/bin/python scripts/telegram_diag.py
"""

from __future__ import annotations

import asyncio
import argparse
import json

import aiohttp

from blink_forwarder.config import ConfigError, load_settings
from blink_forwarder.telegram import TelegramClient, TelegramError


async def diagnose(send_test: bool = False) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 3

    async with aiohttp.ClientSession() as session:
        telegram = TelegramClient(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            session,
            silent=settings.silent,
        )
        try:
            me = await telegram.get_me()
            chat = await telegram.get_chat()
            member = await telegram.get_chat_member(int(me["id"]))
        except TelegramError as exc:
            print(f"Telegram error: {exc}")
            return 2

        if send_test:
            try:
                message_id = await telegram.send_message(
                    "🤖 blink-to-telegram test message"
                )
                print(f"Test message sent (message_id={message_id}).")
            except TelegramError as exc:
                print(f"Test message FAILED: {exc}")
                return 2

        try:
            admins = await telegram.get_chat_administrators()
        except TelegramError as exc:
            admins = [{"error": str(exc)}]

    print(
        json.dumps(
            {
                "bot": me,
                "chat": chat,
                "bot_member": member,
                "administrators": admins,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    print()
    print(f"Configured bot:  @{me.get('username', '?')} (id {me.get('id')})")
    print(
        f"Configured chat: {chat.get('title') or chat.get('username')} "
        f"({chat.get('type', '?')}, id {chat.get('id')})"
    )
    admin_ids = {
        entry.get("user", {}).get("id")
        for entry in admins
        if isinstance(entry, dict)
    }
    if me.get("id") in admin_ids:
        print("Bot in admin list: yes")
    else:
        print("Bot in admin list: NO (or the list could not be read)")

    if chat.get("type") != "channel":
        print(
            "NOTE: this chat is not a channel. For a channel, use its -100... id, "
            "not a linked discussion group."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram chat diagnostics")
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="also send a short test message to the configured chat",
    )
    args = parser.parse_args()
    return asyncio.run(diagnose(send_test=args.send_test))


if __name__ == "__main__":
    raise SystemExit(main())
