from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from blink_forwarder.blink_source import ClipInfo
from blink_forwarder.config import Settings
from blink_forwarder.db import StateDB
from blink_forwarder.forwarder import run

MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2048


class FakeSource:
    """Stands in for BlinkSource; records downloads."""

    downloads: list[Path] = []

    def __init__(self, login_file: Path) -> None:
        self.login_file = login_file
        self.info = ClipInfo(
            source_key="sync-1|Front Door|2026-01-02T03:04:05+00:00|2064",
            sync_name="Home",
            camera="Front Door",
            recorded_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            reported_size=2064,
        )

    async def __aenter__(self) -> "FakeSource":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    def sync_modules(self):
        return iter([("Home", object())])

    async def refresh_manifest(self, sync) -> list[object]:
        return [object()]

    def clip_info(self, sync, item) -> ClipInfo:
        return self.info

    async def download(self, item, destination: Path) -> None:
        FakeSource.downloads.append(destination)
        destination.write_bytes(MP4_BYTES)


class FakeTelegram:
    videos: list[tuple[str, str]] = []
    messages: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def send_video(self, path: Path, caption: str) -> int:
        FakeTelegram.videos.append((path.name, caption))
        return 42

    async def send_message(self, text: str) -> int:
        FakeTelegram.messages.append(text)
        return 1


class DummySession:
    async def close(self) -> None:
        return None


class ForwarderRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        FakeSource.downloads = []
        FakeTelegram.videos = []
        FakeTelegram.messages = []
        self.settings = Settings(
            telegram_bot_token="123456:ABC_def",
            telegram_chat_id="-1001234567890",
            blink_login_file=self.root / "blink_login.json",
            db_path=self.root / "state.db",
            archive_dir=self.root / "archive",
            tmp_dir=self.root / "tmp",
            tz=ZoneInfo("UTC"),
            tz_name="UTC",
            max_file_bytes=49_000_000,
            max_clips_per_run=50,
            send_delay_seconds=0,
            retention_days=90,
            silent=False,
            dry_run=False,
            log_level="WARNING",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self) -> int:
        with patch("aiohttp.ClientSession", DummySession), patch(
            "blink_forwarder.forwarder.BlinkSource", FakeSource
        ), patch("blink_forwarder.forwarder.TelegramClient", FakeTelegram):
            return asyncio.run(run(self.settings))

    def test_sends_once_then_dedups(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(FakeTelegram.videos), 1)
        self.assertEqual(len(FakeSource.downloads), 1)

        archived = list((self.root / "archive").rglob("*.mp4"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), MP4_BYTES)

        db = StateDB(self.settings.db_path)
        stats = db.stats()
        db.close()
        self.assertEqual(stats.sent, 1)

        # A second run must not re-download or re-send the same clip.
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(FakeTelegram.videos), 1)
        self.assertEqual(len(FakeSource.downloads), 1)

    def test_oversize_clip_is_skipped_and_alerted(self) -> None:
        self.settings = Settings(
            **{
                **self.settings.__dict__,
                "max_file_bytes": 100,
            }
        )
        self.assertEqual(self._run(), 0)
        self.assertEqual(FakeTelegram.videos, [])
        self.assertEqual(FakeSource.downloads, [])
        self.assertEqual(len(FakeTelegram.messages), 1)
        self.assertIn("too large", FakeTelegram.messages[0])

        db = StateDB(self.settings.db_path)
        stats = db.stats()
        db.close()
        self.assertEqual(stats.skipped, 1)

    def test_dry_run_does_not_download_or_mark(self) -> None:
        old = self.root / "archive" / "2020-01-01" / "old.mp4"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        os.utime(old, (0, 0))

        self.settings = Settings(
            **{
                **self.settings.__dict__,
                "dry_run": True,
            }
        )
        self.assertEqual(self._run(), 0)
        self.assertEqual(FakeSource.downloads, [])
        self.assertEqual(FakeTelegram.videos, [])
        self.assertEqual(FakeTelegram.messages, [])

        db = StateDB(self.settings.db_path)
        stats = db.stats()
        db.close()
        self.assertEqual(stats.sent, 0)
        self.assertEqual(stats.skipped, 0)

        # Dry runs must not prune the archive either.
        self.assertTrue(old.exists())


if __name__ == "__main__":
    unittest.main()
