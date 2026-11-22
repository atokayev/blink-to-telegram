from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from blink_forwarder.blink_source import ClipInfo, _is_mp4
from blink_forwarder.config import ConfigError, load_settings
from blink_forwarder.db import StateDB
from blink_forwarder.forwarder import archive_path, build_caption, sha256_file


class StateDBTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.tmp.name) / "state.db")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_mark_sent_and_dedup(self) -> None:
        key = "serial|cam|2026-01-01T00:00:00+00:00|123"
        self.assertFalse(self.db.is_processed(key))
        self.db.mark_sent(
            source_key=key,
            content_sha256="deadbeef",
            sync_name="Home",
            camera="Front",
            recorded_at="2026-01-01T00:00:00+00:00",
            size_bytes=123,
            message_id=42,
        )
        self.assertTrue(self.db.is_processed(key))
        self.assertTrue(self.db.sha_seen("deadbeef"))
        self.assertFalse(self.db.sha_seen("cafebabe"))
        stats = self.db.stats()
        self.assertEqual(stats.sent, 1)
        self.assertEqual(stats.skipped, 0)

    def test_mark_skipped(self) -> None:
        key = "serial|cam|2026-01-01T00:00:00+00:00|999"
        self.db.mark_skipped(
            source_key=key,
            sync_name="Home",
            camera="Front",
            recorded_at="2026-01-01T00:00:00+00:00",
            size_bytes=999,
            reason="too_large",
        )
        self.assertTrue(self.db.is_processed(key))
        self.assertEqual(self.db.stats().skipped, 1)

    def test_alert_throttle(self) -> None:
        self.assertTrue(self.db.claim_alert("auth", timedelta(hours=1)))
        self.assertFalse(self.db.claim_alert("auth", timedelta(hours=1)))
        self.assertTrue(self.db.claim_alert("other", timedelta(hours=1)))


class HelperTests(unittest.TestCase):
    def test_caption_escapes_html(self) -> None:
        info = ClipInfo(
            source_key="k",
            sync_name="Home & Garage",
            camera="Front <Door>",
            recorded_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            reported_size=123,
        )
        caption = build_caption(info, ZoneInfo("America/New_York"))
        self.assertIn("Front &lt;Door&gt;", caption)
        self.assertIn("Home &amp; Garage", caption)
        self.assertIn("2026-01-01 22:04:05", caption)

    def test_archive_path_collision(self) -> None:
        info = ClipInfo(
            source_key="k",
            sync_name="Home",
            camera="Front Door",
            recorded_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            reported_size=123,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = archive_path(root, info)
            first.parent.mkdir(parents=True)
            first.write_bytes(b"x")
            second = archive_path(root, info)
            self.assertNotEqual(first, second)
            self.assertTrue(second.name.endswith("_1.mp4"))

    def test_sha256_file(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"hello")
            name = handle.name
        try:
            self.assertEqual(
                sha256_file(Path(name)),
                "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            )
        finally:
            os.unlink(name)

    def test_mp4_sniffing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.mp4"
            good.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2048)
            self.assertTrue(_is_mp4(good))

            bad = Path(tmp) / "bad.mp4"
            bad.write_bytes(b"<html>error</html>" * 100)
            self.assertFalse(_is_mp4(bad))

            tiny = Path(tmp) / "tiny.mp4"
            tiny.write_bytes(b"\x00\x00\x00\x18ftypisom")
            self.assertFalse(_is_mp4(tiny))


class ConfigTests(unittest.TestCase):
    def _env(self, **overrides: str) -> dict[str, str]:
        base = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC_def-123",
            "TELEGRAM_CHAT_ID": "-1001234567890",
            "BLINK_ENV_FILE": "/nonexistent/.env",
            "BLINK_DATA_DIR": tempfile.gettempdir(),
            "TZ": "UTC",
        }
        base.update(overrides)
        return base

    def test_defaults(self) -> None:
        with patch.dict(os.environ, self._env(), clear=True):
            settings = load_settings()
        self.assertEqual(settings.max_file_bytes, 49_000_000)
        self.assertEqual(settings.max_clips_per_run, 50)
        self.assertEqual(settings.send_delay_seconds, 4.0)
        self.assertFalse(settings.dry_run)

    def test_overrides(self) -> None:
        with patch.dict(
            os.environ,
            self._env(MAX_FILE_MB="20", MAX_CLIPS_PER_RUN="5", DRY_RUN="true"),
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.max_file_bytes, 20_000_000)
        self.assertEqual(settings.max_clips_per_run, 5)
        self.assertTrue(settings.dry_run)

    def test_missing_token(self) -> None:
        with patch.dict(os.environ, self._env(), clear=True):
            del os.environ["TELEGRAM_BOT_TOKEN"]
            with self.assertRaises(ConfigError):
                load_settings()

    def test_bad_token(self) -> None:
        with patch.dict(
            os.environ, self._env(TELEGRAM_BOT_TOKEN="not-a-token"), clear=True
        ):
            with self.assertRaises(ConfigError):
                load_settings()

    def test_bad_timezone(self) -> None:
        with patch.dict(os.environ, self._env(TZ="Mars/Olympus"), clear=True):
            with self.assertRaises(ConfigError):
                load_settings()

    def test_blink_env_file_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "custom.env"
            env_file.write_text(
                "TELEGRAM_BOT_TOKEN=999:XYZ\n"
                "TELEGRAM_CHAT_ID=-100999\n"
                f"BLINK_DATA_DIR={tmp}\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"BLINK_ENV_FILE": str(env_file)}, clear=True
            ):
                settings = load_settings()
        self.assertEqual(settings.telegram_bot_token, "999:XYZ")
        self.assertEqual(settings.telegram_chat_id, "-100999")
        self.assertEqual(settings.db_path, Path(tmp) / "state.db")

    def test_environment_overrides_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "custom.env"
            env_file.write_text(
                "TELEGRAM_BOT_TOKEN=999:FROMFILE\n"
                "TELEGRAM_CHAT_ID=-100999\n"
                f"BLINK_DATA_DIR={tmp}\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "BLINK_ENV_FILE": str(env_file),
                    "TELEGRAM_BOT_TOKEN": "111:FROMENV",
                },
                clear=True,
            ):
                settings = load_settings()
        self.assertEqual(settings.telegram_bot_token, "111:FROMENV")


if __name__ == "__main__":
    unittest.main()
