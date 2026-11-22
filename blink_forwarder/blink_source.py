"""Blink account access and local-storage clip retrieval.

Blink does not expose the Sync Module's USB drive over the LAN.  The official
app asks Blink's cloud to build a manifest of the clips on the module, then asks
the module to upload a selected clip to the cloud so it can be downloaded.  This
module drives that flow through blinkpy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import aiohttp
from blinkpy import api
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.helpers import util as blink_util
from blinkpy.helpers.util import to_alphanumeric
from blinkpy.sync_module import LocalStorageMediaItem

_LOGGER = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class BlinkAuthError(RuntimeError):
    """The saved Blink session cannot be used and a login is required."""


class BlinkSourceError(RuntimeError):
    """A Blink API/local-storage operation failed."""


@dataclass(frozen=True)
class ClipInfo:
    source_key: str
    sync_name: str
    camera: str
    recorded_at: dt.datetime  # timezone-aware, UTC
    reported_size: int | None

    @property
    def filename(self) -> str:
        stamp = self.recorded_at.astimezone(dt.timezone.utc).strftime(
            "%Y-%m-%d_%H-%M-%S"
        )
        return f"{stamp}_{_slug(self.camera)}.mp4"


def _slug(value: str, limit: int = 40) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return (slug or "camera")[:limit].strip("-") or "camera"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_mp4(path: Path) -> bool:
    """Cheap sanity check so an HTML error page is never posted as a video."""
    try:
        if path.stat().st_size < 1024:
            return False
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        return False
    return b"ftyp" in head


async def save_login_data(blink: Blink, path: Path) -> None:
    """Persist refreshed tokens atomically, without the account password."""
    data = dict(blink.auth.login_attributes)
    data.pop("password", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    await blink_util.json_save(data, str(tmp))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class BlinkSource:
    """Async context manager around a logged-in blinkpy client."""

    def __init__(self, login_file: Path) -> None:
        self.login_file = login_file
        self.blink: Blink | None = None
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "BlinkSource":
        if not self.login_file.is_file():
            raise BlinkAuthError(
                f"Blink login file not found: {self.login_file}. "
                "Run `blink-login` on the VM to authenticate."
            )

        try:
            login_data = await blink_util.json_load(str(self.login_file))
        except Exception as exc:
            raise BlinkAuthError(
                f"Could not read Blink login file {self.login_file}: {exc}"
            ) from exc
        if not isinstance(login_data, dict):
            raise BlinkAuthError(
                f"Blink login file {self.login_file} does not contain a JSON object"
            )

        self._session = aiohttp.ClientSession()
        blink = Blink(session=self._session)
        blink.auth = Auth(login_data, no_prompt=True, session=self._session)

        try:
            try:
                ok = await blink.start()
            except BlinkTwoFARequiredError as exc:
                raise BlinkAuthError(
                    "Blink is asking for a 2FA code. Run `blink-login` on the VM "
                    "to refresh the saved session."
                ) from exc
            except Exception as exc:
                raise BlinkAuthError(f"Blink login failed: {exc}") from exc

            if not ok or not blink.available:
                raise BlinkAuthError(
                    "Blink login failed. The saved session may have expired; "
                    "run `blink-login` on the VM."
                )
        except BaseException:
            await self._session.close()
            self._session = None
            raise

        self.blink = blink
        try:
            await save_login_data(blink, self.login_file)
        except Exception:
            _LOGGER.warning("Could not persist refreshed Blink tokens", exc_info=True)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None

    def sync_modules(self) -> Iterator[tuple[str, Any]]:
        assert self.blink is not None
        return iter(self.blink.sync.items())

    async def refresh_manifest(self, sync: Any) -> list[Any]:
        """Fetch the current local-storage manifest for one Sync Module.

        We parse the raw manifest instead of calling blinkpy's
        ``update_local_storage_manifest``: that method stores items in a
        ``SortedSet`` whose equality is based only on the clip timestamp, so two
        clips recorded in the same second (different cameras) can collapse into
        one.  A plain list preserves every clip.
        """
        if not sync.local_storage:
            _LOGGER.warning(
                "%s: local storage is not active. Enable it in the Blink app "
                "and make sure the USB drive is formatted.",
                sync.name,
            )
            return []

        assert self.blink is not None
        try:
            request = await sync.poll_local_storage_manifest(max_retries=4)
            if not request or "id" not in request:
                raise BlinkSourceError("manifest request did not return an id")

            response = await sync.poll_local_storage_manifest(
                request["id"], max_retries=4
            )
            if not response or "clips" not in response or "manifest_id" not in response:
                raise BlinkSourceError("manifest response was incomplete")
        except BlinkSourceError:
            raise
        except Exception as exc:
            raise BlinkSourceError(
                f"{sync.name}: could not read the local-storage manifest: {exc}"
            ) from exc

        manifest_id = response["manifest_id"]
        template = string.Template(api.local_storage_clip_url_template()).substitute(
            account_id=self.blink.account_id,
            network_id=sync.network_id,
            sync_id=sync.sync_id,
            manifest_id="$manifest_id",
            clip_id="$clip_id",
        )
        names = {to_alphanumeric(name): name for name in sync.cameras}

        clips = response.get("clips") or []
        if not isinstance(clips, list):
            raise BlinkSourceError(
                f"{sync.name}: unexpected manifest clips payload: {type(clips).__name__}"
            )

        items: list[Any] = []
        try:
            for raw in clips:
                camera_key = raw["camera_name"]
                camera_name = names.get(camera_key, camera_key)
                items.append(
                    LocalStorageMediaItem(
                        raw["id"],
                        camera_name,
                        raw["created_at"],
                        raw["size"],
                        manifest_id,
                        template,
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise BlinkSourceError(
                f"{sync.name}: malformed clip in local-storage manifest: {exc}"
            ) from exc

        _LOGGER.info("%s: %d clip(s) in local storage", sync.name, len(items))
        return items

    def clip_info(self, sync: Any, item: Any) -> ClipInfo:
        recorded = item.created_at
        if not isinstance(recorded, dt.datetime):
            raise BlinkSourceError(f"Clip has an invalid timestamp: {recorded!r}")
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=dt.timezone.utc)
        else:
            recorded = recorded.astimezone(dt.timezone.utc)

        serial = getattr(sync, "serial", None) or getattr(sync, "sync_id", None)
        serial = serial or getattr(sync, "name", "sync")
        camera = str(item.name)
        size = _as_int(getattr(item, "size", None))

        # Deliberately excludes the manifest/clip id: those can change when
        # Blink rebuilds a manifest, while camera+timestamp+size are stable.
        source_key = f"{serial}|{camera}|{recorded.isoformat()}|{size}"
        return ClipInfo(
            source_key=source_key,
            sync_name=str(sync.name),
            camera=camera,
            recorded_at=recorded,
            reported_size=size,
        )

    async def download(self, item: Any, destination: Path) -> None:
        """Ask the Sync Module to upload a clip, then download it to disk."""
        assert self.blink is not None
        destination.parent.mkdir(parents=True, exist_ok=True)

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                destination.unlink(missing_ok=True)
                await item.prepare_download(self.blink)
                ok = await item.download_video(self.blink, str(destination))
                if not ok:
                    raise BlinkSourceError("download_video returned False")
                if not _is_mp4(destination):
                    raise BlinkSourceError(
                        "downloaded data is not a valid MP4 (Blink may have "
                        "returned an error page)"
                    )
                return
            except Exception as exc:
                last_error = exc
                _LOGGER.warning(
                    "Download attempt %d/2 failed for %s: %s",
                    attempt + 1,
                    getattr(item, "name", item),
                    exc,
                )
                await asyncio.sleep(5 * (attempt + 1))

        raise BlinkSourceError(f"Could not download clip: {last_error}")
