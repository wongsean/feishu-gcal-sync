"""Crash-safe file writes and a process lock for the state directory."""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace `path` with `text` so a crash leaves either the old or the new file, never half.

    The file is created 0600 because the state directory holds tokens.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


class ReservedFile:
    """Claims the disk space for a replacement of `path` up front, so that writing it later
    cannot fail for lack of space. Use it when the content comes from something that can't
    be repeated, like spending a single-use refresh_token.
    """

    RESERVE = 16 * 1024

    def __init__(self, path: Path):
        self.path = path
        self._tmp = ""
        self._file: BinaryIO | None = None

    def __enter__(self) -> ReservedFile:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, self._tmp = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        self._file = os.fdopen(fd, "r+b")
        try:
            self._file.write(b" " * self.RESERVE)
            self._file.flush()
            os.fsync(self._file.fileno())
        except BaseException:
            self._discard()
            raise
        return self

    def commit(self, text: str) -> None:
        data = text.encode()
        if self._file is None or len(data) > self.RESERVE:
            raise ValueError("nothing reserved, or content larger than the reservation")
        f, self._file = self._file, None
        try:
            f.seek(0)
            f.write(data)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
            f.close()
            os.replace(self._tmp, self.path)
        except BaseException:
            # Keep the temp file: it may hold the only copy of what was being saved.
            with contextlib.suppress(OSError):
                f.close()
            log.error("could not replace %s; the new content may be in %s", self.path, self._tmp)
            raise
        _fsync_dir(self.path.parent)

    def __exit__(self, *exc: object) -> None:
        if self._file is not None:
            self._discard()

    def _discard(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._tmp)


def _fsync_dir(path: Path) -> None:
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class LockBusy(Exception):
    pass


class RunLock:
    """Exclusive flock so a timer run and a manual command never rotate the token at once."""

    def __init__(self, path: Path, blocking: bool = False):
        self.path = path
        self.blocking = blocking
        self._fd: int | None = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockBusy(str(self.path)) from None
        self._fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
