"""Concurrency-safe Google Calendar OAuth token handling."""

from __future__ import annotations

import fcntl
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar.events",)


def is_invalid_grant(error: BaseException | str) -> bool:
    text = str(error or "").lower()
    return "invalid_grant" in text or "expired or revoked" in text


def is_transient_refresh_error(error: BaseException | str) -> bool:
    if isinstance(error, TransportError):
        return True
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "connection",
            "eof",
            "network",
            "ssl",
            "temporarily",
            "timed out",
            "timeout",
        )
    )


@contextmanager
def token_lock(token_path: Path) -> Iterator[None]:
    """Serialize token reads and refreshes across scheduled and chat agents."""
    lock_path = token_path.with_suffix(token_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_token(
    token_json: str,
    token_path: Path,
    *,
    keep_backup: bool = True,
) -> None:
    """Install a token atomically and keep a private last-known copy."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = token_path.with_suffix(token_path.suffix + f".tmp.{os.getpid()}")
    backup = token_path.with_suffix(token_path.suffix + ".bak")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(token_json)
            output.flush()
            os.fsync(output.fileno())
        if keep_backup and token_path.exists():
            shutil.copy2(token_path, backup)
            os.chmod(backup, 0o600)
        os.replace(temporary, token_path)
        os.chmod(token_path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_calendar_credentials(
    token_path: Path,
    *,
    scopes: tuple[str, ...] = CALENDAR_SCOPES,
    attempts: int = 3,
    request_factory: Callable[[], Request] = Request,
    credentials_loader: Callable[..., Credentials] = Credentials.from_authorized_user_file,
    sleep: Callable[[float], None] = time.sleep,
) -> Credentials:
    """Load credentials, refreshing once under a cross-process file lock."""
    if not token_path.exists():
        raise RuntimeError("Google Calendar token.json не найден — нужна повторная авторизация")

    with token_lock(token_path):
        credentials = credentials_loader(str(token_path), list(scopes))
        if credentials.valid:
            return credentials
        if not credentials.expired or not credentials.refresh_token:
            raise RuntimeError("Google Calendar token.json не содержит рабочего refresh token")

        last_error: BaseException | None = None
        for attempt in range(max(1, attempts)):
            try:
                credentials.refresh(request_factory())
                atomic_write_token(credentials.to_json(), token_path)
                return credentials
            except RefreshError as error:
                last_error = error
                if is_invalid_grant(error) or not is_transient_refresh_error(error):
                    raise
            except (TransportError, OSError) as error:
                last_error = error
                if not is_transient_refresh_error(error):
                    raise

            if attempt < attempts - 1:
                sleep(1.2 * (attempt + 1))

        if last_error is not None:
            raise last_error
        raise RuntimeError("Google Calendar token refresh failed")
