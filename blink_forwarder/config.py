"""Configuration loaded from environment variables / an env file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or invalid."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime settings for one forwarder run."""

    telegram_bot_token: str
    telegram_chat_id: str
    blink_login_file: Path
    db_path: Path
    archive_dir: Path
    tmp_dir: Path
    tz: ZoneInfo
    tz_name: str
    max_file_bytes: int
    max_clips_per_run: int
    send_delay_seconds: float
    retention_days: int
    silent: bool
    dry_run: bool
    log_level: str

    def ensure_dirs(self) -> None:
        """Create the writable directories the forwarder needs."""
        for path in (
            self.db_path.parent,
            self.archive_dir,
            self.tmp_dir,
            self.blink_login_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_env_file() -> None:
    """Load ``BLINK_ENV_FILE`` or the nearest ``.env`` into the environment.

    Existing environment variables always win over the file.
    """
    env_file = os.environ.get("BLINK_ENV_FILE")
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        load_dotenv(override=False)


def load_settings(
    *,
    dry_run: bool | None = None,
    max_clips: int | None = None,
) -> Settings:
    """Load and validate settings from the environment.

    ``BLINK_ENV_FILE`` can point at a specific env file. Otherwise a ``.env``
    file in the current directory (or a parent) is loaded when present.
    Existing environment variables always win over the file.
    """
    load_env_file()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is required")
    if not TOKEN_RE.fullmatch(token):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN does not look like a bot token "
            "(expected '<digits>:<letters/digits/_->')"
        )
    if not chat_id:
        raise ConfigError("TELEGRAM_CHAT_ID is required (for example -1001234567890)")

    data_dir = Path(os.environ.get("BLINK_DATA_DIR", "data")).expanduser()
    login_file = Path(
        os.environ.get("BLINK_LOGIN_FILE", str(data_dir / "blink_login.json"))
    ).expanduser()
    db_path = Path(
        os.environ.get("BLINK_DB", str(data_dir / "state.db"))
    ).expanduser()
    archive_dir = Path(
        os.environ.get("BLINK_ARCHIVE", str(data_dir / "archive"))
    ).expanduser()
    tmp_dir = Path(
        os.environ.get("BLINK_TMP", str(data_dir / "tmp"))
    ).expanduser()

    tz_name = os.environ.get("TZ", "UTC").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown TZ value {tz_name!r}") from exc

    max_file_mb = _env_int("MAX_FILE_MB", 49)
    if not 1 <= max_file_mb <= 2000:
        raise ConfigError("MAX_FILE_MB must be between 1 and 2000")

    clips_per_run = max_clips if max_clips is not None else _env_int(
        "MAX_CLIPS_PER_RUN", 50
    )
    if clips_per_run < 1:
        raise ConfigError("MAX_CLIPS_PER_RUN must be at least 1")

    send_delay = _env_float("SEND_DELAY_SECONDS", 4.0)
    if send_delay < 0:
        raise ConfigError("SEND_DELAY_SECONDS cannot be negative")

    retention_days = _env_int("RETENTION_DAYS", 90)
    if retention_days < 0:
        raise ConfigError("RETENTION_DAYS cannot be negative (use 0 to keep forever)")

    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"Invalid LOG_LEVEL {log_level!r}")

    return Settings(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        blink_login_file=login_file,
        db_path=db_path,
        archive_dir=archive_dir,
        tmp_dir=tmp_dir,
        tz=tz,
        tz_name=tz_name,
        # Use decimal megabytes so the 50,000,000 byte Bot API ceiling is never
        # crossed by a rounding mistake.
        max_file_bytes=max_file_mb * 1_000_000,
        max_clips_per_run=clips_per_run,
        send_delay_seconds=send_delay,
        retention_days=retention_days,
        silent=_env_bool("TELEGRAM_SILENT", False),
        dry_run=_env_bool("DRY_RUN", False) if dry_run is None else dry_run,
        log_level=log_level,
    )
