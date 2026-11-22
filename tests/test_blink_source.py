from __future__ import annotations

import asyncio
import datetime as dt
import unittest
from pathlib import Path

from blink_forwarder.blink_source import BlinkSource


class FakeBlink:
    account_id = "account-1"


class FakeSync:
    name = "Home"
    network_id = "network-1"
    sync_id = "sync-1"
    local_storage = True
    cameras = {"Front Door": object(), "Back Yard": object()}

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)

    async def poll_local_storage_manifest(
        self, manifest_request_id=None, max_retries=4
    ) -> dict:
        return self.responses.pop(0)


class BlinkManifestTests(unittest.TestCase):
    def _source(self) -> BlinkSource:
        source = BlinkSource(Path("/nonexistent"))
        source.blink = FakeBlink()  # type: ignore[assignment]
        return source

    def test_manifest_keeps_same_timestamp_clips(self) -> None:
        sync = FakeSync(
            [
                {"id": "request-1"},
                {
                    "manifest_id": "manifest-1",
                    "clips": [
                        {
                            "id": "13",
                            "camera_name": "BackYard",
                            "created_at": "2026-01-02T03:04:06",
                            "size": 333,
                        },
                        {
                            "id": "11",
                            "camera_name": "FrontDoor",
                            "created_at": "2026-01-02T03:04:05",
                            "size": 111,
                        },
                        {
                            "id": "12",
                            "camera_name": "FrontDoor",
                            "created_at": "2026-01-02T03:04:05",
                            "size": 222,
                        },
                    ],
                },
            ]
        )
        items = asyncio.run(self._source().refresh_manifest(sync))
        # Oldest first; ties on the timestamp broken by clip id.
        self.assertEqual([item.id for item in items], [11, 12, 13])
        self.assertEqual(
            [item.name for item in items],
            ["Front Door", "Front Door", "Back Yard"],
        )
        self.assertEqual([item.size for item in items], [111, 222, 333])

    def test_clip_info_is_stable_and_utc(self) -> None:
        sync = FakeSync([])
        item = type(
            "Item",
            (),
            {
                "name": "Front Door",
                "created_at": dt.datetime(2026, 1, 2, 3, 4, 5),
                "size": 123,
            },
        )()
        info = self._source().clip_info(sync, item)
        self.assertEqual(
            info.source_key,
            "sync-1|Front Door|2026-01-02T03:04:05+00:00|123",
        )
        self.assertEqual(info.filename, "2026-01-02_03-04-05_front-door.mp4")


if __name__ == "__main__":
    unittest.main()
