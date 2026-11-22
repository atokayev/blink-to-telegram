from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from blink_forwarder.telegram import (
    TelegramClient,
    TelegramConfigError,
    TelegramPermanentError,
)


class FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):  # noqa: ANN001
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class TelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.video = Path(self.tmp.name) / "clip.mp4"
        self.video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2048)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_send_video_success(self) -> None:
        session = FakeSession(
            [FakeResponse(200, {"ok": True, "result": {"message_id": 7}})]
        )
        client = TelegramClient("123:abc", "-1001", session)  # type: ignore[arg-type]
        message_id = asyncio.run(
            client.send_video(self.video, "🎥 <b>Front</b>")
        )
        self.assertEqual(message_id, 7)
        self.assertTrue(session.calls[0][0].endswith("/sendVideo"))
        self.assertIn("data", session.calls[0][1])

    def test_send_video_retries_429(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    429,
                    {
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 1},
                    },
                ),
                FakeResponse(200, {"ok": True, "result": {"message_id": 8}}),
            ]
        )
        client = TelegramClient("123:abc", "-1001", session)  # type: ignore[arg-type]
        with patch(
            "blink_forwarder.telegram.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            message_id = asyncio.run(
                client.send_video(self.video, "caption")
            )
        self.assertEqual(message_id, 8)
        self.assertEqual(len(session.calls), 2)
        sleep.assert_awaited_once()

    def test_send_video_permanent_error(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    400,
                    {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: video is too big",
                    },
                )
            ]
        )
        client = TelegramClient("123:abc", "-1001", session)  # type: ignore[arg-type]
        with self.assertRaises(TelegramPermanentError):
            asyncio.run(client.send_video(self.video, "caption"))

    def test_send_video_config_error_is_not_marked_permanent(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    403,
                    {
                        "ok": False,
                        "error_code": 403,
                        "description": "Forbidden: bot is not a member of the channel chat",
                    },
                )
            ]
        )
        client = TelegramClient("123:abc", "-1001", session)  # type: ignore[arg-type]
        with self.assertRaises(TelegramConfigError):
            asyncio.run(client.send_video(self.video, "caption"))


if __name__ == "__main__":
    unittest.main()
