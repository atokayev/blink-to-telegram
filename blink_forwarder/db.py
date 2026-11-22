"""SQLite state store used to make forwarding idempotent."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    source_key       TEXT PRIMARY KEY,
    content_sha256   TEXT,
    sync_name        TEXT NOT NULL,
    camera           TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    size_bytes       INTEGER,
    status           TEXT NOT NULL CHECK (status IN ('sent', 'skipped')),
    reason           TEXT,
    message_id       INTEGER,
    first_seen_at    TEXT NOT NULL,
    processed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS clips_sha256_idx ON clips (content_sha256);
CREATE INDEX IF NOT EXISTS clips_status_idx ON clips (status);

CREATE TABLE IF NOT EXISTS alerts (
    key           TEXT PRIMARY KEY,
    last_sent_at  TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Stats:
    sent: int
    skipped: int
    last_processed_at: str | None


class StateDB:
    """Small synchronous SQLite wrapper.

    The forwarder is single-threaded, so a plain ``sqlite3`` connection is
    sufficient and avoids an extra async dependency.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def is_processed(self, source_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM clips WHERE source_key = ?", (source_key,)
        ).fetchone()
        return row is not None

    def sha_seen(self, digest: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM clips WHERE content_sha256 = ? LIMIT 1", (digest,)
        ).fetchone()
        return row is not None

    def mark_sent(
        self,
        *,
        source_key: str,
        content_sha256: str,
        sync_name: str,
        camera: str,
        recorded_at: str,
        size_bytes: int | None,
        message_id: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO clips (
                source_key, content_sha256, sync_name, camera, recorded_at,
                size_bytes, status, reason, message_id, first_seen_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'sent', NULL, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                content_sha256 = excluded.content_sha256,
                status = 'sent',
                reason = NULL,
                message_id = excluded.message_id,
                processed_at = excluded.processed_at
            """,
            (
                source_key,
                content_sha256,
                sync_name,
                camera,
                recorded_at,
                size_bytes,
                message_id,
                now,
                now,
            ),
        )
        self._conn.commit()

    def mark_skipped(
        self,
        *,
        source_key: str,
        sync_name: str,
        camera: str,
        recorded_at: str,
        size_bytes: int | None,
        reason: str,
        content_sha256: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO clips (
                source_key, content_sha256, sync_name, camera, recorded_at,
                size_bytes, status, reason, message_id, first_seen_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'skipped', ?, NULL, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                content_sha256 = COALESCE(excluded.content_sha256, clips.content_sha256),
                status = 'skipped',
                reason = excluded.reason,
                processed_at = excluded.processed_at
            """,
            (
                source_key,
                content_sha256,
                sync_name,
                camera,
                recorded_at,
                size_bytes,
                reason,
                now,
                now,
            ),
        )
        self._conn.commit()

    def claim_alert(self, key: str, min_interval: timedelta = timedelta(hours=12)) -> bool:
        """Return True if an alert with this key may be sent now.

        Claiming also records the send time, which throttles repeated failures
        (for example an expired Blink login) to one alert per interval.
        """
        now = datetime.now(timezone.utc)
        row = self._conn.execute(
            "SELECT last_sent_at FROM alerts WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            try:
                last = datetime.fromisoformat(row["last_sent_at"])
            except ValueError:
                last = datetime.min.replace(tzinfo=timezone.utc)
            if now - last < min_interval:
                return False
        self._conn.execute(
            """
            INSERT INTO alerts (key, last_sent_at) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET last_sent_at = excluded.last_sent_at
            """,
            (key, now.isoformat()),
        )
        self._conn.commit()
        return True

    def stats(self) -> Stats:
        sent = self._conn.execute(
            "SELECT COUNT(*) FROM clips WHERE status = 'sent'"
        ).fetchone()[0]
        skipped = self._conn.execute(
            "SELECT COUNT(*) FROM clips WHERE status = 'skipped'"
        ).fetchone()[0]
        row = self._conn.execute(
            "SELECT MAX(processed_at) FROM clips"
        ).fetchone()
        return Stats(sent=sent, skipped=skipped, last_processed_at=row[0])
