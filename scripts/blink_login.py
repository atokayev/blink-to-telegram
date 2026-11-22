#!/usr/bin/env python3
"""Interactive one-time Blink login.

Run this on the machine that will execute the cron job (the Oracle VM).  It
performs the OAuth login, handles a 2FA prompt, and stores the refresh token in
the login file read by the forwarder.  The account password is *not* written to
disk.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

import aiohttp
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink

from blink_forwarder.blink_source import save_login_data
from blink_forwarder.config import load_env_file


def default_login_file() -> Path:
    data_dir = Path(os.environ.get("BLINK_DATA_DIR", "data")).expanduser()
    return Path(
        os.environ.get("BLINK_LOGIN_FILE", str(data_dir / "blink_login.json"))
    ).expanduser()


async def login(login_file: Path, username: str | None) -> int:
    if not username:
        username = input("Blink username (email): ").strip()
    if not username:
        print("A username is required.", file=sys.stderr)
        return 2

    password = getpass.getpass("Blink password: ")
    if not password:
        print("A password is required.", file=sys.stderr)
        return 2

    session = aiohttp.ClientSession()
    blink = Blink(session=session)
    blink.auth = Auth(
        {"username": username, "password": password},
        no_prompt=True,
        session=session,
    )

    try:
        try:
            ok = await blink.start()
        except BlinkTwoFARequiredError:
            code = input("2FA code sent by Blink: ").strip()
            ok = await blink.send_2fa_code(code)

        if not ok or not blink.available:
            print(
                "Login failed. Check the username/password and try again.",
                file=sys.stderr,
            )
            return 2

        try:
            await save_login_data(blink, login_file)
        except OSError as exc:
            print(f"Could not save the login file {login_file}: {exc}", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 2
    finally:
        await session.close()

    print(f"\nLogin saved to {login_file} (mode 600, password not stored).")
    print("Sync modules:")
    for name, sync in blink.sync.items():
        status = "active" if sync.local_storage else "INACTIVE"
        print(f"  - {name}: local storage {status}")
        if not sync.local_storage:
            print(
                "    Enable Local Storage in the Blink app and format the USB "
                "drive, then re-run `blink-forwarder --check`."
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description="One-time Blink login")
    parser.add_argument("--login-file", type=Path, default=None)
    parser.add_argument("--username", default=os.environ.get("BLINK_USERNAME"))
    args = parser.parse_args(argv)
    return asyncio.run(login(args.login_file or default_login_file(), args.username))


if __name__ == "__main__":
    raise SystemExit(main())
