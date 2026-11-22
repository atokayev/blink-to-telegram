"""Orchestrates one Blink -> Telegram forwarding run."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import shutil
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import aiohttp

from .blink_source import BlinkAuthError, BlinkSource, BlinkSourceError, ClipInfo
from .config import Settings
from .db import StateDB
from .telegram import (
    TelegramClient,
    TelegramConfigError,
    TelegramError,
    TelegramPermanentError,
)

_LOGGER = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("blinkpy").setLevel(
        logging.INFO if level == "DEBUG" else logging.WARNING
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_caption(info: ClipInfo, tz, include_sync: bool = True) -> str:
    """Single-line caption, e.g. ``Front Door — 2026-11-21 20:27:36``.

    Local time in ``tz``. When several Sync Modules feed the same channel,
    the module name is appended in parentheses.
    """
    local = info.recorded_at.astimezone(tz)
    camera = html.escape(info.camera[:80], quote=False)
    line = f"{camera} — {local:%Y-%m-%d %H:%M:%S}"
    if include_sync:
        line += f" ({html.escape(info.sync_name[:80], quote=False)})"
    return line


def archive_path(root: Path, info: ClipInfo) -> Path:
    """Return a collision-free path under ``archive/YYYY-MM-DD/``."""
    day = info.recorded_at.strftime("%Y-%m-%d")
    base = root / day / info.filename
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    for index in range(1, 10_000):
        candidate = base.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a free archive name for {base}")


def prune_archive(root: Path, retention_days: int) -> None:
    """Delete archived MP4s older than ``retention_days`` (0 = keep forever)."""
    if retention_days <= 0 or not root.exists():
        return
    cutoff = time.time() - retention_days * 86_400
    removed = 0
    for path in root.rglob("*.mp4"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            _LOGGER.warning("Could not prune %s", path, exc_info=True)

    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        directory = Path(dirpath)
        if directory == root:
            continue
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass

    if removed:
        _LOGGER.info("Pruned %d archived clip(s) older than %d days", removed, retention_days)


async def _send_alert(
    db: StateDB,
    telegram: TelegramClient,
    key: str,
    text: str,
    min_interval: timedelta = timedelta(hours=12),
) -> None:
    if not db.claim_alert(key, min_interval):
        return
    try:
        await telegram.send_message(text)
    except TelegramError as exc:
        _LOGGER.warning("Could not deliver Telegram alert %r: %s", key, exc)


async def run(settings: Settings) -> int:
    _configure_logging(settings.log_level)
    settings.ensure_dirs()

    _LOGGER.info(
        "Starting run (dry_run=%s, tz=%s, max_clips=%d, max_file=%d bytes)",
        settings.dry_run,
        settings.tz_name,
        settings.max_clips_per_run,
        settings.max_file_bytes,
    )

    db = StateDB(settings.db_path)
    if not settings.dry_run:
        prune_archive(settings.archive_dir, settings.retention_days)

    sent = 0
    skipped = 0
    failed = 0
    would_send = 0

    session = aiohttp.ClientSession()
    telegram = TelegramClient(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        session,
        silent=settings.silent,
    )

    try:
        try:
            async with BlinkSource(settings.blink_login_file) as source:
                include_sync = source.sync_count() > 1
                limit_reached = False
                for sync_name, sync in source.sync_modules():
                    if limit_reached:
                        break
                    try:
                        manifest = await source.refresh_manifest(sync)
                    except BlinkSourceError as exc:
                        _LOGGER.error("%s", exc)
                        failed += 1
                        continue
                    except Exception:
                        _LOGGER.exception(
                            "%s: unexpected manifest failure", sync_name
                        )
                        failed += 1
                        continue

                    for item in manifest:
                        if sent >= settings.max_clips_per_run:
                            _LOGGER.info(
                                "Reached MAX_CLIPS_PER_RUN=%d; the remaining "
                                "clips will be picked up on the next run.",
                                settings.max_clips_per_run,
                            )
                            limit_reached = True
                            break

                        try:
                            info = source.clip_info(sync, item)
                        except BlinkSourceError as exc:
                            _LOGGER.warning("Skipping malformed manifest item: %s", exc)
                            failed += 1
                            continue
                        except Exception as exc:
                            _LOGGER.warning("Skipping malformed manifest item: %s", exc)
                            failed += 1
                            continue

                        if db.is_processed(info.source_key):
                            continue

                        if settings.dry_run:
                            _LOGGER.info(
                                "[dry-run] would send %s (%s, %s bytes)",
                                info.filename,
                                info.camera,
                                info.reported_size,
                            )
                            would_send += 1
                            sent += 1
                            continue

                        if (
                            info.reported_size is not None
                            and info.reported_size > settings.max_file_bytes
                        ):
                            db.mark_skipped(
                                source_key=info.source_key,
                                sync_name=info.sync_name,
                                camera=info.camera,
                                recorded_at=info.recorded_at.isoformat(),
                                size_bytes=info.reported_size,
                                reason=f"too_large:{info.reported_size}",
                            )
                            skipped += 1
                            _LOGGER.warning(
                                "Clip %s is %.1f MB, above the %.0f MB bot limit; skipped",
                                info.filename,
                                info.reported_size / 1_000_000,
                                settings.max_file_bytes / 1_000_000,
                            )
                            await _send_alert(
                                db,
                                telegram,
                                "oversize",
                                (
                                    "⚠️ <b>Blink clip too large for Telegram</b>\n"
                                    f"{html.escape(info.filename)}\n"
                                    f"{info.reported_size / 1_000_000:.1f} MB "
                                    "(bot API limit is 50 MB).\n"
                                    "The clip is still on the USB drive."
                                ),
                                timedelta(hours=24),
                            )
                            continue

                        tmp_path = settings.tmp_dir / f"{uuid.uuid4().hex}.mp4"
                        try:
                            await source.download(item, tmp_path)

                            actual_size = tmp_path.stat().st_size
                            if actual_size > settings.max_file_bytes:
                                db.mark_skipped(
                                    source_key=info.source_key,
                                    sync_name=info.sync_name,
                                    camera=info.camera,
                                    recorded_at=info.recorded_at.isoformat(),
                                    size_bytes=actual_size,
                                    reason=f"too_large_actual:{actual_size}",
                                )
                                skipped += 1
                                _LOGGER.warning(
                                    "Downloaded clip %s is %.1f MB, above the "
                                    "%.0f MB bot limit; skipped",
                                    info.filename,
                                    actual_size / 1_000_000,
                                    settings.max_file_bytes / 1_000_000,
                                )
                                await _send_alert(
                                    db,
                                    telegram,
                                    "oversize",
                                    (
                                        "⚠️ <b>Blink clip too large for Telegram</b>\n"
                                        f"{html.escape(info.filename)}\n"
                                        f"{actual_size / 1_000_000:.1f} MB "
                                        "(bot API limit is 50 MB).\n"
                                        "The clip is still on the USB drive."
                                    ),
                                    timedelta(hours=24),
                                )
                                continue

                            digest = sha256_file(tmp_path)

                            if db.sha_seen(digest):
                                db.mark_skipped(
                                    source_key=info.source_key,
                                    sync_name=info.sync_name,
                                    camera=info.camera,
                                    recorded_at=info.recorded_at.isoformat(),
                                    size_bytes=info.reported_size,
                                    reason="duplicate_sha256",
                                    content_sha256=digest,
                                )
                                skipped += 1
                                _LOGGER.info(
                                    "Skipping %s: identical content already sent",
                                    info.filename,
                                )
                                continue

                            caption = build_caption(info, settings.tz, include_sync)
                            message_id = await telegram.send_video(tmp_path, caption)

                            destination = archive_path(settings.archive_dir, info)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(tmp_path), destination)

                            db.mark_sent(
                                source_key=info.source_key,
                                content_sha256=digest,
                                sync_name=info.sync_name,
                                camera=info.camera,
                                recorded_at=info.recorded_at.isoformat(),
                                size_bytes=destination.stat().st_size,
                                message_id=message_id,
                            )
                            sent += 1
                            _LOGGER.info(
                                "Sent %s as Telegram message %s", destination.name, message_id
                            )
                            await asyncio.sleep(settings.send_delay_seconds)

                        except TelegramPermanentError as exc:
                            # Keep the file for inspection but do not retry forever.
                            quarantine = settings.archive_dir / "failed"
                            quarantine.mkdir(parents=True, exist_ok=True)
                            destination = archive_path(quarantine, info)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                shutil.move(str(tmp_path), destination)
                            except OSError:
                                pass
                            db.mark_skipped(
                                source_key=info.source_key,
                                sync_name=info.sync_name,
                                camera=info.camera,
                                recorded_at=info.recorded_at.isoformat(),
                                size_bytes=info.reported_size,
                                reason=f"telegram:{exc}",
                            )
                            skipped += 1
                            _LOGGER.error(
                                "Telegram rejected %s (kept in %s): %s",
                                info.filename,
                                destination,
                                exc,
                            )
                        except TelegramConfigError:
                            # Bad token / chat id / permissions. Do not mark the
                            # clip, and abort so the backlog is retried after the
                            # configuration is fixed.
                            raise
                        except Exception:
                            failed += 1
                            _LOGGER.exception("Failed to process %s", info.filename)
                        finally:
                            tmp_path.unlink(missing_ok=True)

        except BlinkAuthError as exc:
            _LOGGER.error("%s", exc)
            if not settings.dry_run:
                await _send_alert(
                    db,
                    telegram,
                    "blink-auth",
                    (
                        "⚠️ <b>Blink forwarder needs a login</b>\n"
                        f"{html.escape(str(exc))}\n\n"
                        "On the Oracle VM run:\n"
                        "<code>sudo -u blink blink-login</code>"
                    ),
                )
            return 2

        except TelegramConfigError as exc:
            _LOGGER.error(
                "Telegram configuration problem: %s. Fix the bot token/chat id/"
                "admin permissions; clips were left unprocessed for the next run.",
                exc,
            )
            return 1
        except Exception:
            _LOGGER.exception("Unexpected failure")
            return 1

    finally:
        await session.close()
        db.close()

    if settings.dry_run:
        _LOGGER.info("Dry run complete: %d clip(s) would be sent", would_send)
    else:
        _LOGGER.info(
            "Run complete: %d sent, %d skipped, %d failed", sent, skipped, failed
        )
    return 0 if failed == 0 else 1
