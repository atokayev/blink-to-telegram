"""Minimal async Telegram Bot API client for video uploads."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org/bot{token}/{method}"
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300, connect=30, sock_read=120)
CALL_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=20)


class TelegramError(RuntimeError):
    """Base class for Telegram failures."""


class TelegramPermanentError(TelegramError):
    """Telegram rejected the request; retrying will not help."""


class TelegramConfigError(TelegramError):
    """The bot token, chat id, or bot permissions are wrong.

    These failures must not mark a clip as processed: once the configuration is
    fixed, the same clip should be retried.
    """


_CONFIG_HINTS = (
    "chat not found",
    "not enough rights",
    "bot is not a member",
    "have no rights",
    "chat_id is empty",
    "peer_id_invalid",
)


def _error_class(code: int, description: str) -> type[TelegramError]:
    if code in {401, 403, 404}:
        return TelegramConfigError
    if code == 400 and any(hint in description.lower() for hint in _CONFIG_HINTS):
        return TelegramConfigError
    return TelegramPermanentError


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str,
        session: aiohttp.ClientSession,
        *,
        silent: bool = False,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._session = session
        self._silent = silent

    def _url(self, method: str) -> str:
        return API_ROOT.format(token=self._token, method=method)

    async def _json(self, response: aiohttp.ClientResponse) -> dict:
        try:
            payload = await response.json(content_type=None)
        except (ValueError, aiohttp.ContentTypeError) as exc:
            raise TelegramError(
                f"Telegram returned a non-JSON response (HTTP {response.status})"
            ) from exc
        if not isinstance(payload, dict):
            raise TelegramError(f"Unexpected Telegram response: {payload!r}")
        return payload

    @staticmethod
    def _retry_after(payload: dict, default: int = 5) -> int:
        parameters = payload.get("parameters") or {}
        try:
            value = int(parameters.get("retry_after", default))
        except (TypeError, ValueError):
            value = default
        return min(max(value, 1), 120)

    async def send_video(self, path: Path, caption: str) -> int:
        """Upload one MP4 with ``sendVideo`` and return its message id."""
        last_error: Exception | None = None
        for attempt in range(5):
            form = aiohttp.FormData()
            form.add_field("chat_id", self._chat_id)
            form.add_field("caption", caption[:1024])
            form.add_field("parse_mode", "HTML")
            form.add_field("supports_streaming", "true")
            if self._silent:
                form.add_field("disable_notification", "true")

            # The file handle must be reopened for every attempt.
            handle = path.open("rb")
            try:
                form.add_field(
                    "video",
                    handle,
                    filename=path.name,
                    content_type="video/mp4",
                )
                async with self._session.post(
                    self._url("sendVideo"), data=form, timeout=UPLOAD_TIMEOUT
                ) as response:
                    status = response.status
                    payload = await self._json(response)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                delay = min(2**attempt, 30)
                _LOGGER.warning(
                    "Telegram upload error (%s); retrying in %ss", exc, delay
                )
                await asyncio.sleep(delay)
                continue
            finally:
                handle.close()

            if payload.get("ok"):
                result = payload.get("result") or {}
                return int(result.get("message_id", 0))

            code = int(payload.get("error_code", status))
            description = str(payload.get("description", "unknown error"))

            if code == 429:
                delay = self._retry_after(payload)
                _LOGGER.warning("Telegram rate limit hit; sleeping %ss", delay)
                await asyncio.sleep(delay)
                continue
            if 500 <= code < 600:
                delay = min(2**attempt, 30)
                _LOGGER.warning(
                    "Telegram server error %s (%s); retrying in %ss",
                    code,
                    description,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            raise _error_class(code, description)(f"{code}: {description}")

        raise TelegramError(f"sendVideo failed after retries: {last_error}")

    async def send_message(self, text: str) -> int:
        """Send a plain HTML message (used for operational alerts)."""
        payload = {
            "chat_id": self._chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                async with self._session.post(
                    self._url("sendMessage"), json=payload, timeout=CALL_TIMEOUT
                ) as response:
                    status = response.status
                    data = await self._json(response)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                await asyncio.sleep(min(2**attempt, 20))
                continue

            if data.get("ok"):
                return int((data.get("result") or {}).get("message_id", 0))

            code = int(data.get("error_code", status))
            description = str(data.get("description", "unknown error"))
            if code == 429:
                await asyncio.sleep(self._retry_after(data))
                continue
            if 500 <= code < 600:
                await asyncio.sleep(min(2**attempt, 20))
                continue
            raise _error_class(code, description)(f"{code}: {description}")

        raise TelegramError(f"sendMessage failed after retries: {last_error}")

    async def get_me(self) -> dict:
        return await self._get("getMe")

    async def get_chat(self) -> dict:
        return await self._get("getChat", params={"chat_id": self._chat_id})

    async def get_chat_member(self, user_id: int) -> dict:
        return await self._get(
            "getChatMember",
            params={"chat_id": self._chat_id, "user_id": user_id},
        )

    async def get_chat_administrators(self) -> list[dict]:
        """Return the chat's administrator list (requires access to the chat)."""
        try:
            async with self._session.get(
                self._url("getChatAdministrators"),
                params={"chat_id": self._chat_id},
                timeout=CALL_TIMEOUT,
            ) as response:
                data = await self._json(response)
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TelegramError(
                f"getChatAdministrators request failed: {exc}"
            ) from exc
        if not data.get("ok"):
            code = data.get("error_code", status)
            description = data.get("description", "unknown error")
            raise _error_class(int(code), str(description))(
                f"{code}: {description}"
            )
        result = data.get("result")
        if not isinstance(result, list):
            raise TelegramError(
                f"Unexpected getChatAdministrators result: {result!r}"
            )
        return result

    async def _get(self, method: str, params: dict | None = None) -> dict:
        try:
            async with self._session.get(
                self._url(method), params=params, timeout=CALL_TIMEOUT
            ) as response:
                data = await self._json(response)
                status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TelegramError(f"{method} request failed: {exc}") from exc
        if not data.get("ok"):
            code = data.get("error_code", status)
            description = data.get("description", "unknown error")
            raise _error_class(int(code), str(description))(
                f"{code}: {description}"
            )
        result = data.get("result")
        if not isinstance(result, dict):
            raise TelegramError(f"Unexpected {method} result: {result!r}")
        return result
